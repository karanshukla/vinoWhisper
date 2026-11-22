"""vinoEcho: the numpy port of LocalVQE's 2.7K filter, and the streaming wrapper around it.
No OpenVINO here: the v1.2 gate is a stub with the same block size."""

from pathlib import Path

import numpy as np
import pytest

from tests import echo_signals, pcm
from vinoecho import CHUNK, Echo
from vinoecho.daf import DAF, M, load_weights

FIXTURE = Path(__file__).parent / "fixtures" / "echo_call.npz"


def _rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


def _db(after, before):
    return 20 * np.log10(_rms(after) / _rms(before))


class _Gate:
    block = CHUNK

    def __init__(self):
        self.calls = 0

    def reset(self):
        self.calls = 0

    def __call__(self, mic, ref):
        self.calls += 1
        return mic - ref


def test_the_port_matches_localvqes_own_output_on_speech():
    saved = np.load(FIXTURE)
    mic = saved["mic"].astype(np.float32) / 32767
    ref = saved["ref"].astype(np.float32) / 32767
    expected = saved["daf"]

    out = DAF(load_weights(), prealign=False).process(mic[: expected.size], ref[: expected.size])

    assert np.corrcoef(out, expected)[0, 1] > 0.99999
    assert np.abs(out - expected).max() < 1e-3


def test_a_late_echo_is_found_by_the_delay_estimate_and_suppressed():
    mic, ref = echo_signals.call(delay_s=0.2, seconds=3.0)
    size = mic.size // M * M
    daf = DAF(load_weights())

    out = daf.process(mic[:size], ref[:size])

    assert 0.2 * 16000 - 2 * M <= daf.cur_shift <= 0.2 * 16000
    assert _db(out[-16000:], mic[-16000:]) < -4.0


def test_the_filter_does_not_touch_a_voice_it_has_no_echo_of():
    voice = pcm.sine(300.0, 1.0, amplitude=0.1)
    silent_far_end = pcm.silence(1.0)

    out = DAF(load_weights()).process(voice, silent_far_end)

    assert abs(_db(out[-8000:], voice[-8000:])) < 0.5


def _split(array, sizes):
    start = 0
    for size in sizes:
        yield array[start : start + size]
        start += size


def test_odd_sized_calls_give_the_same_output_as_one_call_and_hold_the_remainder():
    mic, ref = echo_signals.call(seconds=1.0)
    whole = Echo(_Gate())
    expected_clean, expected_gate = whole.process(mic, ref)

    pieces = Echo(_Gate())
    sizes = [100, 1000, 37, 2000, 5, 4000, 3000]
    clean, gate = [], []
    for m, r in zip(_split(mic, sizes), _split(ref, sizes), strict=True):
        c, g = pieces.process(m, r)
        assert c.size == g.size and c.size % CHUNK == 0
        clean.append(c)
        gate.append(g)

    assert np.array_equal(np.concatenate(clean), expected_clean[: sum(map(len, clean))])
    assert np.array_equal(np.concatenate(gate), expected_gate[: sum(map(len, gate))])
    assert expected_clean.size == mic.size // CHUNK * CHUNK


def test_the_lanes_are_paired_by_arrival_when_one_runs_ahead():
    mic, ref = echo_signals.call(seconds=0.5)
    echo = Echo(_Gate())

    ahead, _ = echo.process(mic[:4096], ref[:0])
    assert ahead.size == 0
    cleaned, gate = echo.process(mic[4096:], ref)
    assert cleaned.size == gate.size == mic.size // CHUNK * CHUNK
    assert np.allclose(gate, mic[: gate.size] - ref[: gate.size])


def test_a_gate_with_the_wrong_block_is_refused():
    gate = _Gate()
    gate.block = 256
    with pytest.raises(ValueError, match="512"):
        Echo(gate)


def test_a_saved_filter_cancels_sooner_than_a_cold_one():
    mic, ref = echo_signals.call(seconds=6.0)
    size = mic.size // CHUNK * CHUNK
    first = Echo(_Gate())
    first.process(mic[: size // 2], ref[: size // 2])
    saved = first.state()

    warm = Echo(_Gate())
    warm.restore(saved)
    cold = Echo(_Gate())
    rest = slice(size // 2, size // 2 + 8000)
    warm_out, _ = warm.process(mic[rest], ref[rest])
    cold_out, _ = cold.process(mic[rest], ref[rest])

    assert _rms(warm_out) < 0.7 * _rms(cold_out)
