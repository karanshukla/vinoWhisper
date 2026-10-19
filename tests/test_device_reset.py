"""An NPU reset (ZE_RESULT_ERROR_DEVICE_LOST) mid-request: reload, and if the
device doesn't come back, fall back rather than retry it on every request."""

import sys
from types import ModuleType

import numpy as np
import pytest

from vinowhisper import config, devices, failures
from vinowhisper.transcriber import WhisperTranscriber

LOST = "Exception from src/plugins/intel_npu: L0 zeFenceHostSynchronize result: ZE_RESULT_ERROR_DEVICE_LOST"
INVENTORY = [devices.Device("NPU", "NPU"), devices.Device("CPU", "CPU")]


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A fake openvino_genai: `down` devices fail to build a pipeline, and the
    first generate on the NPU loses the device."""
    state = {"down": set(), "built": [], "lose_next": True}

    class Pipeline:
        def __init__(self, model_dir, device, **kwargs):
            if device in state["down"]:
                raise RuntimeError(f"{device}: {LOST}")
            self.device = device
            state["built"].append(device)

        def generate(self, samples, streamer, **kwargs):
            if self.device == "NPU" and state["lose_next"]:
                state["lose_next"] = False
                raise RuntimeError(LOST)
            streamer(f"hello from {self.device}")

    genai = ModuleType("openvino_genai")
    genai.WhisperPipeline = Pipeline
    monkeypatch.setitem(sys.modules, "openvino_genai", genai)
    monkeypatch.setattr(devices, "available", lambda: INVENTORY)
    monkeypatch.setattr(config, "FAILED_DEVICES_FILE", tmp_path / "failed-devices.json")
    monkeypatch.setattr(config, "model_dir", lambda kind, multilingual: tmp_path)
    monkeypatch.setattr(WhisperTranscriber, "_check_export", lambda self, kind, model_dir: None)
    return state


def transcribe(t):
    return "".join(t.transcribe_stream(np.zeros(16000, dtype=np.float32)))


def test_a_reset_reloads_on_the_same_device_and_marks_nothing(world):
    t = WhisperTranscriber(device="auto")
    t.load()
    assert transcribe(t) == "hello from NPU"
    assert world["built"] == ["NPU", "NPU"]
    assert failures.load() == {}


def test_a_device_that_does_not_come_back_falls_back_for_this_run(world):
    t = WhisperTranscriber(device="auto")
    t.load()
    world["down"].add("NPU")
    assert transcribe(t) == "hello from CPU"
    # The next request goes straight to the fallback, no NPU retry.
    assert transcribe(t) == "hello from CPU"
    assert world["built"] == ["NPU", "CPU"]
    # A reset says nothing about the NPU working: the next start tries it.
    assert failures.load() == {}


def test_a_device_asked_for_by_name_is_not_swapped(world):
    t = WhisperTranscriber(device="NPU")
    t.load()
    world["down"].add("NPU")
    with pytest.raises(RuntimeError, match="DEVICE_LOST"):
        transcribe(t)
    assert t.device == "NPU"
    assert failures.load() == {}
