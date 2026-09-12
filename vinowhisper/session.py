import json
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from . import config, events

AUDIO_NAME = "audio.wav"
EVENTS_NAME = "events.jsonl"

_PCM_SCALE = 32767.0


class SessionWriter:
    def __init__(self, directory: Path) -> None:
        import wave

        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(directory / AUDIO_NAME), "wb")  # noqa: SIM115
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(config.SAMPLE_RATE_HZ)
        self._events = (directory / EVENTS_NAME).open("w", encoding="utf-8")

    def audio_chunk(self, samples: np.ndarray) -> None:
        pcm = np.clip(samples * _PCM_SCALE, -32768, 32767).astype("<i2")
        self._wav.writeframes(pcm.tobytes())

    def event(self, event: events.Event) -> None:
        self._events.write(json.dumps(events.to_dict(event)) + "\n")
        self._events.flush()

    def close(self) -> None:
        self._wav.close()
        self._events.close()


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
