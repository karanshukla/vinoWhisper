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
            raise CaptureError(
                f"--target on the {self._backend.name} backend must name a monitor source, "
                "not an application (PulseAudio cannot tap a single sink-input). "
                "Run --list-targets to see what is available here."
            )

        self._argv = capture.record_argv(self.source, self.target, self._backend)
        try:
            # stderr inherited, not piped: it is the best diagnostic, and a pipe would need a drain thread.
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
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()

    def _read_loop(self, stdout: IO[bytes]) -> None:
        chunk_bytes = self._READ_CHUNK_SAMPLES * audio.BYTES_PER_SAMPLE
        while not self._stop.is_set():
            try:
                data = stdout.read(chunk_bytes)
            except (ValueError, OSError):
                break
            if not data:
                break
            usable = len(data) - (len(data) % audio.BYTES_PER_SAMPLE)
            if not usable:
                continue
            samples = np.frombuffer(data, dtype="<f4", count=usable // 4)
            self._buffer.write(samples)
            if self._tap is not None:
                self._tap(samples)

    def check_alive(self) -> None:
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
        return self._buffer.total_written / config.SAMPLE_RATE_HZ

    def window(self, seconds: float) -> np.ndarray:
        return self._buffer.read_last(int(seconds * config.SAMPLE_RATE_HZ))
