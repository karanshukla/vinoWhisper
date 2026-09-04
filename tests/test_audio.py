"""RingBuffer and level helpers.

The ring buffer replaced a `np.concatenate([buf, chunk])[-cap:]` that copied
~1.9MB twice per 100ms read. The interesting cases are all about the wrap:
writing across the end, reading across it, and a chunk larger than the whole
buffer, which must not corrupt the write index.

Two kinds of test live here and they are not interchangeable. The ramp ones
below check index arithmetic, where a recognisable integer sequence is the
clearest possible input. The waveform ones at the bottom check *levels*, and
those need signal: with a constant array, rms and amplitude are the same
number, so `rms(np.full(100, 0.5)) == 0.5` asserts nothing about rms. See
tests/pcm.py.
"""

import numpy as np
import pytest

from tests import pcm
from vinowhisper import config
from vinowhisper.audio import RingBuffer, normalize, rms

ROOT_2 = np.sqrt(2.0)


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


# --- Against signal, at the sizes and in the arrival pattern of a real run ---


def test_the_ring_buffer_survives_the_real_chunk_arrival_pattern():
    """The production case, which the ramp tests above never reach: a
    MAX_WINDOW_S buffer filled 100ms at a time until it has wrapped several
    times, then read for one WINDOW_S window.

    Both 2026-08-06 buffer bugs (the memcpy rewrite and the busy-spin) were
    about how chunks arrive rather than about sample values, so the chunk size
    here is read off the Recorder rather than chosen.
    """
    capacity = int(config.MAX_WINDOW_S * config.SAMPLE_RATE_HZ)
    buffer = RingBuffer(capacity)
    source = pcm.sine(440.0, config.MAX_WINDOW_S * 3)  # wraps twice over

    written = 0
    for chunk in pcm.chunks(source):
        buffer.write(chunk)
        written += chunk.size
    assert written == source.size
    assert buffer.total_written == source.size

    window = buffer.read_last(int(config.WINDOW_S * config.SAMPLE_RATE_HZ))
    assert np.array_equal(window, source[-window.size :])


def test_a_full_capacity_read_after_wrapping_is_still_in_order():
    capacity = pcm.samples_for(1.0)
    buffer = RingBuffer(capacity)
    # 2.5 wraps, and 1600 does not divide the capacity, so the chunk boundary
    # and the wrap point never line up — which is the case that actually
    # exercises the split write.
    source = pcm.sine(300.0, 2.5)
    for chunk in pcm.chunks(source):
        buffer.write(chunk)

    held = buffer.read_last(capacity)
    assert held.size == capacity
    assert np.array_equal(held, source[-capacity:])
    # A wrapped read is a fresh copy, never a view into the live buffer.
    buffer.write(pcm.silence(0.1))
    assert np.array_equal(held, source[-capacity:])


def test_rms_of_a_sine_is_amplitude_over_root_two_not_amplitude():
    tone = pcm.sine(440.0, 1.0, amplitude=0.5)
    assert rms(tone) == pytest.approx(0.5 / ROOT_2, rel=1e-3)
    assert rms(tone) != pytest.approx(0.5, rel=1e-2)


def test_the_noise_floor_of_a_monitor_reads_below_the_silence_gate():
    """config.SILENCE_RMS_THRESHOLD is set against a measured ~0.0-0.004 of
    dither and EQ-chain noise on an idle sink monitor. Uniform noise at
    amplitude a has rms a/sqrt(3), which puts this in that band.
    """
    assert rms(pcm.noise(1.0, amplitude=0.002, seed=5)) < config.SILENCE_RMS_THRESHOLD


def test_quiet_web_video_is_not_eaten_by_the_silence_gate():
    """0.014 rms is the level config's TARGET_RMS comment cites for ordinary
    web video. The gate is tuned low precisely so this still gets transcribed;
    the cost of it being too high is dropping real quiet speech.
    """
    quiet = pcm.sine(300.0, 1.0, amplitude=0.014 * ROOT_2)
    assert rms(quiet) == pytest.approx(0.014, rel=1e-2)
    assert rms(quiet) > config.SILENCE_RMS_THRESHOLD


def test_normalize_lifts_quiet_web_video_to_the_target_level():
    """And the gain it reports is the one shown on every Cycle event."""
    quiet = pcm.sine(300.0, 1.0, amplitude=0.014 * ROOT_2)
    boosted, gain = normalize(quiet, config.TARGET_RMS, config.MAX_GAIN)
    assert rms(boosted) == pytest.approx(config.TARGET_RMS, rel=1e-2)
    assert gain == pytest.approx(config.TARGET_RMS / 0.014, rel=1e-2)
    assert 1.0 < gain < config.MAX_GAIN


def test_normalize_clips_a_transient_rather_than_refusing_the_boost():
    """A sine alone can never clip here — normalized to TARGET_RMS its peak is
    0.07 — so the clip path only shows up on a signal with a real crest
    factor. Quiet speech with a click over it is exactly that, and the policy
    is to keep the boost and let the click clip, since Whisper needs the quiet
    part audible and does not need the click at all.
    """
    speech = pcm.sine(300.0, 1.0, amplitude=0.005)
    click = pcm.delay_by(pcm.sine(4000.0, 0.001, amplitude=0.9), 0.5)
    boosted, gain = normalize(pcm.mix(speech, click), config.TARGET_RMS, config.MAX_GAIN)

    assert gain > 1.0
    assert boosted.dtype == np.float32
    # Clipped, so the peak is exactly the rail rather than 0.9 * gain.
    assert np.abs(boosted).max() == pytest.approx(1.0)
    # ...and everywhere it did not clip, the gain was simply applied.
    quiet = slice(0, pcm.samples_for(0.4))
    assert boosted[quiet] == pytest.approx(speech[quiet] * gain, abs=1e-6)


def test_a_transient_costs_the_quiet_speech_around_it_some_boost():
    """Worth pinning because it is the surprising half of the above: gain is
    chosen from the *window's* rms, so one loud click raises that rms and the
    speech under it lands short of TARGET_RMS. Whisper still sees a boost,
    just a smaller one, and there is nowhere better to make this trade
    without per-segment gain the loop does not have.
    """
    speech = pcm.sine(300.0, 1.0, amplitude=0.005)
    click = pcm.delay_by(pcm.sine(4000.0, 0.001, amplitude=0.9), 0.5)

    _, alone = normalize(speech, config.TARGET_RMS, config.MAX_GAIN)
    boosted, with_click = normalize(pcm.mix(speech, click), config.TARGET_RMS, config.MAX_GAIN)

    assert with_click < alone
    assert rms(boosted[: pcm.samples_for(0.4)]) < config.TARGET_RMS
