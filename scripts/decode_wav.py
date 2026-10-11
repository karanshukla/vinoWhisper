"""Decode a kept dictation piece the ways dictation can, and show what each says.

Dictation keeps its pieces when VINOWHISPER_DICTATE_AUDIO_DIR is set. Give this the
`*-raw.wav` from a bad dictation: it decodes the audio untrimmed and as the segmenter
would hand it over, so the cause of invented words is a diff, not a guess.

    uv run python scripts/decode_wav.py ~/.cache/claude-scratch/vino/audio/*-raw.wav
"""

import sys
import wave
from pathlib import Path

import numpy as np

from vinowhisper import audio, config, dictate
from vinowhisper.client import TranscriptionClient


def load(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as source:
        raw = source.readframes(source.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def trimmed(clip: np.ndarray) -> np.ndarray:
    segmenter = dictate._Segmenter()
    for start in range(0, clip.size, 1600):
        segmenter.add(clip[start : start + 1600])
    return segmenter.finish()


def main() -> int:
    client = TranscriptionClient()
    client.wait_ready()
    for name in sys.argv[1:]:
        clip = load(Path(name))
        print(f"\n{name}: {clip.size / config.SAMPLE_RATE_HZ:.2f}s, rms {audio.rms(clip):.4f}")
        for label, samples in (("untrimmed", clip), ("trimmed", trimmed(clip))):
            normalized, gain = audio.normalize(samples, config.TARGET_RMS, config.MAX_GAIN)
            text, _ = client.transcribe(normalized)
            seconds = samples.size / config.SAMPLE_RATE_HZ
            print(f"  {label:10} {seconds:5.2f}s gain x{gain:4.1f}: {dictate.clean(text)!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
