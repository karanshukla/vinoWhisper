"""The HTTP server, with the transcriber faked out.

server.py imports flask and the transcriber, never openvino at import time, so
these run in CI. Nothing here loads a model or binds the real socket.
"""

import socket

import pytest

from vinowhisper import audio, config, devices, failures, listen, server

SAMPLES_PER_S = config.SAMPLE_RATE_HZ


class FakeTranscriber:
    def __init__(self, words=("a", "b"), raises=None):
        self.words = words
        self.raises = raises
        self.seen = None

    def transcribe_stream(self, samples):
        self.seen = samples
        yield from self.words
        if self.raises is not None:
            raise self.raises

    def describe(self):
        return {"device": "CPU", "loaded": True}


@pytest.fixture
def fake(monkeypatch):
    transcriber = FakeTranscriber()
    monkeypatch.setattr(server, "transcriber", transcriber)
    monkeypatch.setattr(server, "_in_flight", 0)
    return transcriber


@pytest.fixture
def client(fake):
    return server.app.test_client()


def pcm(seconds: float) -> bytes:
    return b"\0" * (int(seconds * SAMPLES_PER_S) * audio.BYTES_PER_SAMPLE)


def test_a_valid_window_streams_the_words_back(client, fake):
    response = client.post("/transcribe", data=pcm(1))
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ab"
    assert fake.seen.size == SAMPLES_PER_S
    assert fake.seen.flags.writeable


@pytest.mark.parametrize("body", [b"", b"\0" * 5, b"\0" * 7])
def test_an_empty_or_ragged_body_is_a_400(client, body):
    response = client.post("/transcribe", data=body)
    assert response.status_code == 400
    assert "multiple of 4" in response.get_json()["error"]


def test_audio_longer_than_the_window_is_refused(client):
    # Over MAX_WINDOW_S but under MAX_CONTENT_LENGTH: the duration check, not Flask's.
    body = pcm(config.MAX_WINDOW_S) + b"\0" * audio.BYTES_PER_SAMPLE
    assert len(body) <= server.app.config["MAX_CONTENT_LENGTH"]
    response = client.post("/transcribe", data=body)
    assert response.status_code == 400
    assert "short-form" in response.get_json()["error"]


def test_a_body_over_the_content_limit_is_a_413(client, fake):
    response = client.post("/transcribe", data=pcm(config.MAX_WINDOW_S + 1))
    assert response.status_code == 413
    assert fake.seen is None


def test_the_window_limit_is_what_the_content_limit_allows():
    exact = int(config.MAX_WINDOW_S * SAMPLES_PER_S) * audio.BYTES_PER_SAMPLE
    assert exact < server.app.config["MAX_CONTENT_LENGTH"] <= exact + 4096


def test_in_flight_returns_to_zero_after_a_stream(client):
    assert client.post("/transcribe", data=pcm(1)).status_code == 200
    assert server._in_flight == 0


def test_in_flight_returns_to_zero_when_the_stream_raises(monkeypatch, client):
    # A stuck counter keeps the idle watchdog from ever unloading the model.
    monkeypatch.setattr(server, "transcriber", FakeTranscriber(raises=RuntimeError("npu")))
    with pytest.raises(RuntimeError):
        client.post("/transcribe", data=pcm(1)).get_data()
    assert server._in_flight == 0


def test_in_flight_returns_to_zero_when_the_client_goes_away():
    stream = server._track_in_flight(iter(["a", "b", "c"]))
    assert next(stream) == "a"
    assert server._in_flight == 1
    stream.close()
    assert server._in_flight == 0


def test_health_names_the_version_and_the_device(client):
    body = client.get("/health").get_json()
    assert body["status"] == "ok"
    assert body["version"] == server.__version__
    assert body["device"] == "CPU"


class Exited(Exception):
    pass


def run_watchdog(monkeypatch, *, idle_for, in_flight, ticks=3):
    """Run _idle_watchdog for a few sleeps; True if it called os._exit."""
    now = 1000.0
    monkeypatch.setattr(server, "_last_request_at", now - idle_for)
    monkeypatch.setattr(server, "_in_flight", in_flight)
    monkeypatch.setattr(server.time, "monotonic", lambda: now)
    sleeps = iter(range(ticks))

    def sleep(_seconds):
        if next(sleeps, None) is None:
            raise StopIteration

    def exit_(code):
        assert code == 0
        raise Exited

    monkeypatch.setattr(server.time, "sleep", sleep)
    monkeypatch.setattr(server.os, "_exit", exit_)
    try:
        server._idle_watchdog(60)
    except Exited:
        return True
    except StopIteration:
        return False
    raise AssertionError("the watchdog loop returned")


def test_the_watchdog_exits_once_idle(monkeypatch):
    assert run_watchdog(monkeypatch, idle_for=61, in_flight=0)


def test_the_watchdog_waits_while_recently_used(monkeypatch):
    assert not run_watchdog(monkeypatch, idle_for=59, in_flight=0)


def test_the_watchdog_never_exits_mid_decode(monkeypatch):
    assert not run_watchdog(monkeypatch, idle_for=3600, in_flight=1)


class FakeServer:
    served = False

    def serve_forever(self, poll_interval):
        FakeServer.served = True


class FakeThread:
    def __init__(self, **kwargs):
        assert kwargs["target"] is server._idle_watchdog

    def start(self):
        pass


class Loader:
    """Stands in for WhisperTranscriber in main(): no model, no NPU."""

    select_error: Exception | None = None
    load_error: Exception | None = None
    failure: failures.Failure | None = None

    def __init__(self, device, language, task):
        self.device = device

    def select_device(self):
        if self.select_error:
            raise self.select_error
        return devices.Selection(devices.Device("CPU", "CPU"), warnings=("below NPU",))

    def load(self):
        if self.load_error:
            raise self.load_error


@pytest.fixture
def main_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LANGUAGE_FILE", tmp_path / "absent.json")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(tmp_path / "s"))
    monkeypatch.setattr(server.listen, "listening_socket", lambda: sock)
    monkeypatch.setattr(server, "make_server", lambda *a, **k: FakeServer())
    monkeypatch.setattr(server.threading, "Thread", FakeThread)
    monkeypatch.setattr(server, "transcriber", server.transcriber)
    FakeServer.served = False
    yield sock
    sock.close()


def test_main_serves_once_the_model_loads(monkeypatch, main_env, capsys):
    monkeypatch.setattr(server, "WhisperTranscriber", Loader)
    assert server.main(["--device", "CPU"]) == 0
    assert FakeServer.served
    err = capsys.readouterr().err
    assert "loading on CPU" in err
    assert "WARNING: below NPU" in err


def test_main_fails_fast_when_the_socket_is_taken(monkeypatch, main_env, capsys):
    def taken():
        raise listen.SocketError("another server is running")

    monkeypatch.setattr(server.listen, "listening_socket", taken)
    monkeypatch.setattr(server, "WhisperTranscriber", Loader)
    assert server.main([]) == 1
    assert "another server is running" in capsys.readouterr().err
    assert not FakeServer.served


def test_main_reports_a_missing_device(monkeypatch, main_env, capsys):
    loader = type("L", (Loader,), {"select_error": devices.DeviceError("no NPU")})
    monkeypatch.setattr(server, "WhisperTranscriber", loader)
    assert server.main(["--device", "NPU"]) == 1
    assert "no NPU" in capsys.readouterr().err
    assert not FakeServer.served


def test_a_failed_load_names_how_to_clear_the_mark(monkeypatch, main_env, capsys):
    loader = type(
        "L",
        (Loader,),
        {
            "load_error": RuntimeError("compile failed"),
            "failure": failures.Failure("NPU", "2026-10-25", "compile failed"),
        },
    )
    monkeypatch.setattr(server, "WhisperTranscriber", loader)
    assert server.main([]) == 1
    err = capsys.readouterr().err
    assert "compile failed" in err
    assert failures.CLEAR_HINT in err
    assert not FakeServer.served


class FakeCleaner:
    def __init__(self, answer=("Hello.", False), raises=None):
        self.answer, self.raises = answer, raises
        self.seen = None
        self.warmed = 0
        self.device = "NPU"

    def generate(self, system, user, max_new_tokens, budget_s=None):
        self.seen = (system, user, max_new_tokens, budget_s)
        if self.raises is not None:
            raise self.raises
        return self.answer

    def warm(self):
        self.warmed += 1

    def describe(self):
        return {"loaded": True, "loading": False, "device": "NPU", "error": None}

    def unload_if_idle(self, idle_s):
        return False


@pytest.fixture
def cleaner(monkeypatch):
    fake = FakeCleaner()
    monkeypatch.setattr(server, "cleaner", fake)
    return fake


def test_cleanup_wraps_the_text_and_passes_the_budget(client, cleaner):
    from vinowhisper import cleanup

    response = client.post("/cleanup", json={"text": "um hello", "budget_s": 2.5})
    assert response.status_code == 200
    assert response.get_json() == {"text": "Hello.", "truncated": False, "device": "NPU"}
    system, user, max_new, budget = cleaner.seen
    assert system == cleanup.PROMPT
    assert user == "<transcript>um hello</transcript>"
    assert (max_new, budget) == (cleanup.max_tokens("um hello"), 2.5)


@pytest.mark.parametrize("body", [None, {}, {"text": ""}, {"text": 3}, ["um"]])
def test_cleanup_without_text_is_a_400(client, cleaner, body):
    assert client.post("/cleanup", json=body).status_code == 400
    assert cleaner.seen is None


@pytest.mark.parametrize(
    ("raises", "status"),
    [
        (server.llm.ModelMissing("the cleanup model isn't downloaded"), 409),
        (TimeoutError("past the time limit"), 504),
        (RuntimeError("could not load\ntrace"), 503),
    ],
)
def test_cleanup_failures_have_their_own_status(client, cleaner, raises, status):
    cleaner.raises = raises
    response = client.post("/cleanup", json={"text": "um hello"})
    assert response.status_code == status
    assert "\n" not in response.get_json()["error"]
    assert server._in_flight == 0


def test_warm_returns_at_once_and_health_reports_the_model(client, cleaner):
    response = client.post("/cleanup/warm")
    assert response.status_code == 202 and cleaner.warmed == 1
    assert client.get("/health").get_json()["cleanup"]["device"] == "NPU"


@pytest.mark.parametrize(
    ("budget", "expect"),
    [
        (2.5, 2.5),
        (None, config.CLEANUP_MAX_BUDGET_S),
        (600, config.CLEANUP_MAX_BUDGET_S),
        (0, config.CLEANUP_MAX_BUDGET_S),
        (-1, config.CLEANUP_MAX_BUDGET_S),
        (True, config.CLEANUP_MAX_BUDGET_S),
        ("5", config.CLEANUP_MAX_BUDGET_S),
    ],
)
def test_no_request_can_hold_the_model_past_the_servers_ceiling(client, cleaner, budget, expect):
    body = {"text": "um hello"} if budget is None else {"text": "um hello", "budget_s": budget}
    assert client.post("/cleanup", json=body).status_code == 200
    assert cleaner.seen[3] == expect


def test_summaries_only_open_saved_sessions_in_the_transcripts_folder(
    monkeypatch, client, tmp_path
):
    kicked = []
    monkeypatch.setattr(server.summariser, "kick", lambda paths: kicked.append(paths))
    monkeypatch.setattr(server.config, "TRANSCRIPT_DIR", tmp_path / "t")
    (tmp_path / "t").mkdir()
    inside = tmp_path / "t" / "2026-10-28-130558.vwsession"
    inside.write_bytes(b"")
    outside = tmp_path / "elsewhere.vwsession"
    outside.write_bytes(b"")

    assert client.post("/summaries", json={"paths": [str(inside)]}).status_code == 202
    assert kicked == [[inside.resolve()]]
    for raw in [str(outside), "../elsewhere.vwsession", "/etc/passwd", "t/../../x.vwsession"]:
        response = client.post("/summaries", json={"paths": [raw]})
        assert response.status_code == 400, raw
    assert client.post("/summaries", json={}).status_code == 202
