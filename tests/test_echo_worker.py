"""The thread between capture and the caption loop: echo-cancel the mic when the output calls for
it, pass it through otherwise, and carry the filter between sessions."""

import time

import numpy as np

from tests import pcm
from vinowhisper.echo_policy import AUTO, ON, Output
from vinowhisper.echo_worker import EchoWorker, Status, load_state, state_file

SPEAKERS = Output(sink="alsa_output.speakers", port="speaker", port_type="Speaker", api="alsa")
BUDS = Output(sink="bluez_output.buds", api="bluez5")


class _Engine:
    locked = True

    def __init__(self):
        self.resets = 0
        self.restored = None
        self.fail = False

    def reset(self):
        self.resets += 1

    def state(self):
        return {"marker": np.array([7.0])}

    def restore(self, saved):
        self.restored = saved

    def process(self, mic, ref):
        if self.fail:
            raise RuntimeError("boom")
        return mic * 0.5, mic * 0.25


def _worker(tmp_path, mode=AUTO, output=SPEAKERS, engine=None, make=None):
    engine = engine or _Engine()
    heard = {"clean": [], "gate": [], "output": output}
    worker = EchoWorker(
        mode,
        make or (lambda: engine),
        heard["clean"].append,
        heard["gate"].append,
        current_output=lambda: heard["output"],
        state_dir=tmp_path / "state",
    )
    return worker, engine, heard


def _chunk(value=0.2, seconds=0.032):
    return pcm.sine(220.0, seconds, amplitude=value)


def test_on_speakers_the_mic_is_cleaned_and_the_gate_gets_its_own_signal(tmp_path):
    worker, _, heard = _worker(tmp_path)
    worker._redecide()
    mic = _chunk()

    worker._step(mic, mic, worker.status.active)

    assert worker.status == Status(AUTO, True, "on: speakers")
    assert np.allclose(heard["clean"][0], mic * 0.5)
    assert np.allclose(heard["gate"][0], mic * 0.25)


def test_on_bluetooth_the_mic_passes_through_untouched_and_the_reason_says_why(tmp_path):
    worker, engine, heard = _worker(tmp_path, output=BUDS)
    worker._redecide()
    mic = _chunk()

    worker._step(mic, mic, worker.status.active)

    assert worker.status == Status(AUTO, False, "off: Bluetooth")
    assert np.array_equal(heard["clean"][0], mic)
    assert np.array_equal(heard["gate"][0], mic)
    assert engine.resets == 0


def test_moving_to_earphones_and_back_follows_the_output(tmp_path):
    worker, engine, heard = _worker(tmp_path)
    worker._redecide()
    assert worker.status.active

    heard["output"] = BUDS
    worker._redecide()
    assert not worker.status.active

    heard["output"] = SPEAKERS
    worker._redecide()
    assert worker.status.active
    assert engine.resets == 2


def test_the_filter_is_saved_when_the_output_changes_and_loaded_when_it_comes_back(tmp_path):
    worker, engine, heard = _worker(tmp_path)
    worker._redecide()

    heard["output"] = BUDS
    worker._redecide()
    saved = load_state(state_file(SPEAKERS, "mic", tmp_path / "state"))
    assert saved is not None and saved["marker"][0] == 7.0

    heard["output"] = SPEAKERS
    worker._redecide()
    assert engine.restored is not None and engine.restored["marker"][0] == 7.0


def test_a_different_sink_or_port_is_a_different_saved_filter(tmp_path):
    directory = tmp_path / "state"
    other_port = Output(sink=SPEAKERS.sink, port="hdmi")
    names = {state_file(o, "mic", directory).name for o in (SPEAKERS, other_port, BUDS)}
    assert len(names) == 3
    assert state_file(SPEAKERS, "mic", directory) != state_file(SPEAKERS, "other-mic", directory)


def test_an_engine_that_fails_to_load_leaves_captions_uncancelled_and_is_not_retried(tmp_path):
    attempts = []

    def make():
        attempts.append(1)
        raise RuntimeError("no model")

    worker, _, heard = _worker(tmp_path, make=make)
    worker._redecide()
    worker._redecide()
    mic = _chunk()
    worker._step(mic, mic, worker.status.active)

    assert len(attempts) == 1
    assert not worker.status.active and "no model" in worker.status.reason
    assert np.array_equal(heard["clean"][0], mic)


def test_an_engine_that_fails_mid_session_falls_back_to_the_raw_mic(tmp_path):
    worker, engine, heard = _worker(tmp_path, mode=ON)
    worker._redecide()
    engine.fail = True
    mic = _chunk()

    worker._step(mic, mic, True)

    assert not worker.status.active
    assert np.array_equal(heard["clean"][0], mic)


def test_the_thread_carries_fed_audio_to_the_callbacks_and_saves_on_stop(tmp_path):
    worker, _, heard = _worker(tmp_path)
    worker.start()
    mic = _chunk()
    worker.feed_ref(mic)
    worker.feed_mic(mic)
    deadline = time.monotonic() + 3
    while not heard["clean"] and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop()

    assert heard["clean"] and np.allclose(heard["clean"][0], mic * 0.5)
    assert load_state(state_file(SPEAKERS, "mic", tmp_path / "state")) is not None
