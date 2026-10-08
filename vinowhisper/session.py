import json
import os
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import IO

import numpy as np

from . import config, events
from .paragraphs import ParagraphBreaker

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
    mode_flag = os.O_APPEND if "a" in mode else os.O_TRUNC
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | mode_flag | os.O_CLOEXEC, 0o600)
    # The mode above only applies on creation; an overwritten recording keeps its old one.
    os.fchmod(fd, 0o600)
    if "b" in mode:
        return os.fdopen(fd, mode)
    return os.fdopen(fd, mode, encoding="utf-8")


_SOURCE_LABELS = {"output": "system audio", "mic": "microphone"}


class TranscriptWriter:
    def __init__(
        self,
        target: Path,
        source: str,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.path: Path | None = None
        self._target = target
        self._source = _SOURCE_LABELS.get(source, source)
        self._clock = clock
        self._now = now
        self._started_at = clock()
        self._started = now()
        self._device = "?"
        self._breaker = ParagraphBreaker()
        self._file: IO | None = None
        self._first = True
        self._closed = False

    def event(self, event: events.Event) -> None:
        if self._closed:
            return
        if isinstance(event, events.Ready):
            self._device = event.device
            self._started_at = self._clock()
            self._started = self._now()
        elif isinstance(event, events.Cycle):
            self._words(event.confirmed)
        elif isinstance(event, events.Silence):
            self._breaker.silence(event.elapsed_s)
        elif isinstance(event, events.Stopped):
            self._words(event.flushed)
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        file, self._file = self._file, None
        if file is not None:
            try:
                file.write("\n")
                file.close()
            except OSError:
                pass

    def _words(self, words: list[str]) -> None:
        if not words:
            return
        file = self._file or self._open()
        for word in words:
            if self._breaker.word(word):
                file.write("\n\n" + self._stamp() + word)
            elif self._first:
                file.write(self._stamp() + word)
            else:
                file.write(" " + word)
            self._first = False
        file.flush()

    def _stamp(self) -> str:
        seconds = int(self._clock() - self._started_at)
        return f"[{seconds // 60:02d}:{seconds % 60:02d}] "

    def _open(self) -> IO:
        target = self._target
        if target.is_dir() or (not target.exists() and not target.suffix):
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = target / self._started.strftime("%Y-%m-%d-%H%M%S.txt")
        else:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = target
        self._file = _open_private(target, "a")
        if os.fstat(self._file.fileno()).st_size:
            self._file.write("\n")
        stamp = f"{self._started:%Y-%m-%d %H:%M}"
        self._file.write(f"vinoWhisper transcript, {stamp}, {self._source}, {self._device}\n\n")
        return self._file


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
