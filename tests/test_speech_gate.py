"""The caption loop's speech gate: sound the detector hears nobody talking in is
neither decoded nor left in a window for Whisper to turn into words."""

import numpy as np
import pytest

from tests import pcm
from vinowhisper import caption, config, events

CHUNK = 1600


def _gate(script):
    flags = iter(script)
    gate = caption.SpeechGate(lambda chunk: 0.9 if next(flags) else 0.1)
    for _ in script:
        gate.tap(np.zeros(CHUNK, dtype=np.float32))
    return gate


def _window(chunks):
    return pcm.sine(220.0, chunks * CHUNK / config.SAMPLE_RATE_HZ, amplitude=0.1)


def test_a_window_with_no_speech_is_not_decoded():
    assert _gate([False] * 40).apply(_window(40)) is None


def test_a_blip_shorter_than_the_minimum_is_not_speech():
    script = [False] * 20 + [True] * 2 + [False] * 18
    assert config.CAPTION_MIN_SPEECH_S > 2 * CHUNK / config.SAMPLE_RATE_HZ
    assert _gate(script).apply(_window(40)) is None


def test_sound_away_from_speech_is_scaled_to_near_silence_and_speech_is_kept():
    script = [False] * 20 + [True] * 10 + [False] * 10
    window = _window(40)
    gated = _gate(script).apply(window)

    assert gated is not None
    speech = slice(20 * CHUNK, 30 * CHUNK)
    np.testing.assert_array_equal(gated[speech], window[speech])
    assert np.abs(gated[: 10 * CHUNK]).max() < 1e-4
    assert np.abs(gated[-5 * CHUNK :]).max() < 1e-4


def test_the_edges_of_speech_are_padded_so_soft_onsets_survive():
    script = [False] * 20 + [True] * 10 + [False] * 10
    window = _window(40)
    gated = _gate(script).apply(window)

    pad = round(config.TRIM_PAD_S * config.SAMPLE_RATE_HZ / CHUNK)
    before = slice((20 - pad) * CHUNK, 20 * CHUNK)
    np.testing.assert_array_equal(gated[before], window[before])


def test_audio_older_than_the_detectors_history_is_kept():
    gate = _gate([True] * 10)
    window = _window(30)
    gated = gate.apply(window)

    np.testing.assert_array_equal(gated, window)


def test_a_detector_that_fails_turns_the_gate_off_instead_of_stopping_captions():
    def broken(chunk):
        raise RuntimeError("inference failed")

    gate = caption.SpeechGate(broken)
    gate.tap(np.zeros(CHUNK, dtype=np.float32))
    window = _window(10)

    assert not gate.active
    assert gate.apply(window) is window


def test_history_is_bounded_by_the_longest_window():
    gate = _gate([True] * int(config.MAX_WINDOW_S * 10 + 50))
    assert gate._held <= config.MAX_WINDOW_S * config.SAMPLE_RATE_HZ + CHUNK


class _Recorder:
    def __init__(self, tap=None, **_kwargs):
        self.tap = tap
        self.checks = 0

    @property
    def captured_s(self):
        return 5.0 + self.checks

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def check_alive(self):
        self.checks += 1
        if self.checks > 2:
            raise KeyboardInterrupt
        for _ in range(10 if self.tap else 0):
            self.tap(np.zeros(CHUNK, dtype=np.float32))

    def window(self, seconds):
        return pcm.sine(220.0, 1.0, amplitude=0.1)


class _Client:
    heard_language = ""
    decoded = 0

    def __init__(self, **_kwargs):
        pass

    def wait_ready(self):
        return {"device": "NPU", "warnings": ["below NPU"]}

    def transcribe(self, samples):
        _Client.decoded += 1
        return "thank you", 0.1


@pytest.fixture
def loop(monkeypatch):
    monkeypatch.setattr(caption, "Recorder", _Recorder)
    monkeypatch.setattr(caption, "TranscriptionClient", _Client)
    _Client.decoded = 0


def test_mumbling_is_reported_as_no_speech_and_never_reaches_the_server(loop):
    stream = caption.caption_events(
        "output", None, 12.0, gate=lambda warnings: caption.SpeechGate(lambda chunk: 0.2)
    )
    silences = [e for e in stream if isinstance(e, events.Silence)]

    assert _Client.decoded == 0
    assert silences and all(e.no_speech and e.sink_muted is None for e in silences)


def test_speech_is_decoded(loop):
    stream = caption.caption_events(
        "output", None, 12.0, gate=lambda warnings: caption.SpeechGate(lambda chunk: 0.9)
    )
    cycles = [e for e in stream if isinstance(e, events.Cycle)]

    assert _Client.decoded == 2 and len(cycles) == 2


def test_a_detector_that_cannot_load_is_a_warning_on_ready(loop):
    def unavailable(warnings):
        warnings.append("speech detector unavailable")
        return None

    stream = list(caption.caption_events("output", None, 12.0, gate=unavailable))

    assert stream[0].warnings == ["below NPU", "speech detector unavailable"]
    assert _Client.decoded == 2


def test_no_speech_is_not_reported_as_a_dead_capture(capsys):
    renderer = caption.TerminalRenderer()
    renderer.handle(events.Silence(elapsed_s=90.0, rms=0.05, sink_muted=None, no_speech=True))
    assert "no signal" not in capsys.readouterr().err
