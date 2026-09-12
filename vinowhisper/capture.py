import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from . import config, distro

PIPEWIRE = "pipewire"
PULSEAUDIO = "pulseaudio"

BACKEND_ENV = "VINOWHISPER_CAPTURE_BACKEND"


class CaptureError(RuntimeError):
    """A capture tool is missing, failed to start, or died mid-session."""


@dataclass(frozen=True)
class Backend:
    name: str
    record: str
    capability: str

    @property
    def supports_app_capture(self) -> bool:
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
    name = sink or default_sink()
    return name if name.endswith(".monitor") else f"{name}.monitor"


def sink_muted(sink: str = "@DEFAULT_SINK@") -> bool | None:
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
    chosen = chosen or backend()
    rate = str(config.SAMPLE_RATE_HZ)

    if chosen.name == PIPEWIRE:
        argv = ["pw-record", "--raw", "--rate", rate, "--channels", "1", "--format", "f32"]
        if source == "output":
            argv += ["--target", target or default_sink()]
            # Without it WirePlumber routes the capture to the mic, --target or not.
            argv += ["-P", "{ stream.capture.sink = true }"]
        elif target:
            argv += ["--target", target]
        argv.append("-")
        return argv

    argv = ["parec", f"--rate={rate}", "--channels=1", "--format=float32le"]
    if source == "output":
        argv.append(f"--device={monitor_source(target)}")
    elif target:
        argv.append(f"--device={target}")
    return argv
