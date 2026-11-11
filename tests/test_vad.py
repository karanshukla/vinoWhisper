"""The speech detector for file imports (#180): its pin, its download, and how it
steps through audio. A fake openvino stands in for the runtime."""

import hashlib
import json
import sys
from types import ModuleType

import numpy as np
import pytest
import requests

from vinowhisper import vad

BLOB = b"not really an onnx model"


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.body


class Session:
    def __init__(self, body=BLOB, fails=False):
        self.body = body
        self.fails = fails
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if self.fails:
            raise requests.ConnectionError("offline")
        return Response(self.body)


@pytest.fixture
def pin(monkeypatch, tmp_path):
    path = tmp_path / "vad_model.json"
    files = {"m.onnx": {"sha256": hashlib.sha256(BLOB).hexdigest(), "size": len(BLOB)}}
    pinned = {"repo": "snakers4/silero-vad", "revision": "abc123", "path": "src/data"}
    path.write_text(json.dumps({**pinned, "files": files}))
    monkeypatch.setattr(vad, "PINS_PATH", path)
    return tmp_path / "models"


def test_the_packaged_pin_names_one_file_at_a_commit():
    pin = vad.pinned()
    assert pin is not None and len(pin.sha256) == 1
    assert len(pin.revision) == 40
    (name,) = pin.sha256
    assert pin.url(name).startswith(
        f"https://raw.githubusercontent.com/snakers4/silero-vad/{pin.revision}/"
    )


def test_the_model_is_downloaded_once_and_checked(pin):
    session = Session()
    path = vad.model_path(pin, session=session)
    assert path.read_bytes() == BLOB
    assert session.urls == [
        "https://raw.githubusercontent.com/snakers4/silero-vad/abc123/src/data/m.onnx"
    ]
    assert vad.model_path(pin, session=Session(fails=True)) == path


def test_a_download_that_does_not_match_the_pin_is_refused(pin):
    with pytest.raises(vad.VadError, match="sha256"):
        vad.model_path(pin, session=Session(body=b"tampered"))
    assert not (pin / "m.onnx").exists()


def test_offline_is_a_vad_error_not_a_crash(pin):
    with pytest.raises(vad.VadError, match="offline"):
        vad.model_path(pin, session=Session(fails=True))


def test_without_a_detector_the_import_goes_on_and_says_why(monkeypatch):
    def missing(directory=None, session=None):
        raise vad.VadError("could not fetch the speech detector (offline)")

    monkeypatch.setattr(vad, "model_path", missing)
    said = []
    assert vad.detector(said.append) is None
    assert said == [
        "could not fetch the speech detector (offline); paragraphs break on silence only"
    ]


@pytest.fixture
def runtime(monkeypatch):
    seen = []

    class Request:
        def infer(self, inputs):
            seen.append(inputs)
            window, state = inputs["input"], inputs["state"]
            return {"output": np.array([[float(window[0, -1])]]), "stateN": state + 1}

    class Compiled:
        def create_infer_request(self):
            return Request()

    class Core:
        def read_model(self, path):
            return path

        def compile_model(self, model, device, options):
            assert device == "CPU"
            return Compiled()

    openvino = ModuleType("openvino")
    openvino.Core = Core
    monkeypatch.setitem(sys.modules, "openvino", openvino)
    return seen


def test_audio_is_fed_in_512_sample_steps_with_64_of_context(runtime, tmp_path):
    detector = vad.SpeechDetector(tmp_path / "m.onnx")
    frame = np.arange(1600, dtype=np.float32) / 1600
    probability = detector(frame)
    # 1600 samples is three steps; the last 64 wait for the next frame.
    assert [inputs["input"].shape for inputs in runtime] == [(1, 576)] * 3
    assert not runtime[0]["input"][0, :64].any()
    np.testing.assert_array_equal(runtime[1]["input"][0, :64], frame[448:512])
    assert [float(inputs["state"].flat[0]) for inputs in runtime] == [0.0, 1.0, 2.0]
    assert probability == pytest.approx(frame[1535])

    detector(np.ones(448, dtype=np.float32))
    np.testing.assert_array_equal(runtime[3]["input"][0, 64:128], frame[1536:])


def test_a_frame_too_short_for_a_step_repeats_the_last_answer(runtime, tmp_path):
    detector = vad.SpeechDetector(tmp_path / "m.onnx")
    detector(np.full(512, 0.7, dtype=np.float32))
    assert detector(np.zeros(100, dtype=np.float32)) == pytest.approx(0.7)
    assert len(runtime) == 1
