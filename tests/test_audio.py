"""RingBuffer and level helpers.

The ring buffer replaced a `np.concatenate([buf, chunk])[-cap:]` that copied
~1.9MB twice per 100ms read. The interesting cases are all about the wrap:
writing across the end, reading across it, and a chunk larger than the whole
buffer, which must not corrupt the write index.
"""

import numpy as np
import pytest

from vinowhisper.audio import RingBuffer, normalize, rms


def ramp(count: int, start: int = 0) -> np.ndarray:
    return np.arange(start, start + count, dtype=np.float32)


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        RingBuffer(0)


def test_reads_back_what_was_written():
    buffer = RingBuffer(10)
    buffer.write(ramp(4))
    assert list(buffer.read_last(4)) == [0, 1, 2, 3]
    assert buffer.total_written == 4


def test_read_more_than_written_returns_what_there_is():
    buffer = RingBuffer(10)
    buffer.write(ramp(3))
    assert list(buffer.read_last(100)) == [0, 1, 2]
    assert buffer.read_last(0).size == 0


def test_keeps_the_newest_samples_when_it_wraps():
    buffer = RingBuffer(5)
    buffer.write(ramp(8))  # 0..7, so 3..7 survive
    assert list(buffer.read_last(5)) == [3, 4, 5, 6, 7]
    # total_written is monotonic — the caption loop measures new audio with it.
    assert buffer.total_written == 8


def test_read_spanning_the_wrap_point_stays_in_order():
    buffer = RingBuffer(5)
    buffer.write(ramp(3))
    buffer.write(ramp(4, start=3))  # writes 3,4,5,6 across the boundary
    assert list(buffer.read_last(5)) == [2, 3, 4, 5, 6]


def test_chunk_larger_than_capacity_keeps_the_tail_and_the_count():
    buffer = RingBuffer(4)
    buffer.write(ramp(10))
    assert list(buffer.read_last(4)) == [6, 7, 8, 9]
    assert buffer.total_written == 10
    # The write index must still be consistent afterwards.
    buffer.write(ramp(2, start=10))
    assert list(buffer.read_last(4)) == [8, 9, 10, 11]


def test_empty_write_is_a_noop():
    buffer = RingBuffer(4)
    buffer.write(np.zeros(0, dtype=np.float32))
    assert buffer.total_written == 0


def test_rms_of_silence_and_of_a_constant():
    assert rms(np.zeros(0, dtype=np.float32)) == 0.0
    assert rms(np.zeros(100, dtype=np.float32)) == 0.0
    assert rms(np.full(100, 0.5, dtype=np.float32)) == pytest.approx(0.5)


def test_normalize_boosts_quiet_audio_towards_the_target():
    quiet = np.full(100, 0.01, dtype=np.float32)
    boosted, gain = normalize(quiet, target_rms=0.05, max_gain=20.0)
    assert gain == pytest.approx(5.0)
    assert rms(boosted) == pytest.approx(0.05, rel=1e-3)


def test_normalize_never_attenuates():
    loud = np.full(100, 0.5, dtype=np.float32)
    same, gain = normalize(loud, target_rms=0.05, max_gain=20.0)
    assert gain == 1.0
    assert np.array_equal(same, loud)


def test_normalize_respects_max_gain_and_clips():
    very_quiet = np.full(100, 0.001, dtype=np.float32)
    boosted, gain = normalize(very_quiet, target_rms=0.05, max_gain=20.0)
    assert gain == 20.0
    assert boosted.dtype == np.float32
    assert boosted.max() <= 1.0


def test_normalize_leaves_digital_silence_alone():
    silence = np.zeros(100, dtype=np.float32)
    same, gain = normalize(silence, target_rms=0.05, max_gain=20.0)
    assert gain == 1.0
    assert np.array_equal(same, silence)
