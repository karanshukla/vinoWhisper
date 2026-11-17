import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
import requests

from . import audio, cleanup, config, replacements, vocabulary
from .client import TranscriptionClient
from .recorder import CaptureError, Recorder
from .stitch import collapse_repeats, collapse_word_repeats, strip_controls
from .version import VERSION

MIN_AUDIO_S = 0.3

# Keys come up on the last syllable, and pw-record delivers in 100ms chunks.
TAIL_S = 0.25
_TAIL_WAIT_S = 1.0

_NON_SPEECH = re.compile(r"^\s*[\[(][^\])]*[\])]\s*$")

_FULL = "full"
_QUIET = "quiet"
_HANDS_FREE = "hands-free"
_EOF = "eof"

_START = "start"
_STOP = "stop"
_CANCEL = "cancel"
_IGNORE = "ignore"
_UNKNOWN = "unknown"


def command_action(command: str, generation: int) -> str:
    if command == "start":
        return _START
    if command in ("stop", f"{_FULL} {generation}", f"{_QUIET} {generation}"):
        return _STOP
    if command == _HANDS_FREE:
        return _HANDS_FREE
    if not command or command.startswith((_FULL, _QUIET)):
        return _IGNORE
    if command == "cancel":
        return _CANCEL
    return _UNKNOWN


def too_little_to_decode(audio_s: float, level: float) -> bool:
    return audio_s < MIN_AUDIO_S or level < config.SILENCE_RMS_THRESHOLD


def preview_due(since_preview: int, quiet_s: float) -> bool:
    return (
        since_preview >= config.PREVIEW_EVERY_S * config.SAMPLE_RATE_HZ
        and quiet_s < config.PREVIEW_QUIET_S
    )


def worth_previewing(tail: np.ndarray, threshold: float) -> bool:
    return (
        tail.size >= config.PREVIEW_MIN_S * config.SAMPLE_RATE_HZ and audio.rms(tail) >= threshold
    )


class SilenceCount:
    def __init__(self) -> None:
        self.speech = 0
        self.quiet = 0
        self._held: list[tuple[int, float]] = []

    def hold(self, size: int, level: float) -> None:
        self._held.append((size, level))

    def score(self, threshold: float) -> None:
        for size, level in self._held:
            if level >= threshold:
                self.speech += size
                self.quiet = 0
            else:
                self.quiet += size
        self._held.clear()

    @property
    def speech_then_silence(self) -> bool:
        rate = config.SAMPLE_RATE_HZ
        return (
            self.speech >= config.HANDS_FREE_MIN_SPEECH_S * rate
            and self.quiet >= config.HANDS_FREE_SILENCE_S * rate
        )


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


class _Cleaner(Protocol):
    def prepare(self) -> None: ...
    @property
    def active(self) -> bool: ...
    def wants(self, text: str) -> bool: ...
    def __call__(self, text: str) -> cleanup.Outcome: ...
    def take_notice(self) -> str | None: ...


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
    def __init__(self, keep: bool = False) -> None:
        self._chunks: list[tuple[np.ndarray, float]] = []
        self._samples = 0
        self._quiet = 0
        self._total = 0
        self._energy = 0.0
        self._floor = float("inf")
        self._levels: list[float] = []
        self._keep: list[np.ndarray] | None = [] if keep else None

    @property
    def threshold(self) -> float:
        floor = self._floor if self._floor <= config.NOISE_FLOOR_MAX_RMS else 0.0
        return max(config.SILENCE_RMS_THRESHOLD, floor * config.NOISE_FLOOR_MARGIN)

    @property
    def settled(self) -> bool:
        return self._total > config.NOISE_FLOOR_SETTLE_S * config.SAMPLE_RATE_HZ

    @property
    def quiet_s(self) -> float:
        return self._quiet / config.SAMPLE_RATE_HZ

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
        # (pinned by `test_dictate.py::test_a_dropout_does_not_become_the_noise_floor`)
        settling = self._total < config.NOISE_FLOOR_SETTLE_S * config.SAMPLE_RATE_HZ
        if not settling and level >= config.SILENCE_RMS_THRESHOLD / config.NOISE_FLOOR_MARGIN:
            self._floor = min(self._floor, level)
        self._chunks.append((chunk, level))
        self._levels.append(level)
        if self._keep is not None:
            self._keep.append(chunk)
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

    @property
    def speech_level(self) -> float:
        loudest = sorted(self._levels)[-5:]
        return sum(loudest) / len(loudest) if loudest else 0.0

    @property
    def stationary(self) -> bool:
        floor = self._floor
        if (
            floor == float("inf")
            or floor < config.SILENCE_RMS_THRESHOLD / config.NOISE_FLOOR_MARGIN
        ):
            return False
        top = self.speech_level
        return top < config.STATIONARY_MAX_RMS and top < config.STATIONARY_RATIO * floor

    def has_speech(self, chunks: list[tuple[np.ndarray, float]]) -> bool:
        if self.stationary:
            return False
        gate = max(self.threshold, config.SPEECH_FRACTION * self.speech_level)
        loud = sum(1 for _, level in chunks if level >= gate)
        return loud >= config.SPEECH_MIN_CHUNKS

    def note(self) -> str:
        floor = self._floor if self._floor != float("inf") else 0.0
        return (
            f"floor={floor:.4f} speech={self.speech_level:.4f} "
            f"threshold={self.threshold:.4f} stationary={self.stationary}"
        )

    def full(self) -> np.ndarray:
        if not self._keep:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._keep)

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
        if not head or not self.has_speech(head):
            return np.zeros(0, dtype=np.float32)
        return np.concatenate([chunk for chunk, _ in self._trim(head)])

    def _trim(self, head: list[tuple[np.ndarray, float]]) -> list[tuple[np.ndarray, float]]:
        threshold = self.threshold
        loud = [i for i, (_, level) in enumerate(head) if level >= threshold]
        if not loud:
            return head
        pad = int(config.TRIM_PAD_S * config.SAMPLE_RATE_HZ)
        start, end = loud[0], loud[-1]
        kept = 0
        while start > 0 and kept < pad:
            start -= 1
            kept += head[start][0].size
        kept = 0
        while end < len(head) - 1 and kept < pad:
            end += 1
            kept += head[end][0].size
        return head[start : end + 1]


class _Decoder:
    def __init__(
        self,
        client: _Client,
        warmup: _Warmup,
        emit: Emit,
        table: dict[str, str] | None = None,
        terms: list[str] | None = None,
    ) -> None:
        self.texts: list[str] = []
        self.error: Exception | None = None
        self._client = client
        self._warmup = warmup
        self._emit = emit
        self._table = table or {}
        self._terms = terms or []
        self._live = True
        self._gate = threading.Lock()
        self._pending: queue.Queue[tuple[np.ndarray, int | None, int, float] | None] = queue.Queue()
        self._busy = False
        self.behind_job = False
        self.behind_preview = False
        self._previewing = False
        self.last_wait_s = 0.0
        self.last_decode_s = 0.0
        self.preview_end: int | None = None
        self._cancelled = threading.Event()
        self._thread = threading.Thread(target=self._run, name="dictate-decode", daemon=True)
        self._thread.start()

    def spoken(self) -> str:
        return clean(" ".join(self.texts))

    def joined(self) -> str:
        # (pinned by `test_dictate.py::test_a_replacement_phrase_split_across_two_pieces_still_matches`)
        return self.apply_replacements(self.spoken())

    def apply_replacements(self, text: str) -> str:
        return replacements.apply(vocabulary.correct(text, self._terms), self._table)

    def joined_with(self, tail: str) -> str:
        return self.apply_replacements(clean(" ".join([*self.texts, tail])))

    def submit(self, samples: np.ndarray) -> None:
        self.behind_job = self._busy or not self._pending.empty()
        self.behind_preview = self._busy and self._previewing
        self._pending.put((samples, None, 0, time.monotonic()))

    def preview(self, samples: np.ndarray, end: int) -> None:
        # (pinned by `test_dictate.py::test_a_preview_is_not_queued_behind_another_decode`)
        if self._live and self._pending.empty() and not self._busy:
            self._pending.put((samples, len(self.texts), end, time.monotonic()))

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
            samples, tag, end, queued = job
            self._previewing = tag is not None
            self._busy = True
            started = time.monotonic()
            try:
                self._decode(samples, tag, end)
            except Exception as exc:
                # (pinned by `test_dictate.py::test_a_decode_that_raises_reports_an_error_instead_of_typing_part_of_the_text`)
                if tag is None and self.error is None:
                    self.error = exc
            finally:
                self._busy = False
                if tag is None:
                    self.last_wait_s = started - queued
                    self.last_decode_s = time.monotonic() - started

    def _decode(self, samples: np.ndarray, tag: int | None, end: int) -> None:
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
                self.preview_end = None
                if self._live:
                    self._emit({"event": "Partial", "text": self.joined()})
            elif self._live and len(self.texts) == tag:
                self.preview_end = end
                self._emit({"event": "Partial", "text": self.joined_with(text)})


class Dictation:
    def __init__(
        self,
        emit: Emit,
        client: _Client | None = None,
        recorder: Callable[[Callable[[np.ndarray], None]], _Recording] | None = None,
        trace: bool | None = None,
        cleaner: _Cleaner | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._trace_on = (
            os.environ.get("VINOWHISPER_DICTATE_TRACE") == "1" if trace is None else trace
        )
        self._clock = clock
        self._trace: _Trace | None = None
        self._emit = emit
        self._client = client or TranscriptionClient(vocabulary=True)
        self._recorder = recorder or (lambda tap: Recorder(source="mic", tap=tap))
        self._cleaner: _Cleaner = cleaner or _FileCleaner()
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
        self._silence = SilenceCount()
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
        action = command_action(command, self._generation)
        if action == _START:
            self._start()
        elif action == _STOP:
            self._stop(transcribe=True)
        elif action == _HANDS_FREE:
            self._latch_hands_free()
        elif action == _CANCEL:
            self._stop(transcribe=False)
        elif action == _UNKNOWN:
            self._emit({"event": "Error", "message": f"unknown command {command!r}"})

    def _start(self) -> None:
        if self._active is not None:
            return
        warmup = self._warmup = self._warm()
        self._generation += 1
        self._captured = 0
        self._stop_sent = False
        self._trace = _Trace(self._clock()) if self._trace_on else None
        self._hands_free = False
        self._silence = SilenceCount()
        self._since_preview = 0
        self._cleaner.prepare()
        self._segmenter = _Segmenter(keep=bool(os.environ.get("VINOWHISPER_DICTATE_AUDIO_DIR")))
        self._decoder = _Decoder(
            self._client, warmup, self._emit, replacements.load(), vocabulary.load()
        )
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
        if self._hands_free and not self._stop_sent and self._silence.speech_then_silence:
            self._stop_sent = True
            self._commands.put(f"{_QUIET} {self._generation}")

    def _stop_at_the_limit(self) -> None:
        if not self._stop_sent and self._captured >= config.DICTATION_MAX_S * config.SAMPLE_RATE_HZ:
            self._stop_sent = True
            self._commands.put(f"{_FULL} {self._generation}")

    def _preview(self, segmenter: _Segmenter, decoder: _Decoder, size: int) -> None:
        self._since_preview += size
        # (pinned by `test_dictate.py::test_a_pause_is_not_previewed_and_speech_resuming_previews_at_once`)
        if not preview_due(self._since_preview, segmenter.quiet_s):
            return
        self._since_preview = 0
        tail = segmenter.peek()
        if worth_previewing(tail, segmenter.threshold):
            decoder.preview(tail, self._captured)

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
            # (pinned by `test_dictate.py::test_the_opening_is_scored_only_once_the_noise_floor_is_known`)
            self._silence.hold(samples.size, level)
            if segmenter.settled:
                self._silence.score(segmenter.threshold)
            if segment is not None:
                _submit(decoder, segment)
            self._stop_if_quiet()
            self._preview(segmenter, decoder, samples.size)
            self._stop_at_the_limit()

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
        tail_wait_s = self._end_recording(recording, released_at, wait_for_tail=transcribe)
        if tail_wait_s is None:
            return
        if not transcribe:
            self._discard()
            self._emit({"event": "Cancelled"})
            return
        with self._lock:
            if self._decoder is not None:
                self._decoder.mute()
        with self._lock:
            segmenter, decoder = self._segmenter, self._decoder
            self._segmenter = self._decoder = None
        if segmenter is None or decoder is None:
            return
        self._decode_the_rest(segmenter, decoder, released_at, tail_wait_s)

    def _end_recording(
        self, recording: _Recording, released_at: float, wait_for_tail: bool
    ) -> float | None:
        tail_wait_s = 0.0
        try:
            recording.check_alive()
            if wait_for_tail:
                _wait_for_tail(recording)
                tail_wait_s = time.monotonic() - released_at
        except CaptureError as exc:
            recording.__exit__(None, None, None)
            self._discard()
            self._emit({"event": "Error", "message": f"Microphone capture failed: {exc}"})
            return None
        recording.__exit__(None, None, None)
        return tail_wait_s

    def _decode_the_rest(
        self, segmenter: _Segmenter, decoder: _Decoder, released_at: float, tail_wait_s: float
    ) -> None:
        raw = segmenter.peek()
        full = segmenter.full()
        note = segmenter.note()
        final = segmenter.finish()
        _keep_audio(raw, final, full)
        if _cleanup_debug():
            print(
                f"[vinowhisper] gate: {note} final={final.size / config.SAMPLE_RATE_HZ:.2f}s",
                file=sys.stderr,
            )
        stale_samples = (
            None if decoder.preview_end is None else self._captured - decoder.preview_end
        )
        _submit(decoder, final)
        self._finish(segmenter, decoder, released_at)
        if self._trace_on:
            _report_release(decoder, stale_samples, final.size, tail_wait_s, released_at)

    def _finish(self, segmenter: _Segmenter, decoder: _Decoder, released_at: float) -> None:
        audio_s = segmenter.total_s
        level = segmenter.rms
        if too_little_to_decode(audio_s, level):
            decoder.cancel()
            self._emit(
                {"event": "Dictated", "text": "", "audio_s": audio_s, "total_s": 0.0, "rms": level}
            )
            return
        self._emit({"event": "Transcribing", "audio_s": audio_s})

        if not self._server_answered(decoder):
            return
        decoder.finish()
        if decoder.error is not None:
            self._emit(_decode_error(decoder.error))
            return
        text, cleaned = self._tidy(decoder)
        self._emit(
            {
                "event": "Dictated",
                "text": text,
                "cleaned": cleaned,
                "audio_s": audio_s,
                "total_s": time.monotonic() - released_at,
                "rms": level,
            }
        )
        self._pass_on_notice()

    def _server_answered(self, decoder: _Decoder) -> bool:
        warmup = self._warmup
        if warmup is None:
            return True
        warmup.done.wait()
        if warmup.error is not None:
            decoder.cancel()
            self._emit(_server_error(warmup.error))
            return False
        if warmup.health:
            self._emit(_ready(warmup.health))
            warmup.health = {}
        return True

    def _pass_on_notice(self) -> None:
        # After Dictated, so an older tray that skips it still types (pinned by
        # `test_dictate.py::test_a_cleanup_that_turns_itself_off_says_so_after_the_text`).
        notice = self._cleaner.take_notice()
        if notice is not None:
            print(f"[vinowhisper] {notice}", file=sys.stderr)
            self._emit({"event": "Notice", "message": notice})

    def _tidy(self, decoder: _Decoder) -> tuple[str, bool]:
        spoken = decoder.spoken()
        if not spoken or not self._cleaner.active:
            return decoder.apply_replacements(spoken), False
        if not self._cleaner.wants(spoken):
            _log_cleanup_skipped(spoken, decoder.texts)
            return decoder.apply_replacements(spoken), False
        self._emit({"event": "Cleaning"})
        outcome = self._cleaner(spoken)
        _log_cleanup(spoken, decoder.texts, outcome)
        return decoder.apply_replacements(outcome.text), outcome.cleaned

    def _close(self) -> None:
        if self._active is not None:
            self._active.__exit__(None, None, None)
            self._active = None
        self._discard()


def _cleanup_debug() -> bool:
    return os.environ.get("VINOWHISPER_CLEANUP_DEBUG") == "1"


def _log_cleanup_skipped(spoken: str, pieces: list[str]) -> None:
    print(
        f"[vinowhisper] cleanup: skipped, nothing to fix "
        f"(in {len(spoken)} chars, {len(pieces)} pieces)",
        file=sys.stderr,
    )
    if _cleanup_debug():
        print(f"[vinowhisper] cleanup-debug pieces={pieces!r}", file=sys.stderr)


def _log_cleanup(spoken: str, pieces: list[str], outcome: cleanup.Outcome) -> None:
    status = "ok" if outcome.cleaned else f"raw, {outcome.reason}"
    print(
        f"[vinowhisper] cleanup: {status} in {outcome.elapsed_s * 1000:.0f}ms "
        f"(in {len(spoken)} chars, out {len(outcome.text)}, {len(pieces)} pieces)",
        file=sys.stderr,
    )
    if _cleanup_debug():
        print(f"[vinowhisper] cleanup-debug pieces={pieces!r}", file=sys.stderr)
        print(f"[vinowhisper] cleanup-debug in ={spoken!r}", file=sys.stderr)
        print(f"[vinowhisper] cleanup-debug out={outcome.text!r}", file=sys.stderr)
        if outcome.answer is not None:
            print(f"[vinowhisper] cleanup-debug model={outcome.answer!r}", file=sys.stderr)


def _warm_cleanup() -> None:
    from .client import TranscriptionClient

    TranscriptionClient().warm_cleanup()


class _FileCleaner:
    def __init__(
        self,
        load: Callable[[], cleanup.Settings | None] | None = None,
        transport: cleanup.Transport | None = None,
        breaker: cleanup.Breaker | None = None,
        warm: Callable[[], None] | None = None,
    ) -> None:
        self._load = load
        self._transport = transport
        self._warm = warm
        # (pinned by `test_dictate.py::test_a_missing_model_stops_cleanup_until_it_is_downloaded`)
        self._breaker = breaker or cleanup.Breaker()
        self._notice: str | None = None
        self._settings: cleanup.Settings | None = None
        self._ready = threading.Event()
        self._ready.set()

    def prepare(self) -> None:
        self._ready.clear()

        def load() -> None:
            try:
                self._settings = (self._load or cleanup.load_settings)()
            finally:
                self._ready.set()
            settings = self._settings
            if settings is not None and settings.model_ready and self._breaker.allows(settings):
                try:
                    (self._warm or _warm_cleanup)()
                except Exception as exc:
                    print(f"[vinowhisper] cleanup: warm-up failed: {exc}", file=sys.stderr)

        threading.Thread(target=load, name="cleanup-settings", daemon=True).start()

    @property
    def active(self) -> bool:
        self._ready.wait()
        settings = self._settings
        return settings is not None and self._breaker.allows(settings)

    def wants(self, text: str) -> bool:
        self._ready.wait()
        settings = self._settings
        return settings is not None and (settings.mode == "always" or cleanup.needs_cleanup(text))

    def __call__(self, text: str) -> cleanup.Outcome:
        self._ready.wait()
        settings = self._settings
        if settings is None:
            return cleanup.Outcome(text, False, "disabled", 0.0)
        outcome = cleanup.clean_up(text, settings, self._transport or cleanup.local)
        self._notice = self._breaker.record(settings, outcome)
        return outcome

    def take_notice(self) -> str | None:
        notice, self._notice = self._notice, None
        return notice


def _report_release(
    decoder: _Decoder,
    stale_samples: int | None,
    final_size: int,
    tail_wait_s: float,
    released_at: float,
) -> None:
    rate = config.SAMPLE_RATE_HZ
    stale = "none" if stale_samples is None else f"{stale_samples / rate * 1000:.0f}ms"

    print(
        f"[vinowhisper] dictate-release: tail_wait={tail_wait_s * 1000:.0f}ms "
        f"final_audio={final_size / rate:.1f}s behind_job={decoder.behind_job} "
        f"behind_preview={decoder.behind_preview} "
        f"queue_wait={decoder.last_wait_s * 1000:.0f}ms "
        f"decode={decoder.last_decode_s * 1000:.0f}ms preview_stale={stale} "
        f"release_to_text={(time.monotonic() - released_at) * 1000:.0f}ms",
        file=sys.stderr,
    )


_KEPT_PIECES = 10


def _keep_audio(raw: np.ndarray, decoded: np.ndarray, full: np.ndarray) -> None:
    # (pinned by `test_dictate.py::test_kept_audio_is_private_and_only_the_newest_are_kept`)
    target = os.environ.get("VINOWHISPER_DICTATE_AUDIO_DIR")
    if not target:
        return
    directory = Path(target).expanduser()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid():
            print(f"[vinowhisper] not keeping audio in {directory}: not yours", file=sys.stderr)
            return
        directory.chmod(0o700)
        for name, samples in (("raw", raw), ("decoded", decoded), ("full", full)):
            _write_private_wav(directory / f"{stamp}-{name}.wav", samples)
        _drop_all_but_the_newest(directory)
    except OSError as exc:
        print(f"[vinowhisper] could not keep audio: {exc}", file=sys.stderr)


def _write_private_wav(path: Path, samples: np.ndarray) -> None:
    pcm16 = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle, wave.open(handle, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(config.SAMPLE_RATE_HZ)
        out.writeframes(pcm16.tobytes())


def _drop_all_but_the_newest(directory: Path) -> None:
    kept = sorted(directory.glob("*.wav"))
    for old in kept[: max(0, len(kept) - 3 * _KEPT_PIECES)]:
        old.unlink(missing_ok=True)


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


def _decode_error(exc: Exception) -> dict:
    if isinstance(exc, requests.RequestException):
        return _server_error(exc)
    message = f"Transcription failed: {exc}"
    print(f"[vinowhisper] {message}", file=sys.stderr)
    return {"event": "Error", "message": message}


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
        "This is what vinowhisper-tray runs for its dictation key; it types nothing itself."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        required=True,
        help="One JSON object per event on stdout. Commands, one per line on stdin: "
        "start, hands-free (a tap latched it: stop after silence), stop (and transcribe), cancel.",
    )
    parser.add_argument("--version", action="version", version=f"vinowhisper {VERSION}")
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
