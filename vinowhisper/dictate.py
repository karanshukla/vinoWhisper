import argparse
import json
import os
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

from . import __version__, audio, config, replacements
from .client import TranscriptionClient
from .recorder import CaptureError, Recorder
from .stitch import collapse_repeats, collapse_word_repeats, strip_controls

MIN_AUDIO_S = 0.3

# Keys come up on the last syllable, and pw-record delivers in 100ms chunks.
TAIL_S = 0.25
_TAIL_WAIT_S = 1.0

_NON_SPEECH = re.compile(r"^\s*[\[(][^\])]*[\])]\s*$")

_FULL = "full"
_QUIET = "quiet"
_HANDS_FREE = "hands-free"
_EOF = "eof"


class Emit(Protocol):
    def __call__(self, record: dict) -> None: ...


class _Recording(Protocol):
    def __enter__(self) -> "_Recording": ...
    def __exit__(self, *exc_info: object) -> None: ...
    def check_alive(self) -> None: ...
    @property
    def captured_s(self) -> float: ...


@dataclass
class _Trace:
    started: float
    first_s: float | None = None
    loud_s: float | None = None
    first_rms: float = 0.0
    chunks: int = 0
    loud_index: int = 0


class _Client(Protocol):
    def wait_ready(self) -> dict: ...
    def transcribe(self, samples: np.ndarray) -> tuple[str, float | None]: ...


def clean(transcript: str) -> str:
    words = collapse_word_repeats(collapse_repeats(strip_controls(transcript)).split())
    text = " ".join(words)
    return "" if _NON_SPEECH.match(text) else text


@dataclass
class _Warmup:
    health: dict = field(default_factory=dict)
    error: Exception | None = None
    done: threading.Event = field(default_factory=threading.Event)


class _Segmenter:
    def __init__(self) -> None:
        self._chunks: list[tuple[np.ndarray, float]] = []
        self._samples = 0
        self._quiet = 0
        self._total = 0
        self._energy = 0.0
        self._floor = float("inf")

    @property
    def threshold(self) -> float:
        floor = self._floor if self._floor <= config.NOISE_FLOOR_MAX_RMS else 0.0
        return max(config.SILENCE_RMS_THRESHOLD, floor * config.NOISE_FLOOR_MARGIN)

    @property
    def total_s(self) -> float:
        return self._total / config.SAMPLE_RATE_HZ

    @property
    def rms(self) -> float:
        return float(np.sqrt(self._energy / self._total)) if self._total else 0.0

    def add(self, chunk: np.ndarray) -> np.ndarray | None:
        if chunk.size == 0:
            return None
        level = audio.rms(chunk)
        self._floor = min(self._floor, level)
        self._chunks.append((chunk, level))
        self._samples += chunk.size
        self._total += chunk.size
        self._energy += level * level * chunk.size
        self._quiet = self._quiet + chunk.size if level < self.threshold else 0
        rate = config.SAMPLE_RATE_HZ
        if self._samples >= config.SEGMENT_MIN_S * rate and self._quiet >= (
            config.SEGMENT_PAUSE_S * rate
        ):
            return self._cut(len(self._chunks))
        if self._samples >= config.MAX_WINDOW_S * rate:
            return self._cut(self._quietest_in_tail() + 1)
        return None

    def finish(self) -> np.ndarray:
        return self._cut(len(self._chunks))

    def peek(self) -> np.ndarray:
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate([chunk for chunk, _ in self._chunks])

    def _quietest_in_tail(self) -> int:
        wanted = config.SEGMENT_FALLBACK_S * config.SAMPLE_RATE_HZ
        best, seen = len(self._chunks) - 1, 0
        for index in range(len(self._chunks) - 1, -1, -1):
            if self._chunks[index][1] < self._chunks[best][1]:
                best = index
            seen += self._chunks[index][0].size
            if seen >= wanted:
                break
        return best

    def _cut(self, count: int) -> np.ndarray:
        head, self._chunks = self._chunks[:count], self._chunks[count:]
        self._samples = sum(chunk.size for chunk, _ in self._chunks)
        self._quiet = 0
        for chunk, level in reversed(self._chunks):
            if level >= self.threshold:
                break
            self._quiet += chunk.size
        if not head:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate([chunk for chunk, _ in head])


class _Decoder:
    def __init__(
        self,
        client: _Client,
        warmup: _Warmup,
        emit: Emit,
        table: dict[str, str] | None = None,
    ) -> None:
        self.texts: list[str] = []
        self.error: Exception | None = None
        self._client = client
        self._warmup = warmup
        self._emit = emit
        self._table = table or {}
        self._live = True
        self._gate = threading.Lock()
        self._pending: queue.Queue[tuple[np.ndarray, int | None] | None] = queue.Queue()
        self._busy = False
        self._cancelled = threading.Event()
        self._thread = threading.Thread(target=self._run, name="dictate-decode", daemon=True)
        self._thread.start()

    def joined(self) -> str:
        # Replacements run on the joined text, not per piece, so a phrase split
        # across two segments still matches.
        return replacements.apply(clean(" ".join(self.texts)), self._table)

    def joined_with(self, tail: str) -> str:
        return replacements.apply(clean(" ".join([*self.texts, tail])), self._table)

    def submit(self, samples: np.ndarray) -> None:
        self._pending.put((samples, None))

    def preview(self, samples: np.ndarray) -> None:
        # Only when the decoder is idle, so a preview never delays a real piece by more than one.
        if self._live and self._pending.empty() and not self._busy:
            self._pending.put((samples, len(self.texts)))

    def finish(self) -> None:
        self._pending.put(None)
        self._thread.join()

    def cancel(self) -> None:
        with self._gate:
            self._live = False
            self._cancelled.set()
        self._pending.put(None)

    def mute(self) -> None:
        with self._gate:
            self._live = False

    def _run(self) -> None:
        while (job := self._pending.get()) is not None:
            samples, tag = job
            self._busy = True
            try:
                self._decode(samples, tag)
            finally:
                self._busy = False

    def _decode(self, samples: np.ndarray, tag: int | None) -> None:
        if self._cancelled.is_set() or self.error is not None:
            return
        self._warmup.done.wait()
        if self._warmup.error is not None or self._cancelled.is_set():
            return
        normalized, _gain = audio.normalize(samples, config.TARGET_RMS, config.MAX_GAIN)
        try:
            transcript, _first = self._client.transcribe(normalized)
        except requests.RequestException as exc:
            if tag is None:
                self.error = exc
            return
        text = clean(transcript)
        if not text:
            return
        with self._gate:
            if self._cancelled.is_set():
                return
            if tag is None:
                self.texts.append(text)
                if self._live:
                    self._emit({"event": "Partial", "text": self.joined()})
            elif self._live and len(self.texts) == tag:
                self._emit({"event": "Partial", "text": self.joined_with(text)})


class Dictation:
    def __init__(
        self,
        emit: Emit,
        client: _Client | None = None,
        recorder: Callable[[Callable[[np.ndarray], None]], _Recording] | None = None,
        trace: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._trace_on = (
            os.environ.get("VINOWHISPER_DICTATE_TRACE") == "1" if trace is None else trace
        )
        self._clock = clock
        self._trace: _Trace | None = None
        self._emit = emit
        self._client = client or TranscriptionClient()
        self._recorder = recorder or (lambda tap: Recorder(source="mic", tap=tap))
        self._commands: queue.Queue[str] = queue.Queue()
        self._active: _Recording | None = None
        self._warmup: _Warmup | None = None
        self._captured = 0
        self._stop_sent = False
        self._generation = 0
        self._segmenter: _Segmenter | None = None
        self._decoder: _Decoder | None = None
        self._lock = threading.Lock()
        self._hands_free = False
        self._speech = 0
        self._quiet = 0
        self._since_preview = 0

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
        elif command == _HANDS_FREE:
            self._latch_hands_free()
        elif command == f"{_QUIET} {self._generation}":
            self._stop(transcribe=True)
        elif command.startswith((_FULL, _QUIET)):
            pass
        elif command == "cancel":
            self._stop(transcribe=False)
        elif command:
            self._emit({"event": "Error", "message": f"unknown command {command!r}"})

    def _start(self) -> None:
        if self._active is not None:
            return
        # The model loads on the server's first request; overlap that with the speech.
        warmup = self._warmup = self._warm()
        self._generation += 1
        self._captured = 0
        self._stop_sent = False
        self._trace = _Trace(self._clock()) if self._trace_on else None
        self._hands_free = False
        self._speech = 0
        self._quiet = 0
        self._since_preview = 0
        self._segmenter = _Segmenter()
        self._decoder = _Decoder(self._client, warmup, self._emit, replacements.load())
        recording = self._recorder(self._tap)
        try:
            recording.__enter__()
        except CaptureError as exc:
            self._discard()
            self._emit({"event": "Error", "message": f"Microphone capture failed: {exc}"})
            return
        self._active = recording
        self._emit({"event": "Listening", "limit_s": config.DICTATION_MAX_S})

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

    def _latch_hands_free(self) -> None:
        if self._active is not None:
            with self._lock:
                self._hands_free = True
                self._stop_if_quiet()

    def _stop_if_quiet(self) -> None:
        wanted = config.HANDS_FREE_SILENCE_S * config.SAMPLE_RATE_HZ
        heard = config.HANDS_FREE_MIN_SPEECH_S * config.SAMPLE_RATE_HZ
        if (
            self._hands_free
            and not self._stop_sent
            and self._speech >= heard
            and self._quiet >= wanted
        ):
            self._stop_sent = True
            self._commands.put(f"{_QUIET} {self._generation}")

    def _tap(self, samples: np.ndarray) -> None:
        self._note_chunk(samples)
        level = audio.rms(samples)
        self._emit({"event": "Level", "rms": level})
        with self._lock:
            segmenter, decoder = self._segmenter, self._decoder
            if segmenter is None or decoder is None:
                return
            self._captured += samples.size
            segment = segmenter.add(samples)
            if level >= segmenter.threshold:
                self._speech += samples.size
                self._quiet = 0
            else:
                self._quiet += samples.size
            if segment is not None:
                _submit(decoder, segment)
            self._stop_if_quiet()
            self._since_preview += samples.size
            if self._since_preview >= config.PREVIEW_EVERY_S * config.SAMPLE_RATE_HZ:
                self._since_preview = 0
                tail = segmenter.peek()
                if (
                    tail.size >= config.PREVIEW_MIN_S * config.SAMPLE_RATE_HZ
                    and audio.rms(tail) >= segmenter.threshold
                ):
                    decoder.preview(tail)
            if (
                not self._stop_sent
                and self._captured >= config.DICTATION_MAX_S * config.SAMPLE_RATE_HZ
            ):
                self._stop_sent = True
                self._commands.put(f"{_FULL} {self._generation}")

    def _discard(self) -> None:
        with self._lock:
            decoder, self._decoder, self._segmenter = self._decoder, None, None
        if decoder is not None:
            decoder.cancel()

    def _note_chunk(self, samples: np.ndarray) -> None:
        trace = self._trace
        if trace is None:
            return
        elapsed = self._clock() - trace.started
        trace.chunks += 1
        if trace.first_s is None:
            trace.first_s = elapsed
            trace.first_rms = audio.rms(samples)
        if trace.loud_s is None and audio.rms(samples) >= config.SILENCE_RMS_THRESHOLD:
            trace.loud_s = elapsed
            trace.loud_index = trace.chunks

    def _report_trace(self) -> None:
        trace, self._trace = self._trace, None
        if trace is None:
            return
        if trace.first_s is None:
            print("[vinowhisper] dictate-trace: no audio chunk arrived", file=sys.stderr)
            return
        loud = (
            "none"
            if trace.loud_s is None
            else f"{trace.loud_s * 1000:.0f}ms (chunk {trace.loud_index})"
        )
        print(
            f"[vinowhisper] dictate-trace: first_chunk={trace.first_s * 1000:.0f}ms "
            f"first_loud={loud} first_rms={trace.first_rms:.4f} "
            f"threshold={config.SILENCE_RMS_THRESHOLD}",
            file=sys.stderr,
        )

    def _stop(self, transcribe: bool) -> None:
        recording, self._active = self._active, None
        if recording is None:
            return
        self._report_trace()
        released_at = time.monotonic()
        try:
            recording.check_alive()
            if transcribe:
                _wait_for_tail(recording)
        except CaptureError as exc:
            recording.__exit__(None, None, None)
            self._discard()
            self._emit({"event": "Error", "message": f"Microphone capture failed: {exc}"})
            return
        recording.__exit__(None, None, None)
        if transcribe:
            with self._lock:
                if self._decoder is not None:
                    self._decoder.mute()
        if not transcribe:
            self._discard()
            self._emit({"event": "Cancelled"})
            return
        with self._lock:
            segmenter, decoder = self._segmenter, self._decoder
            self._segmenter = self._decoder = None
        if segmenter is None or decoder is None:
            return
        _submit(decoder, segmenter.finish())
        self._finish(segmenter, decoder, released_at)

    def _finish(self, segmenter: _Segmenter, decoder: _Decoder, released_at: float) -> None:
        audio_s = segmenter.total_s
        level = segmenter.rms
        if audio_s < MIN_AUDIO_S or level < config.SILENCE_RMS_THRESHOLD:
            decoder.cancel()
            self._emit(
                {"event": "Dictated", "text": "", "audio_s": audio_s, "total_s": 0.0, "rms": level}
            )
            return
        self._emit({"event": "Transcribing", "audio_s": audio_s})

        warmup = self._warmup
        if warmup is not None:
            warmup.done.wait()
            if warmup.error is not None:
                decoder.cancel()
                self._emit(_server_error(warmup.error))
                return
            if warmup.health:
                self._emit(_ready(warmup.health))
                warmup.health = {}

        decoder.finish()
        if decoder.error is not None:
            self._emit(_server_error(decoder.error))
            return
        self._emit(
            {
                "event": "Dictated",
                "text": decoder.joined(),
                "audio_s": audio_s,
                "total_s": time.monotonic() - released_at,
                "rms": level,
            }
        )

    def _close(self) -> None:
        if self._active is not None:
            self._active.__exit__(None, None, None)
            self._active = None
        self._discard()


def _submit(decoder: _Decoder, segment: np.ndarray) -> None:
    if audio.rms(segment) >= config.SILENCE_RMS_THRESHOLD:
        decoder.submit(segment)


def _wait_for_tail(recording: _Recording) -> None:
    wanted = min(recording.captured_s + TAIL_S, config.DICTATION_MAX_S)
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
    print(
        f"[vinowhisper] server not reachable at {config.server_address()}: {exc}", file=sys.stderr
    )
    return {
        "event": "Error",
        "message": f"The transcription server is not reachable at {config.server_address()}. "
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
        "start, hands-free (a tap latched it: stop after silence), stop (and transcribe), cancel.",
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
