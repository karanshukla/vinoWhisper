"""The fixtures themselves.

A generator that quietly drifts is worse than no generator, because every
threshold assertion built on it drifts with it. So the properties the rest of
the suite relies on are pinned here: the analytic rms of each waveform, that
the noise stream is reproducible, and that the chunker splits at the size the
recorder actually reads.
"""

import numpy as np
import pytest

from tests import pcm
from vinowhisper.audio import rms
from vinowhisper.recorder import Recorder

ROOT_2 = np.sqrt(2.0)
ROOT_3 = np.sqrt(3.0)


def test_a_sine_has_the_length_and_dtype_the_capture_side_produces():
    tone = pcm.sine(440.0, 0.5)
    assert tone.size == pcm.SAMPLE_RATE_HZ // 2
    assert tone.dtype == np.float32


def test_sine_rms_is_amplitude_over_root_two():
    """The property that makes a sine worth using over a constant."""
    tone = pcm.sine(440.0, 1.0, amplitude=0.8)
    assert rms(tone) == pytest.approx(0.8 / ROOT_2, rel=1e-3)


def test_sine_rms_does_not_move_with_frequency_or_phase():
    level = rms(pcm.sine(200.0, 1.0, amplitude=0.5))
    assert rms(pcm.sine(3000.0, 1.0, amplitude=0.5)) == pytest.approx(level, rel=1e-3)
    assert rms(pcm.sine(200.0, 1.0, amplitude=0.5, phase=np.pi / 3)) == pytest.approx(
        level, rel=1e-3
    )


def test_noise_is_bit_identical_for_a_seed():
    """The whole reason this is an LCG and not numpy.random."""
    assert np.array_equal(pcm.noise(0.1, seed=7), pcm.noise(0.1, seed=7))
    assert not np.array_equal(pcm.noise(0.1, seed=7), pcm.noise(0.1, seed=8))


def test_noise_is_reproducible_across_a_process_boundary():
    """Pinned as literal values, so a rewrite of the recurrence has to admit
    it changed the stream rather than staying green against itself.
    """
    assert pcm.noise(4 / pcm.SAMPLE_RATE_HZ, seed=1).tolist() == pytest.approx(
        [-0.52708899974823, -0.26145875453948975, 0.00848400592803955, 0.40976643562316895]
    )


def test_noise_rms_is_amplitude_over_root_three():
    assert rms(pcm.noise(1.0, amplitude=0.6, seed=3)) == pytest.approx(0.6 / ROOT_3, rel=2e-2)


def test_noise_stays_inside_its_amplitude():
    samples = pcm.noise(0.5, amplitude=0.25, seed=11)
    assert np.abs(samples).max() <= 0.25


def test_silence_is_actually_zero():
    assert rms(pcm.silence(0.25)) == 0.0


def test_mix_sums_and_zero_pads_the_short_buffer():
    short = pcm.sine(100.0, 0.1, amplitude=0.5)
    long = pcm.sine(100.0, 0.3, amplitude=0.5)
    mixed = pcm.mix(short, long)
    assert mixed.size == long.size
    assert mixed[: short.size] == pytest.approx(short + long[: short.size], abs=1e-6)
    assert mixed[short.size :] == pytest.approx(long[short.size :], abs=1e-6)


def test_mix_does_not_clip():
    """Deliberate: normalize is what clips, and it is the thing under test."""
    loud = pcm.sine(100.0, 0.05, amplitude=0.9)
    assert np.abs(pcm.mix(loud, loud)).max() > 1.0


def test_mix_of_nothing_is_empty():
    assert pcm.mix().size == 0


def test_delay_by_prepends_silence_and_keeps_the_signal():
    tone = pcm.sine(100.0, 0.1)
    delayed = pcm.delay_by(tone, 0.05)
    pad = pcm.samples_for(0.05)
    assert delayed.size == tone.size + pad
    assert rms(delayed[:pad]) == 0.0
    assert np.array_equal(delayed[pad:], tone)


def test_the_chunker_splits_at_the_recorders_read_size():
    """Bound to the Recorder, not to a copy of 1600."""
    assert pcm.READ_CHUNK_SAMPLES == Recorder._READ_CHUNK_SAMPLES

    tone = pcm.sine(440.0, 0.25)  # 4000 samples: two full chunks and a short one
    parts = list(pcm.chunks(tone))
    assert [part.size for part in parts] == [1600, 1600, 800]
    assert np.array_equal(np.concatenate(parts), tone)


def test_the_chunker_of_an_empty_buffer_yields_nothing():
    assert list(pcm.chunks(pcm.silence(0.0))) == []
