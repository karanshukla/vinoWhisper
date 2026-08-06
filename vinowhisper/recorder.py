"""Continuous audio capture via pw-record, held in a rolling ring buffer.

Two sources: "output" (system audio — whatever's playing, e.g. a video) and
"mic" (the physical microphone).

Tapping system audio needs the `stream.capture.sink = true` node property, not
just `--target`: plain `pw-record` auto-connects to the default *source* (the
mic), and confirmed 2026-08-03 that `--target <sink>.monitor` alone is still
silently overridden back to the mic by WirePlumber's default policy for
"Capture"-role streams. With the property set (same convention OBS uses for
desktop capture), `pw-link` shows `pw-record:input_FL <-
effect_input.bass_eq:monitor_FL` instead of the mic.

**Mute caveat, and why --target takes arbitrary nodes.** A sink's monitor
carries what the sink is playing *after* its own volume and mute are applied
(on this machine, at least — see `monitor.channel-volumes` in the README).
Muting the system therefore silences the monitor, and there is no client-side
fix for that: the samples really are zero. The way out is to capture further
upstream — an individual application's playback stream node also exposes
monitor ports, and those sit before the sink's mute. `--list-targets`
enumerates them; `--target <serial>` taps one.
"""

import json
import subprocess
import threading

import numpy as np

from . import audio, config


class CaptureError(RuntimeError):
    """pw-record failed to start, or died mid-session."""


def _pw(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise CaptureError(f"{argv[0]} not found — install pipewire-utils/pulseaudio-utils") from exc
    except subprocess.CalledProcessError as exc:
        raise CaptureError(f"{' '.join(argv)} failed: {exc.stderr.strip()}") from exc
    return result.stdout


def default_sink() -> str:
    return _pw(["pactl", "get-default-sink"]).strip()


def playback_streams() -> list[dict[str, str]]:
    """Every application currently playing audio, as capture targets.

    These are the nodes to aim at when the system is muted: an app's own
    playback stream is upstream of the sink's mute, so its monitor still
    carries signal when the sink's doesn't.
    """
    dump = json.loads(_pw(["pw-dump"]))
    streams = []
    for obj in dump:
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        serial = props.get("object.serial")
        if serial is None:
            continue
        streams.append(
            {
                "target": str(serial),
                "app": str(props.get("application.name") or props.get("node.name") or "?"),
                "media": str(props.get("media.name") or ""),
            }
        )
    return streams


class Recorder:
    """Runs pw-record for the lifetime of the `with` block, continuously
    filling a ring buffer capped at config.MAX_WINDOW_S seconds.
    """

    # 100ms at 16kHz. Small enough that the buffer is never meaningfully stale,
    # large enough that the read loop isn't syscall-bound.
    _READ_CHUNK_SAMPLES = 1600

    def __init__(self, source: str = config.DEFAULT_SOURCE, target: str | None = None) -> None:
        if source not in ("output", "mic"):
            raise ValueError(f"source must be 'output' or 'mic', got {source!r}")
        self.source = source
        self.target = target
        self._argv: list[str] = []
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._buffer = audio.RingBuffer(int(config.MAX_WINDOW_S * config.SAMPLE_RATE_HZ))
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
            # An explicit --target may be an app's playback stream rather than
            # a sink; stream.capture.sink taps the monitor ports either way.
            argv += ["--target", self.target or default_sink()]
            argv += ["-P", "{ stream.capture.sink = true }"]
        elif self.target:
            argv += ["--target", self.target]
        argv.append("-")

        self._argv = argv
        # stderr deliberately inherited, not piped: pw-record's own errors
        # ("can't connect to target") are the most useful diagnostic there is,
        # and piping them without a drain thread would just deadlock.
        try:
            self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE)
        except FileNotFoundError as exc:
            raise CaptureError("pw-record not found — install pipewire-utils") from exc

        self._thread = threading.Thread(target=self._read_loop, name="pw-record-reader", daemon=True)
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

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
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
            if usable:
                self._buffer.write(np.frombuffer(data, dtype="<f4", count=usable // 4))

    def check_alive(self) -> None:
        """Raise if pw-record has exited.

        Without this the failure is invisible: the read loop just ends, the
        buffer freezes, and the caption loop happily re-transcribes the same
        stale window forever.
        """
        if self._proc is None:
            raise CaptureError("recorder not started — use it as a context manager")
        status = self._proc.poll()
        if status is not None:
            raise CaptureError(
                f"pw-record exited with status {status} (see its output above)\n"
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
