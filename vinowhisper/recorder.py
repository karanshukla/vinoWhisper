"""Continuous audio capture via pw-record, held in a rolling buffer.

Two sources: "output" (system audio — whatever's playing, e.g. a video),
"mic" (the physical microphone). Plain `pw-record` with no special
properties auto-connects to the default *source* (mic) regardless of
`--target` — confirmed 2026-08-03 that `--target <sink>.monitor` alone still
gets silently overridden back to the mic by WirePlumber's default policy for
"Capture"-role streams. Tapping a sink's monitor instead requires the
`stream.capture.sink = true` node property (same convention tools like OBS
use for desktop-audio capture), which is what actually made
`pw-link` show `pw-record:input_FL <- effect_input.bass_eq:monitor_FL`
instead of the mic.
"""

import subprocess
import threading

import numpy as np

from . import config

BYTES_PER_SAMPLE = 4  # f32
_MAX_SAMPLES = int(config.WINDOW_S * config.SAMPLE_RATE_HZ)
_READ_CHUNK_SAMPLES = 1600  # 100ms at 16kHz


def _default_sink() -> str:
    result = subprocess.run(
        ["pactl", "get-default-sink"], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


class Recorder:
    """Runs pw-record for the lifetime of the `with` block, continuously
    filling a rolling buffer capped at config.WINDOW_S seconds.
    """

    def __init__(self, source: str = config.DEFAULT_SOURCE) -> None:
        if source not in ("output", "mic"):
            raise ValueError(f"source must be 'output' or 'mic', got {source!r}")
        self.source = source
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._buffer = np.zeros(0, dtype=np.float32)
        self._stop = threading.Event()

    def __enter__(self) -> "Recorder":
        argv = [
            "pw-record",
            "--raw",
            "--rate", str(config.SAMPLE_RATE_HZ),
            "--channels", "1",
            "--format", "f32",
        ]
        if self.source == "output":
            argv += ["--target", _default_sink(), "-P", "{ stream.capture.sink = true }"]
        argv.append("-")

        self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE)
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._proc is not None:
            self._proc.terminate()
            self._proc.wait(timeout=2.0)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        chunk_bytes = _READ_CHUNK_SAMPLES * BYTES_PER_SAMPLE
        while not self._stop.is_set():
            data = self._proc.stdout.read(chunk_bytes)
            if not data:
                break
            samples = np.frombuffer(data, dtype="<f4")
            with self._lock:
                self._buffer = np.concatenate([self._buffer, samples])[-_MAX_SAMPLES:]

    def snapshot(self) -> np.ndarray:
        """Copy of the current rolling window, oldest sample first."""
        with self._lock:
            return self._buffer.copy()
