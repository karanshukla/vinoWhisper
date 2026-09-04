"""Deterministic PCM generators, so the audio layer can be tested with signal.

Not collected by pytest (python_files = test_*.py); imported by the tests that
need a waveform rather than a hand-built array.

The audio layer is the part of this project with actual thresholds in it —
SILENCE_RMS_THRESHOLD, TARGET_RMS, MAX_GAIN — and thresholds can only be
tested against something that has a level. Testing `rms` with
`np.full(100, 0.5)` asserts that the mean of a constant is that constant; it
says nothing about a signal, because for a constant `rms` and amplitude are
the same number. A sine separates them (rms = amplitude / sqrt(2)), which is
what makes the assertion mean anything.

**Noise comes from an explicit LCG, not from numpy.random.** np.random's
stream is a documented-but-versioned implementation detail, so pinning a seed
pins a numpy version rather than a waveform. The recurrence below is four
lines and is bit-identical on every platform and every numpy release, which is
the property a fixture needs. Constants are the Numerical Recipes ones.

Two things this file deliberately does not do:

- **No clipping in `mix`.** Summing two loud buffers past ±1 is something a
  test should be able to see, not something a fixture should quietly fix.
  `audio.normalize` is the thing that clips, and it is under test.
- **No sample-rate guessing.** Everything defaults to config.SAMPLE_RATE_HZ,
  because that is the only rate the capture side ever produces.
"""

from collections.abc import Iterator

import numpy as np

from vinowhisper import config
from vinowhisper.recorder import Recorder

SAMPLE_RATE_HZ = config.SAMPLE_RATE_HZ

# Read straight off the Recorder rather than repeating 1600 here. The two bugs
# this chunker exists to reproduce (the ring buffer's memcpy rewrite and the
# busy-spin on an empty buffer, both 2026-08-06) were about chunk *arrival
# pattern*, so a fixture that chunks at some other size is testing a program
# that does not exist.
READ_CHUNK_SAMPLES = Recorder._READ_CHUNK_SAMPLES

# Numerical Recipes' LCG. See the module docstring for why this is hand-rolled.
_LCG_A = 1664525
_LCG_C = 1013904223
_LCG_MODULUS = 1 << 32


def samples_for(seconds: float, sample_rate_hz: int = SAMPLE_RATE_HZ) -> int:
    return int(round(seconds * sample_rate_hz))


def sine(
    freq_hz: float,
    seconds: float,
    amplitude: float = 1.0,
    phase: float = 0.0,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
) -> np.ndarray:
    """A pure tone. rms is amplitude / sqrt(2), independent of frequency."""
    t = np.arange(samples_for(seconds, sample_rate_hz), dtype=np.float64) / sample_rate_hz
    return (amplitude * np.sin(2.0 * np.pi * freq_hz * t + phase)).astype(np.float32)


def silence(seconds: float, sample_rate_hz: int = SAMPLE_RATE_HZ) -> np.ndarray:
    """True digital silence. Note that a real sink monitor never reads this —
    it sits at ~0.0-0.004 from dither and the EQ chain, which is `noise`.
    """
    return np.zeros(samples_for(seconds, sample_rate_hz), dtype=np.float32)


def noise(
    seconds: float,
    amplitude: float = 1.0,
    seed: int = 1,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
) -> np.ndarray:
    """Uniform noise in [-amplitude, amplitude). rms is amplitude / sqrt(3).

    Drawn from the top 24 bits of the LCG word: an LCG's low-order bits have
    very short periods (bit 0 alternates), and taking the whole word would put
    that structure straight into the waveform.
    """
    count = samples_for(seconds, sample_rate_hz)
    out = np.empty(count, dtype=np.float32)
    state = seed % _LCG_MODULUS
    for i in range(count):
        state = (_LCG_A * state + _LCG_C) % _LCG_MODULUS
        out[i] = (state >> 8) / float(1 << 23) - 1.0
    out *= np.float32(amplitude)
    return out


def mix(*buffers: np.ndarray) -> np.ndarray:
    """Sum, zero-padding the short ones. Deliberately does not clip."""
    if not buffers:
        return np.zeros(0, dtype=np.float32)
    out = np.zeros(max(buffer.size for buffer in buffers), dtype=np.float32)
    for buffer in buffers:
        out[: buffer.size] += buffer
    return out


def delay_by(
    buffer: np.ndarray, seconds: float, sample_rate_hz: int = SAMPLE_RATE_HZ
) -> np.ndarray:
    """`buffer`, pushed back by `seconds` of leading silence."""
    pad = np.zeros(samples_for(seconds, sample_rate_hz), dtype=np.float32)
    return np.concatenate([pad, buffer])


def chunks(buffer: np.ndarray, chunk_samples: int = READ_CHUNK_SAMPLES) -> Iterator[np.ndarray]:
    """`buffer` as the reader thread would deliver it: 100ms at a time, with a
    short final chunk, exactly like the recorder's last read before EOF.
    """
    for start in range(0, buffer.size, chunk_samples):
        yield buffer[start : start + chunk_samples]
