import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import numpy as np

from . import config, events

AUDIO_NAME = "audio.wav"
EVENTS_NAME = "events.jsonl"

_PCM_SCALE = 32767.0


class SessionWriter:
    def __init__(self, directory: Path) -> None:
        import wave

        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._wav_file = _open_private(directory / AUDIO_NAME, "wb")
        self._wav = wave.open(self._wav_file, "wb")  # noqa: SIM115
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(config.SAMPLE_RATE_HZ)
        self._events = _open_private(directory / EVENTS_NAME, "w")

    def audio_chunk(self, samples: np.ndarray) -> None:
        pcm = np.clip(samples * _PCM_SCALE, -32768, 32767).astype("<i2")
        self._wav.writeframes(pcm.tobytes())

    def event(self, event: events.Event) -> None:
        self._events.write(json.dumps(events.to_dict(event)) + "\n")
        self._events.flush()

    def close(self) -> None:
        self._wav.close()
        # wave does not close a file object it was handed.
        self._wav_file.close()
        self._events.close()


def _open_private(path: Path, mode: str) -> IO:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    # The mode above only applies on creation; an overwritten recording keeps its old one.
    os.fchmod(fd, 0o600)
    if "b" in mode:
        return os.fdopen(fd, mode)
    return os.fdopen(fd, mode, encoding="utf-8")


def read_events(directory: Path) -> list[dict]:
    path = directory / EVENTS_NAME
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_cycles(directory: Path) -> Iterator[dict]:
    for record in read_events(directory):
        if record["event"] == "Cycle":
            yield record


def read_audio(directory: Path) -> np.ndarray:
    import wave

    with wave.open(str(directory / AUDIO_NAME), "rb") as handle:
        if handle.getframerate() != config.SAMPLE_RATE_HZ:
            raise ValueError(f"expected {config.SAMPLE_RATE_HZ}Hz, got {handle.getframerate()}Hz")
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / _PCM_SCALE
