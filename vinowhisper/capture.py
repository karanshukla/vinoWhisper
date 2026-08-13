"""Talking to whatever audio server this machine actually runs.

Split out of recorder.py when the tool stopped being Fedora/KDE-only. The
subprocess plumbing that feeds the ring buffer is one concern; *which* binary to
plumb, and what to ask it for, is another, and only the second one differs
between distros.

Two backends:

- **PipeWire** (`pw-record`, `pw-dump`). Preferred where it exists, because it
  is the only one that can tap an individual application's playback stream,
  which is what `--target` is for.
- **PulseAudio** (`parec`, `pactl`). The fallback for the distros and setups
  that never moved to PipeWire. Everything works except per-application
  capture: PulseAudio has no equivalent of monitoring one sink-input without
  loading a loopback module, so `--target` there takes a monitor *source*.

Both emit the same thing — raw little-endian float32, 16kHz, mono, on stdout —
so the Recorder above them does not care which one it got.

**pactl is not a PulseAudio-only tool.** `pipewire-pulse` provides it too, and
on a PipeWire system it is usually the shortest path to the default sink and
the mute state. So the control-plane helpers here prefer `pactl` when present
regardless of backend, and fall back to parsing `pw-dump` when it isn't —
which is what a PipeWire install without the Pulse compatibility layer looks
like.

Tapping system audio through pw-record needs the `stream.capture.sink = true`
node property, not just `--target`: plain `pw-record` auto-connects to the
default *source* (the mic), and confirmed 2026-08-03 that `--target
<sink>.monitor` alone is silently overridden back to the mic by WirePlumber's
default policy for "Capture"-role streams.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from . import config, distro

PIPEWIRE = "pipewire"
PULSEAUDIO = "pulseaudio"

# Escape hatch for a machine running both stacks where the auto-pick is wrong.
BACKEND_ENV = "VINOWHISPER_CAPTURE_BACKEND"


class CaptureError(RuntimeError):
    """A capture tool is missing, failed to start, or died mid-session."""


@dataclass(frozen=True)
class Backend:
    name: str
    record: str  # the binary that writes raw PCM to stdout
    capability: str  # what distro.py calls the package providing it

    @property
    def supports_app_capture(self) -> bool:
        """Whether --target can name an individual application's stream."""
        return self.name == PIPEWIRE


_PIPEWIRE_BACKEND = Backend(name=PIPEWIRE, record="pw-record", capability=distro.AUDIO_PIPEWIRE)
_PULSE_BACKEND = Backend(name=PULSEAUDIO, record="parec", capability=distro.AUDIO_PULSE)


def _which(name: str) -> str | None:
    return shutil.which(name)


def available_backends() -> list[Backend]:
    return [
        backend
        for backend in (_PIPEWIRE_BACKEND, _PULSE_BACKEND)
        if _which(backend.record) is not None
    ]


def backend() -> Backend:
    """The capture backend to use, or a CaptureError naming the fix.

    Not cached: the whole point of the setup wizard is that the answer changes
    after you install something, within one process.
    """
    forced = os.environ.get(BACKEND_ENV, "").strip().lower()
    if forced:
        for candidate in (_PIPEWIRE_BACKEND, _PULSE_BACKEND):
            if candidate.name == forced:
                if _which(candidate.record) is None:
                    raise CaptureError(
                        f"{BACKEND_ENV}={forced} but {candidate.record} is not on PATH"
                    )
                return candidate
        raise CaptureError(f"{BACKEND_ENV}={forced!r} is not one of: {PIPEWIRE}, {PULSEAUDIO}")

    backends = available_backends()
    if backends:
        return backends[0]

    info = distro.detect()
    lines = [
        "No audio capture tool found: need either pw-record (PipeWire) or parec (PulseAudio).",
        *distro.remediation(distro.AUDIO_PIPEWIRE, info).lines(),
        "  or, for the PulseAudio path:",
        *distro.remediation(distro.AUDIO_PULSE, info).lines(),
    ]
    raise CaptureError("\n".join(lines))


def _run(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise CaptureError(f"{argv[0]} not found — run vinowhisper-setup") from exc
    except subprocess.CalledProcessError as exc:
        raise CaptureError(f"{' '.join(argv)} failed: {exc.stderr.strip()}") from exc
    return result.stdout


def _pw_dump() -> list[dict]:
    if _which("pw-dump") is None:
        return []
    try:
        parsed = json.loads(_run(["pw-dump"]))
    except (CaptureError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _default_sink_from_pw_dump() -> str | None:
    """The default sink out of PipeWire's own metadata, when pactl is absent.

    A PipeWire install without pipewire-pulse has no pactl at all, which used
    to make the whole tool unusable there for want of one string.
    """
    for obj in _pw_dump():
        info = obj.get("info") or {}
        if (info.get("props") or {}).get("metadata.name") != "default":
            continue
        for entry in obj.get("metadata") or []:
            if entry.get("key") == "default.audio.sink":
                value = entry.get("value")
                if isinstance(value, dict):
                    name = value.get("name")
                    if isinstance(name, str):
                        return name
                elif isinstance(value, str):
                    return value
    return None


def default_sink() -> str:
    if _which("pactl") is not None:
        return _run(["pactl", "get-default-sink"]).strip()
    name = _default_sink_from_pw_dump()
    if name:
        return name
    raise CaptureError(
        "could not determine the default sink: pactl is not installed and "
        "pw-dump had no default.audio.sink metadata.\n"
        + "\n".join(distro.remediation(distro.AUDIO_PULSE).lines())
    )


def monitor_source(sink: str | None = None) -> str:
    """The source name that carries a sink's output, for the PulseAudio path.

    pw-record takes the sink itself plus stream.capture.sink; parec needs the
    `.monitor` source spelled out.
    """
    name = sink or default_sink()
    return name if name.endswith(".monitor") else f"{name}.monitor"


def sink_muted(sink: str = "@DEFAULT_SINK@") -> bool | None:
    """Whether the sink is muted, or None if it couldn't be determined.

    Context rather than diagnosis: measured 2026-08-07, this machine's sink
    monitor is pre-mute and carries full signal while muted (README table). It
    is reported because it is cheap and because someone will ask.
    """
    if _which("pactl") is None:
        return None
    try:
        answer = _run(["pactl", "get-sink-mute", sink]).strip()
    except CaptureError:
        return None
    if answer.endswith("yes"):
        return True
    if answer.endswith("no"):
        return False
    return None


def monitor_channel_volumes(sink: str | None = None) -> bool | None:
    """The `monitor.channel-volumes` node property on a sink, if readable.

    Decides whether a sink's monitor is pre- or post-*volume*. Not the same
    question as mute, which PipeWire handles separately. PipeWire defaults it
    to false. PipeWire-only: PulseAudio has no equivalent property, and None
    there means "not applicable", not "broken".
    """
    try:
        target = sink or default_sink()
    except CaptureError:
        return None

    for obj in _pw_dump():
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("node.name") != target:
            continue
        value = props.get("monitor.channel-volumes")
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() == "true"
    return None


def playback_streams() -> list[dict[str, str]]:
    """Capture targets: applications on PipeWire, monitor sources on PulseAudio.

    Note "with an open stream", not "currently playing audio": there is no
    level probe here, so an app that is paused or muted at its own volume
    control still appears, carrying silence. That is a real source of
    confusion (hit 2026-08-07) and vinowhisper-doctor's level probe is what
    distinguishes them.
    """
    if backend().supports_app_capture:
        return _pipewire_streams()
    return _pulse_monitor_sources()


def _pipewire_streams() -> list[dict[str, str]]:
    streams = []
    for obj in _pw_dump():
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


def _pulse_monitor_sources() -> list[dict[str, str]]:
    """Monitor sources, since PulseAudio cannot tap one application's stream.

    Listing sink-inputs here would be worse than listing nothing: they would
    look like valid --target values and parec cannot record them.
    """
    if _which("pactl") is None:
        return []
    try:
        raw = _run(["pactl", "list", "short", "sources"])
    except CaptureError:
        return []

    sources = []
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) < 2 or not fields[1].endswith(".monitor"):
            continue
        sources.append({"target": fields[1], "app": fields[1], "media": "monitor source"})
    return sources


def record_argv(source: str, target: str | None, chosen: Backend | None = None) -> list[str]:
    """The command line that streams raw f32 PCM on stdout.

    Kept as a pure function of (source, target, backend) so the argv for every
    supported combination is checkable without an audio server present.
    """
    chosen = chosen or backend()
    rate = str(config.SAMPLE_RATE_HZ)

    if chosen.name == PIPEWIRE:
        argv = ["pw-record", "--raw", "--rate", rate, "--channels", "1", "--format", "f32"]
        if source == "output":
            # An explicit --target may be an app's playback stream rather than
            # a sink; stream.capture.sink taps the monitor ports either way.
            argv += ["--target", target or default_sink()]
            argv += ["-P", "{ stream.capture.sink = true }"]
        elif target:
            argv += ["--target", target]
        argv.append("-")
        return argv

    # parec writes raw PCM to stdout with no container, same as pw-record --raw.
    argv = ["parec", f"--rate={rate}", "--channels=1", "--format=float32le"]
    if source == "output":
        argv.append(f"--device={monitor_source(target)}")
    elif target:
        argv.append(f"--device={target}")
    return argv
