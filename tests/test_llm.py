"""The local cleanup model: its pins, the download, and the pipeline's lifecycle.

The session and the pipeline are fakes, so nothing here touches the network, the NPU or
OpenVINO. The real model ran on the NPU on 2026-10-27; see docs/tray.md.
"""

import hashlib
import json
import re
import sys
import threading
import types

import pytest

from tests.test_source import _Session
from vinowhisper import cleanup, config, llm, source

FILES = {"openvino_model.xml": b"<net/>", "openvino_model.bin": b"\x01" * 5000}


def _pin(files=FILES, revision="b" * 40) -> source.Source:
    return source.Source(
        model_id="org/cleanup",
        revision=revision,
        sha256={name: hashlib.sha256(body).hexdigest() for name, body in files.items()},
        sizes={name: len(body) for name, body in files.items()},
    )


def test_the_shipped_pin_is_a_commit_with_every_file_the_pipeline_loads():
    pin = llm.pinned()
    assert pin is not None
    assert pin.model_id == config.CLEANUP_MODEL_ID
    assert re.fullmatch(r"[0-9a-f]{40}", pin.revision)
    for name in (
        "openvino_model.xml",
        "openvino_model.bin",
        "openvino_tokenizer.xml",
        "openvino_detokenizer.xml",
        "chat_template.jinja",
    ):
        assert re.fullmatch(r"[0-9a-f]{64}", pin.sha256[name]), name
        assert pin.sizes[name] > 0


def test_the_pin_file_ships_in_the_wheel():
    from pathlib import Path

    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    assert '"cleanup_model.json"' in pyproject


def test_a_fetch_checks_every_file_reports_progress_and_marks_the_revision(tmp_path):
    pin = _pin()
    seen = []
    llm.fetch(lambda done, total: seen.append((done, total)), pin, tmp_path, _Session(FILES))
    assert llm.is_present(pin, tmp_path)
    total = sum(pin.sizes.values())
    assert seen[-1] == (total, total)
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)
    assert (tmp_path / llm.REVISION_MARKER).read_text().strip() == pin.revision


def test_a_corrupt_download_leaves_no_marker(tmp_path):
    pin = _pin()
    bad = {**FILES, "openvino_model.bin": b"\x02" * 5000}
    with pytest.raises(source.SourceError):
        llm.fetch(pin=pin, directory=tmp_path, session=_Session(bad))
    assert not llm.is_present(pin, tmp_path)


def test_a_verify_rehashes_and_downloads_only_what_changed(tmp_path):
    pin = _pin()
    llm.fetch(pin=pin, directory=tmp_path, session=_Session(FILES))
    (tmp_path / "openvino_model.bin").write_bytes(b"\x03" * 5000)
    again = _Session(FILES)
    llm.fetch(pin=pin, directory=tmp_path, session=again)
    assert [url.rsplit("/", 1)[1] for url in again.fetched] == ["openvino_model.bin"]
    assert llm.is_present(pin, tmp_path)


def test_presence_is_the_marker_at_this_revision_and_every_file_at_its_size(tmp_path):
    pin = _pin()
    llm.fetch(pin=pin, directory=tmp_path, session=_Session(FILES))
    assert not llm.is_present(_pin(revision="c" * 40), tmp_path)
    (tmp_path / "openvino_model.xml").write_bytes(b"<net></net>")
    assert not llm.is_present(pin, tmp_path)


def test_status_and_delete(tmp_path):
    pin, model, cache = _pin(), tmp_path / "model", tmp_path / "cache"
    assert llm.status(pin, model, cache)["present"] is False
    llm.fetch(pin=pin, directory=model, session=_Session(FILES))
    (cache / "NPU").mkdir(parents=True)
    (cache / "NPU" / "blob").write_bytes(b"\0" * 10)
    status = llm.status(pin, model, cache)
    assert status["present"] and status["bytes"] >= sum(pin.sizes.values())
    assert status["cache_bytes"] == 10
    assert status["download_bytes"] == sum(pin.sizes.values())
    llm.delete(model, cache)
    assert not model.exists() and not cache.exists()


def test_the_status_command_prints_one_json_line(capsys):
    assert cleanup.main(["model", "status"]) == 0
    line = json.loads(capsys.readouterr().out)
    assert set(line) >= {
        "label",
        "repo",
        "revision",
        "download_bytes",
        "present",
        "bytes",
        "cache_bytes",
        "dir",
    }
    assert line["present"] is False


def test_a_failed_fetch_command_says_so_as_json(monkeypatch, capsys):
    def offline(progress=None, **_):
        raise source.SourceError("does not match its pinned sha256\nmore detail")

    monkeypatch.setattr(llm, "fetch", offline)
    assert cleanup.main(["model", "fetch"]) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [{"event": "error", "message": "does not match its pinned sha256"}]


class _Status:
    RUNNING, STOP, CANCEL = 0, 1, 2


class _Ids:
    def __init__(self, n):
        self.data = [list(range(n))]


class _Encoded:
    def __init__(self, n):
        self.input_ids = _Ids(n)


class _Tokenizer:
    def __init__(self):
        self.history = None

    def apply_chat_template(self, history, add_generation_prompt):
        self.history = history
        return "PROMPT"

    def encode(self, text):
        return _Encoded(len(text.split()))


class _Pipe:
    def __init__(self, answer="Hello there.", pieces=3, on_piece=None):
        self.answer, self.pieces, self.on_piece = answer, pieces, on_piece
        self.tokenizer = _Tokenizer()
        self.configs = []

    def get_tokenizer(self):
        return self.tokenizer

    def generate(self, prompt, generation_config, streamer):
        self.configs.append(generation_config)
        for _ in range(self.pieces):
            if self.on_piece:
                self.on_piece()
            if streamer("x") == _Status.CANCEL:
                return "partial"
        return self.answer


class _Config:
    pass


@pytest.fixture
def genai(monkeypatch):
    module = types.ModuleType("openvino_genai")
    module.StreamingStatus = _Status  # type: ignore[attr-defined]
    module.GenerationConfig = _Config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openvino_genai", module)
    return module


def _loaded(pipe, clock=lambda: 0.0) -> llm.CleanupModel:
    model = llm.CleanupModel(clock=clock)
    model._pipe, model.device = pipe, "NPU"
    return model


def test_generate_turns_reasoning_off_decodes_greedily_and_strips_think_blocks(genai):
    pipe = _Pipe(answer="<think>\n\n</think>\nHello there.")
    answer, truncated = _loaded(pipe).generate("SYSTEM", "<transcript>hi</transcript>", 64)
    assert (answer, truncated) == ("Hello there.", False)
    assert pipe.tokenizer.history[0] == {"role": "system", "content": "SYSTEM /no_think"}
    settings = pipe.configs[0]
    assert (settings.max_new_tokens, settings.do_sample, settings.apply_chat_template) == (
        64,
        False,
        False,
    )
    assert settings.repetition_penalty == 1.0


def test_an_answer_that_fills_the_token_cap_is_truncated(genai):
    answer, truncated = _loaded(_Pipe(answer="a b c d")).generate("S", "U", 4)
    assert truncated


def test_generation_stops_at_the_deadline_and_says_so(genai):
    now = [0.0]

    def tick():
        now[0] += 1.0

    pipe = _Pipe(pieces=100, on_piece=tick)
    model = _loaded(pipe, clock=lambda: now[0])
    with pytest.raises(TimeoutError):
        model.generate("S", "U", 512, budget_s=2.5)
    assert now[0] < 10, "it kept generating past the deadline"
    assert not model._lock.locked()


def test_the_model_unloads_only_after_sitting_idle(genai):
    now = [0.0]
    model = _loaded(_Pipe(), clock=lambda: now[0])
    model.generate("S", "U", 8)
    now[0] = config.CLEANUP_IDLE_UNLOAD_S - 1
    assert not model.unload_if_idle(config.CLEANUP_IDLE_UNLOAD_S)
    now[0] = config.CLEANUP_IDLE_UNLOAD_S + 1
    assert model.unload_if_idle(config.CLEANUP_IDLE_UNLOAD_S)
    assert not model.loaded and model.device is None


def test_a_failed_or_timed_out_run_still_counts_as_use(genai):
    """vinoPop's idle unload was skipped after a cancelled run (2026-10-27), which left
    ~2.5GB pinned by the NPU driver; here the timestamp is set in a finally."""
    now = [0.0]

    class Broken(_Pipe):
        def generate(self, prompt, generation_config, streamer):
            now[0] = 100.0
            raise RuntimeError("npu went away")

    model = _loaded(Broken(), clock=lambda: now[0])
    with pytest.raises(RuntimeError):
        model.generate("S", "U", 8)
    now[0] = 100.0 + config.CLEANUP_IDLE_UNLOAD_S - 1
    assert not model.unload_if_idle(config.CLEANUP_IDLE_UNLOAD_S)


def test_the_model_is_not_unloaded_mid_answer(genai):
    started, release = threading.Event(), threading.Event()

    def hold():
        started.set()
        release.wait(5)

    model = _loaded(_Pipe(pieces=1, on_piece=hold), clock=lambda: 1e9)
    worker = threading.Thread(target=model.generate, args=("S", "U", 8))
    worker.start()
    started.wait(5)
    try:
        assert not model.unload_if_idle(0)
    finally:
        release.set()
        worker.join(5)


def test_loading_without_the_download_says_so(tmp_path):
    model = llm.CleanupModel(directory=tmp_path / "absent")
    with pytest.raises(llm.ModelMissing):
        model.load()


def test_load_falls_through_the_devices_in_order(genai, monkeypatch, tmp_path):
    pin = _pin()
    llm.fetch(pin=pin, directory=tmp_path, session=_Session(FILES))
    monkeypatch.setattr(llm, "pinned", lambda: pin)
    monkeypatch.setattr(llm.ovfetch, "preload", lambda: [])
    found = [
        llm.devices.Device("GPU", "GPU"),
        llm.devices.Device("NPU", "NPU"),
        llm.devices.Device("CPU", "CPU"),
    ]
    monkeypatch.setattr(llm.devices, "available", lambda: found)
    tried = []

    def build(self, device):
        tried.append(device)
        if device == "NPU":
            raise RuntimeError("compile failed\nlong trace")
        return _Pipe()

    monkeypatch.setattr(llm.CleanupModel, "_build", build)
    model = llm.CleanupModel(directory=tmp_path)
    model.load()
    assert tried == ["NPU", "GPU"]
    assert model.device == "GPU"
