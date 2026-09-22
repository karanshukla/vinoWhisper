import argparse
import json
import queue
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import requests

from . import __version__, audio, config
from .client import TranscriptionClient
from .recorder import CaptureError, Recorder
from .stitch import collapse_repeats, collapse_word_repeats

# A tap on the key, with nothing said, is not worth a decode.
MIN_AUDIO_S = 0.3

# Keys come up on the last syllable, and pw-record delivers in 100ms chunks.
TAIL_S = 0.25
_TAIL_WAIT_S = 1.0

# Whisper's names for silence, emitted as if they were speech.
_NON_SPEECH = re.compile(r"^\s*[\[(][^\])]*[\])]\s*$")

_FULL = "full"
_EOF = "eof"


class Emit(Protocol):
    def __call__(self, record: dict) -> None: ...


class _Recording(Protocol):
    def __enter__(self) -> "_Recording": ...
    def __exit__(self, *exc_info: object) -> None: ...
    def check_alive(self) -> None: ...
    @property
    def captured_s(self) -> float: ...
    def window(self, seconds: float) -> np.ndarray: ...


class _Client(Protocol):
    def wait_ready(self) -> dict: ...
    def transcribe(self, samples: np.ndarray) -> tuple[str, float | None]: ...


def clean(transcript: str) -> str:
    words = collapse_word_repeats(collapse_repeats(transcript).split())
    text = " ".join(words)
    return "" if _NON_SPEECH.match(text) else text


@dataclass
class _Warmup:
    health: dict = field(default_factory=dict)
    error: Exception | None = None
    done: threading.Event = field(default_factory=threading.Event)


class Dictation:
    def __init__(
        self,
        emit: Emit,
        client: _Client | None = None,
        recorder: Callable[[Callable[[np.ndarray], None]], _Recording] | None = None,
    ) -> None:
        self._emit = emit
        self._client = client or TranscriptionClient()
        self._recorder = recorder or (lambda tap: Recorder(source="mic", tap=tap))
        self._commands: queue.Queue[str] = queue.Queue()
        self._active: _Recording | None = None
        self._warmup: _Warmup | None = None
        self._captured = 0
        self._full_sent = False
        self._generation = 0

    def feed(self, lines: Iterable[str]) -> threading.Thread:
        def read() -> None:
            for line in lines:
                self._commands.put(line.strip())
            self._commands.put(_EOF)

        thread = threading.Thread(target=read, name="dictate-stdin", daemon=True)
        thread.start()
        return thread

    def run(self) -> None:
        try:
            while True:
                command = self._commands.get()
                if command == _EOF:
                    return
                self.handle(command)
        finally:
            self._close()

    def handle(self, command: str) -> None:
        if command == "start":
            self._start()
        elif command in ("stop", f"{_FULL} {self._generation}"):
            self._stop(transcribe=True)
        elif command.startswith(_FULL):
            pass
        elif command == "cancel":
            self._stop(transcribe=False)
        elif command:
            self._emit({"event": "Error", "message": f"unknown command {command!r}"})

    def _start(self) -> None:
        if self._active is not None:
            return
        # The model loads on the server's first request; overlap that with the speech.
        self._warmup = self._warm()
        self._generation += 1
        self._captured = 0
        self._full_sent = False
        recording = self._recorder(self._tap)
        try:
            recording.__enter__()
        except CaptureError as exc:
            self._emit({"event": "Error", "message": f"Microphone capture failed: {exc}"})
            return
        self._active = recording
        self._emit({"event": "Listening", "limit_s": config.MAX_WINDOW_S})

    def _warm(self) -> _Warmup:
        warmup = _Warmup()

        def run() -> None:
            try:
                warmup.health = self._client.wait_ready()
            except requests.RequestException as exc:
                warmup.error = exc
            finally:
                warmup.done.set()

        threading.Thread(target=run, name="dictate-warmup", daemon=True).start()
        return warmup

    def _tap(self, samples: np.ndarray) -> None:
        self._captured += samples.size
        self._emit({"event": "Level", "rms": audio.rms(samples)})
        if not self._full_sent and self._captured >= config.MAX_WINDOW_S * config.SAMPLE_RATE_HZ:
            self._full_sent = True
            self._commands.put(f"{_FULL} {self._generation}")

    def _stop(self, transcribe: bool) -> None:
        recording, self._active = self._active, None
        if recording is None:
            return
        try:
            recording.check_alive()
            if transcribe:
                _wait_for_tail(recording)
            samples = recording.window(config.MAX_WINDOW_S)
        except CaptureError as exc:
            recording.__exit__(None, None, None)
            self._emit({"event": "Error", "message": f"Microphone capture failed: {exc}"})
            return
        recording.__exit__(None, None, None)
        if not transcribe:
            self._emit({"event": "Cancelled"})
            return
        self._transcribe(samples)

    def _transcribe(self, samples: np.ndarray) -> None:
        audio_s = samples.size / config.SAMPLE_RATE_HZ
        level = audio.rms(samples)
        if audio_s < MIN_AUDIO_S or level < config.SILENCE_RMS_THRESHOLD:
            self._emit(
                {"event": "Dictated", "text": "", "audio_s": audio_s, "total_s": 0.0, "rms": level}
            )
            return
        self._emit({"event": "Transcribing", "audio_s": audio_s})

        warmup = self._warmup
        if warmup is not None:
            warmup.done.wait()
            if warmup.error is not None:
                self._emit(_server_error(warmup.error))
                return
            if warmup.health:
                self._emit(_ready(warmup.health))
                warmup.health = {}

        normalized, _gain = audio.normalize(samples, config.TARGET_RMS, config.MAX_GAIN)
        started_at = time.monotonic()
        try:
            transcript, _first = self._client.transcribe(normalized)
        except requests.RequestException as exc:
            self._emit(_server_error(exc))
            return
        self._emit(
            {
                "event": "Dictated",
                "text": clean(transcript),
                "audio_s": audio_s,
                "total_s": time.monotonic() - started_at,
                "rms": level,
            }
        )

    def _close(self) -> None:
        if self._active is not None:
            self._active.__exit__(None, None, None)
            self._active = None


def _wait_for_tail(recording: _Recording) -> None:
    wanted = min(recording.captured_s + TAIL_S, config.MAX_WINDOW_S)
    deadline = time.monotonic() + _TAIL_WAIT_S
    while recording.captured_s < wanted and time.monotonic() < deadline:
        time.sleep(0.02)


def _ready(health: dict) -> dict:
    return {
        "event": "Ready",
        "device": str(health.get("device", "?")),
        "degraded": bool(health.get("degraded", False)),
        "warnings": [str(warning) for warning in health.get("warnings", [])],
    }


def _server_error(exc: Exception) -> dict:
    print(f"[vinowhisper] server not reachable at {config.SERVER_URL}: {exc}", file=sys.stderr)
    return {
        "event": "Error",
        "message": f"The transcription server is not reachable at {config.SERVER_URL}. "
        "Run vinowhisper-doctor to see what is missing.",
    }


class _JsonLines:
    def __init__(self) -> None:
        self._stream = sys.stdout
        self._lock = threading.Lock()

    def __call__(self, record: dict) -> None:
        with self._lock:
            self._stream.write(json.dumps(record) + "\n")
            self._stream.flush()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-utterance dictation from the microphone, driven over stdin. "
        "This is what vinowhisper-gui runs for its dictation key; it types nothing itself."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        required=True,
        help="One JSON object per event on stdout. Commands, one per line on stdin: "
        "start, stop (and transcribe), cancel.",
    )
    parser.add_argument("--version", action="version", version=f"vinowhisper {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _parse_args(argv)
    dictation = Dictation(_JsonLines())
    dictation.feed(sys.stdin)
    try:
        dictation.run()
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
