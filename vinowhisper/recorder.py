"""Continuous audio capture held in a rolling ring buffer.

Two sources: "output" (system audio — whatever's playing, e.g. a video) and
"mic" (the physical microphone). Which binary does the capturing, and how it
has to be asked, lives in capture.py; this file is only the subprocess and the
buffer it feeds.

**Why --target takes arbitrary nodes.** Originally to route around the sink's
mute. That turned out to be a misdiagnosis: measured 2026-08-07, this sink's
monitor is both pre-volume and pre-mute, holding 0.98x of the playing app's
level while the system is muted (see the table in docs/audio.md). The sink
monitor is the right default and needs no rescuing.

`--target` still earns its place on PipeWire, because an individual
application's playback stream node also exposes monitor ports, so it can
isolate one app out of several. What it cannot do is recover audio an app has
muted at its own volume control: the app writes silence into its own stream,
and there is no tap upstream of that. `--list-targets` enumerates what is
available on the current backend.
"""

import subprocess
import threading
from collections.abc import Callable
from typing import IO

import numpy as np

from . import audio, capture, config
from .capture import (  # noqa: F401 - re-exported; these were recorder's API first
    CaptureError,
    default_sink,
    monitor_channel_volumes,
    playback_streams,
    sink_muted,
)


class Recorder:
    """Runs the capture tool for the lifetime of the `with` block, continuously
    filling a ring buffer capped at config.MAX_WINDOW_S seconds.
    """

    # 100ms at 16kHz. Small enough that the buffer is never meaningfully stale,
    # large enough that the read loop isn't syscall-bound.
    _READ_CHUNK_SAMPLES = 1600

    def __init__(
        self,
        source: str = config.DEFAULT_SOURCE,
        target: str | None = None,
        tap: Callable[[np.ndarray], None] | None = None,
    ) -> None:
        if source not in ("output", "mic"):
            raise ValueError(f"source must be 'output' or 'mic', got {source!r}")
        self.source = source
        self.target = target
        # Called with every chunk as it arrives, for --record. Runs on the
        # reader thread, so it has to stay cheap.
        self._tap = tap
        self._argv: list[str] = []
        self._backend: capture.Backend | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._buffer = audio.RingBuffer(int(config.MAX_WINDOW_S * config.SAMPLE_RATE_HZ))
        self._stop = threading.Event()

    @property
    def backend_name(self) -> str:
        return self._backend.name if self._backend else "?"

    def __enter__(self) -> "Recorder":
        self._backend = capture.backend()
        if self.target and self.source == "output" and not self._backend.supports_app_capture:
            # Better here than as silence twenty seconds into a session: on the
            # PulseAudio backend a --target that names an application means
            # nothing, and the value is used as a source name instead.
            raise CaptureError(
                f"--target on the {self._backend.name} backend must name a monitor source, "
                "not an application (PulseAudio cannot tap a single sink-input). "
                "Run --list-targets to see what is available here."
            )

        self._argv = capture.record_argv(self.source, self.target, self._backend)
        # stderr deliberately inherited, not piped: the capture tool's own
        # errors ("can't connect to target") are the most useful diagnostic
        # there is, and piping them without a drain thread would just deadlock.
        try:
            self._proc = subprocess.Popen(self._argv, stdout=subprocess.PIPE)
        except FileNotFoundError as exc:
            raise CaptureError(f"{self._argv[0]} not found — run vinowhisper-setup") from exc

        self._thread = threading.Thread(
            target=self._read_loop, args=(self._proc.stdout,), name="capture-reader", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        proc, thread = self._proc, self._thread
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        if thread is not None:
            thread.join(timeout=2.0)
        # After the join, so the reader is never mid-read on a closed pipe.
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()

    def _read_loop(self, stdout: IO[bytes]) -> None:
        # The pipe is passed in rather than read off self, so this needs no
        # assertion about _proc being non-None to satisfy a type checker — and
        # an assertion is not a thing to rely on, since -O strips it.
        chunk_bytes = self._READ_CHUNK_SAMPLES * audio.BYTES_PER_SAMPLE
        while not self._stop.is_set():
            try:
                data = stdout.read(chunk_bytes)
            except (ValueError, OSError):
                break  # pipe closed underneath us during shutdown
            if not data:
                break
            # A short final read at EOF need not land on a sample boundary.
            usable = len(data) - (len(data) % audio.BYTES_PER_SAMPLE)
            if not usable:
                continue
            samples = np.frombuffer(data, dtype="<f4", count=usable // 4)
            self._buffer.write(samples)
            if self._tap is not None:
                self._tap(samples)

    def check_alive(self) -> None:
        """Raise if the capture process has exited.

        Without this the failure is invisible: the read loop just ends, the
        buffer freezes, and the caption loop happily re-transcribes the same
        stale window forever.
        """
        if self._proc is None:
            raise CaptureError("recorder not started — use it as a context manager")
        status = self._proc.poll()
        if status is not None:
            raise CaptureError(
                f"{self._argv[0]} exited with status {status} (see its output above)\n"
                f"  argv: {' '.join(self._argv)}"
            )

    @property
    def captured_s(self) -> float:
        """Seconds of audio captured since start. Monotonic, not the buffer's
        length — the caller uses it to measure how much is genuinely new.
        """
        return self._buffer.total_written / config.SAMPLE_RATE_HZ

    def window(self, seconds: float) -> np.ndarray:
        """Copy of the most recent `seconds` of audio, oldest sample first."""
        return self._buffer.read_last(int(seconds * config.SAMPLE_RATE_HZ))
