"""vinowhisper-dictate: one utterance, recorded until told to stop, decoded once.

The recorder and the client are fakes, so this runs with no microphone and no
server; what is under test is the command handling and what gets emitted.
"""

import threading
import time

import numpy as np
import pytest
import requests

from tests import pcm
from vinowhisper import config, dictate
from vinowhisper.recorder import CaptureError

SPEECH = pcm.sine(220.0, 2.0, amplitude=0.1)


class FakeRecording:
    def __init__(self, tap, signal: np.ndarray, fail_on_enter: bool = False) -> None:
        self._tap = tap
        self._signal = signal
        self._fail = fail_on_enter
        self.entered = False
        self.exited = False

    def __enter__(self):
        if self._fail:
            raise CaptureError("pw-record not found")
        self.entered = True
        for chunk in pcm.chunks(self._signal):
            self._tap(chunk)
        return self

    def __exit__(self, *exc_info):
        self.exited = True

    def check_alive(self) -> None:
        pass

    @property
    def captured_s(self) -> float:
        return self._signal.size / config.SAMPLE_RATE_HZ


class FakeClient:
    def __init__(
        self,
        transcript: str = "Hello there.",
        error: Exception | None = None,
        script: list[str] | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        self.transcript = transcript
        self.error = error
        self.script = script
        self.gate = gate
        self.started = threading.Event()
        self.finished = threading.Event()
        self.decoded: list[np.ndarray] = []

    def wait_ready(self) -> dict:
        if self.error is not None:
            raise self.error
        return {"device": "NPU", "degraded": False, "warnings": []}

    def transcribe(self, samples: np.ndarray) -> tuple[str, float | None]:
        self.decoded.append(samples)
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5.0)
        text = self.script[len(self.decoded) - 1] if self.script else self.transcript
        self.finished.set()
        return text, 0.2


@pytest.fixture(autouse=True)
def _no_tail(monkeypatch):
    monkeypatch.setattr(dictate, "TAIL_S", 0.0)


def _run(commands, signal=SPEECH, client=None, fail_on_enter=False):
    records: list[dict] = []
    recordings: list[FakeRecording] = []
    client = client or FakeClient()

    def recorder(tap):
        recordings.append(FakeRecording(tap, signal, fail_on_enter))
        return recordings[-1]

    dictation = dictate.Dictation(records.append, client=client, recorder=recorder)
    dictation.feed(commands).join()
    dictation.run()
    return [r for r in records if r["event"] != "Level"], recordings, client


def test_start_then_stop_decodes_once_and_reports_the_device():
    records, recordings, client = _run(["start", "stop"])
    assert [r["event"] for r in records] == ["Listening", "Transcribing", "Ready", "Dictated"]
    assert records[-1]["text"] == "Hello there."
    assert records[2]["device"] == "NPU"
    assert len(client.decoded) == 1
    assert recordings[0].exited


def test_audio_is_normalized_before_it_is_decoded():
    quiet = pcm.sine(220.0, 2.0, amplitude=0.01)
    _, _, client = _run(["start", "stop"], signal=quiet)
    assert dictate.audio.rms(client.decoded[0]) > dictate.audio.rms(quiet)


def test_silence_or_a_bare_tap_costs_no_decode():
    for signal in (pcm.silence(2.0), pcm.sine(220.0, 0.1, amplitude=0.1)):
        records, _, client = _run(["start", "stop"], signal=signal)
        assert records[-1] == {**records[-1], "event": "Dictated", "text": ""}
        assert client.decoded == []


def test_audio_just_under_the_minimum_costs_no_decode():
    just_under = pcm.sine(220.0, dictate.MIN_AUDIO_S - 0.05, amplitude=0.1)
    _, _, client = _run(["start", "stop"], signal=just_under)
    assert client.decoded == []


def test_audio_just_over_the_minimum_is_decoded():
    just_over = pcm.sine(220.0, dictate.MIN_AUDIO_S + 0.05, amplitude=0.1)
    _, _, client = _run(["start", "stop"], signal=just_over)
    assert len(client.decoded) == 1


def test_cancel_discards_the_recording():
    records, recordings, client = _run(["start", "cancel"])
    assert [r["event"] for r in records] == ["Listening", "Cancelled"]
    assert client.decoded == []
    assert recordings[0].exited


def test_stop_without_start_and_a_second_start_are_ignored():
    records, recordings, _ = _run(["stop", "start", "start", "stop"])
    assert [r["event"] for r in records] == ["Listening", "Transcribing", "Ready", "Dictated"]
    assert len(recordings) == 1


def test_the_overall_cap_stops_and_decodes_by_itself(monkeypatch):
    monkeypatch.setattr(config, "DICTATION_MAX_S", 3.0)
    records: list[dict] = []
    client = FakeClient()
    long = pcm.sine(220.0, 4.0, amplitude=0.1)
    dictation = dictate.Dictation(
        records.append, client=client, recorder=lambda tap: FakeRecording(tap, long)
    )
    dictation.handle("start")
    dictation.handle(dictation._commands.get_nowait())
    assert records[-1]["event"] == "Dictated"
    assert len(client.decoded) == 1
    assert [r for r in records if r["event"] == "Listening"] == [
        {"event": "Listening", "limit_s": 3.0}
    ]


def test_a_stale_full_signal_does_not_stop_the_next_recording():
    records: list[dict] = []
    dictation = dictate.Dictation(
        records.append,
        client=FakeClient(),
        recorder=lambda tap: FakeRecording(tap, SPEECH),
    )
    dictation.handle("start")
    dictation.handle("stop")
    dictation.handle("start")
    dictation.handle("full 1")
    assert [r["event"] for r in records if r["event"] != "Level"][-1] == "Listening"


def test_a_microphone_that_will_not_open_is_an_error_not_a_crash():
    records, _, _ = _run(["start", "stop"], fail_on_enter=True)
    assert [r["event"] for r in records] == ["Error"]
    assert "pw-record" in records[0]["message"]


def test_an_unreachable_server_names_the_doctor():
    client = FakeClient(error=requests.ConnectionError("refused"))
    records, _, _ = _run(["start", "stop"], client=client)
    assert records[-1]["event"] == "Error"
    assert "vinowhisper-doctor" in records[-1]["message"]


def test_unknown_commands_are_reported():
    records, _, _ = _run(["frobnicate"])
    assert records == [{"event": "Error", "message": "unknown command 'frobnicate'"}]


def test_the_tail_waits_for_audio_after_the_key_comes_up(monkeypatch):
    monkeypatch.setattr(dictate, "TAIL_S", 0.25)

    class Growing:
        reads = 0

        @property
        def captured_s(self) -> float:
            self.reads += 1
            return 1.0 + 0.1 * self.reads

    growing = Growing()
    dictate._wait_for_tail(growing)  # type: ignore[arg-type]
    assert growing.captured_s >= 1.35


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Hello there. ", "Hello there."),
        ("[BLANK_AUDIO]", ""),
        ("(silence)", ""),
        ("you" * 40, "youyouyou"),
        ("I said (quietly) hello", "I said (quietly) hello"),
    ],
)
def test_clean_drops_whisper_non_speech_and_runaway_repeats(raw, expected):
    assert dictate.clean(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hello\x1b[201~\x1b[200~ world", "hello[201~[200~ world"),
        ("sudo\u202e rm", "sudo rm"),
        ("zero\u200bwidth\u200d", "zerowidth"),
        ("line\none\x00", "line one"),
    ],
)
def test_clean_keeps_control_characters_off_the_clipboard(raw, expected):
    assert dictate.clean(raw) == expected


def test_every_field_the_overlay_reads_is_emitted():
    """gui/src/protocol.rs `Dictate` reads these; renaming one blanks the pill silently."""
    records, _, _ = _run(["start", "stop"])
    by_event = {r["event"]: r for r in records}
    assert {"Listening", "Transcribing", "Ready", "Dictated"} <= by_event.keys()
    assert isinstance(by_event["Dictated"]["text"], str)
    assert {"device", "degraded"} <= by_event["Ready"].keys()

    levels: list[dict] = []
    dictation = dictate.Dictation(
        levels.append, client=FakeClient(), recorder=lambda tap: FakeRecording(tap, SPEECH)
    )
    dictation.handle("start")
    assert any(r["event"] == "Level" and isinstance(r["rms"], float) for r in levels)
    dictation.handle("cancel")
    assert levels[-1] == {"event": "Cancelled"}
    assert _run(["bogus"])[0][0].keys() == {"event", "message"}


def _speech_with_pauses(phrases: int, speech_s: float = 6.0, pause_s: float = 0.5) -> np.ndarray:
    parts = []
    for _ in range(phrases):
        parts += [pcm.sine(220.0, speech_s, amplitude=0.1), pcm.silence(pause_s)]
    return np.concatenate(parts)


def _wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert condition()


def _dictation(signal, client, records):
    return dictate.Dictation(
        records.append, client=client, recorder=lambda tap: FakeRecording(tap, signal)
    )


def test_a_long_utterance_with_pauses_decodes_in_segments_and_joins_in_order():
    signal = np.concatenate([_speech_with_pauses(6), pcm.sine(220.0, 2.0, amplitude=0.1)])
    assert signal.size / config.SAMPLE_RATE_HZ > 40
    client = FakeClient(script=[f"part{n}." for n in range(7)])
    records, _, client = _run(["start", "stop"], signal=signal, client=client)
    assert len(client.decoded) == 7
    assert all(chunk.size / config.SAMPLE_RATE_HZ < config.MAX_WINDOW_S for chunk in client.decoded)
    assert sum(chunk.size for chunk in client.decoded) == signal.size
    done = records[-1]
    assert done["event"] == "Dictated"
    assert done["text"] == " ".join(f"part{n}." for n in range(7))
    assert done["audio_s"] == pytest.approx(signal.size / config.SAMPLE_RATE_HZ)
    assert [r["event"] for r in records].count("Dictated") == 1


def test_release_decodes_only_the_tail_when_earlier_segments_are_done():
    signal = np.concatenate([_speech_with_pauses(3), pcm.sine(220.0, 1.0, amplitude=0.1)])
    client = FakeClient(script=["one.", "two.", "three.", "four."])
    records: list[dict] = []
    dictation = _dictation(signal, client, records)
    dictation.handle("start")
    _wait_for(lambda: len(client.decoded) == 3)
    _wait_for(lambda: client.finished.is_set())
    time.sleep(0.05)
    before = len(client.decoded)
    dictation.handle("stop")
    assert before == 3
    assert len(client.decoded) == before + 1
    assert client.decoded[-1].size / config.SAMPLE_RATE_HZ == pytest.approx(1.2, abs=0.01)
    assert records[-1]["text"] == "one. two. three. four."


def test_cancel_midway_types_nothing():
    signal = np.concatenate([_speech_with_pauses(3), pcm.sine(220.0, 1.0, amplitude=0.1)])
    gate = threading.Event()
    client = FakeClient(gate=gate)
    records: list[dict] = []
    dictation = _dictation(signal, client, records)
    dictation.handle("start")
    assert client.started.wait(5.0)
    dictation.handle("cancel")
    gate.set()
    _wait_for(lambda: len(client.decoded) >= 1)
    time.sleep(0.1)
    events = [r["event"] for r in records if r["event"] != "Level"]
    assert events == ["Listening", "Cancelled"]
    assert len(client.decoded) == 1


def test_an_utterance_shorter_than_the_minimum_segment_is_one_request():
    signal = np.concatenate(
        [pcm.sine(220.0, 2.0, amplitude=0.1), pcm.silence(0.6), pcm.sine(220.0, 1.5, amplitude=0.1)]
    )
    records, _, client = _run(["start", "stop"], signal=signal)
    assert len(client.decoded) == 1
    assert client.decoded[0].size == signal.size
    assert records[-1]["text"] == "Hello there."


def test_speech_with_no_pause_is_cut_at_the_quietest_chunk_of_the_last_seconds():
    signal = pcm.sine(220.0, 40.0, amplitude=0.1)
    dip = int(27.0 * config.SAMPLE_RATE_HZ)
    signal[dip : dip + pcm.READ_CHUNK_SAMPLES] *= 0.1
    client = FakeClient(script=["first.", "second."])
    records, _, client = _run(["start", "stop"], signal=signal, client=client)
    assert [chunk.size for chunk in client.decoded] == [
        dip + pcm.READ_CHUNK_SAMPLES,
        signal.size - dip - pcm.READ_CHUNK_SAMPLES,
    ]
    assert records[-1]["text"] == "first. second."


def test_a_silent_segment_is_not_decoded_but_the_utterance_still_is():
    signal = np.concatenate([pcm.silence(5.5), pcm.sine(220.0, 1.0, amplitude=0.1)])
    _, _, client = _run(["start", "stop"], signal=signal)
    assert len(client.decoded) == 1
    assert client.decoded[0].size == pcm.samples_for(1.5)


def test_a_failed_segment_decode_is_reported_once():
    client = FakeClient(error=None)
    client.transcribe = lambda samples: (_ for _ in ()).throw(requests.ConnectionError("x"))  # type: ignore[method-assign]
    signal = _speech_with_pauses(2)
    records, _, _ = _run(["start", "stop"], signal=signal, client=client)
    assert [r["event"] for r in records].count("Error") == 1
    assert records[-1]["event"] == "Error"
