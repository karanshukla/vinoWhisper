"""Answers the questions that would otherwise need a dozen hand-run commands.

Most of what goes wrong here is environmental, not code: the NPU not
enumerating, the model exported the wrong way for the device it ended up on,
no capture tool installed, nothing actually playing. Each check below asks the
system directly and reports what it found, and where an answer is actionable it
prints the command for *this* distro rather than for Fedora.

Two design rules worth keeping:

- **Measure, don't reason.** The level probe exists because the
  `monitor.channel-volumes` property answers only the volume half of the
  question, and on 2026-08-07 a whole investigation went the wrong way on
  reasoning alone. Run it once with audio playing normally and once with the
  system muted; if the sink monitor drops to silence while an application
  stream stays audible, that is the mute problem confirmed by measurement.
- **--json is for bug reports.** Same checks, machine-readable, no probing
  prompts. Paste it into an issue and the environment stops being a guess.
"""

import argparse
import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass

from . import __version__, audio, capture, config, devices, distro, recorder

_PROBE_S = 2.0

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "FAIL", "??"


@dataclass
class Result:
    status: str
    label: str
    detail: str = ""


def _python() -> Result:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    if version[:2] == (3, 14):
        return Result(FAIL, "python", f"{text} breaks optimum's export code; use 3.13")
    return Result(OK, "python", text)


def _distro() -> Result:
    info = distro.detect()
    if not info.known:
        return Result(
            WARN,
            "distro",
            f"{info.name} — unrecognised, so package names below are generic advice",
        )
    return Result(OK, "distro", str(info))


def _openvino() -> list[Result]:
    try:
        import openvino
    except ImportError as exc:
        return [Result(FAIL, "openvino", f"{exc} — run `uv sync`")]

    results = [Result(OK, "openvino", openvino.__version__)]
    try:
        import openvino_genai

        results.append(Result(OK, "openvino-genai", openvino_genai.__version__))
    except ImportError as exc:
        results.append(Result(FAIL, "openvino-genai", str(exc)))
    return results


def _devices() -> list[Result]:
    """What OpenVINO can run on, which one gets picked, and why not the NPU."""
    try:
        inventory = devices.available()
    except devices.DeviceError as exc:
        return [Result(FAIL, "devices", str(exc))]

    results = [
        Result(OK, "devices", ", ".join(str(device) for device in inventory) or "none"),
    ]

    npu = [device for device in inventory if device.kind == "NPU"]
    if npu:
        results.append(Result(OK, "npu", str(npu[0])))
    else:
        results.append(Result(FAIL, "npu", "not enumerated by OpenVINO"))
        # The interesting part: kernel node, permissions, then the userspace
        # package for this distro. "No NPU" on its own fixes nothing.
        for note in devices.npu_preflight():
            status = OK if note.ok else (UNKNOWN if note.ok is None else FAIL)
            results.append(Result(status, f"npu: {note.label}", note.detail))
        remedy = distro.remediation(distro.NPU_DRIVER)
        results.append(
            Result(WARN, "npu: userspace driver", "\n" + "\n".join(remedy.lines()).lstrip())
        )

    try:
        selection = devices.select(config.DEFAULT_DEVICE, inventory)
    except devices.DeviceError as exc:
        results.append(Result(FAIL, "selected device", str(exc)))
        return results

    results.append(
        Result(
            OK if not selection.degraded else WARN,
            "selected device",
            f"{selection.device.name}"
            + ("" if not selection.degraded else " — NOT the NPU, captions will lag further"),
        )
    )
    return results


def _models() -> list[Result]:
    """Both exports, checked against the device that would actually be used."""
    try:
        inventory = devices.available()
        needed_kind = devices.select(config.DEFAULT_DEVICE, inventory).kind
    except devices.DeviceError:
        needed_kind = "NPU"

    results = []
    for kind, variant, directory in (
        ("NPU", "npu", config.MODEL_DIR),
        ("CPU/GPU", "stateful", config.STATEFUL_MODEL_DIR),
    ):
        required = (needed_kind == "NPU") if kind == "NPU" else (needed_kind != "NPU")
        label = f"model ({variant})"
        if not directory.is_dir():
            results.append(
                Result(
                    FAIL if required else UNKNOWN,
                    label,
                    f"not exported at {directory}"
                    + (
                        f" — run ./scripts/convert_model.sh --variant {variant}"
                        if required
                        else " (only needed if you run on this device class)"
                    ),
                )
            )
            continue

        # The separate decoder_with_past submodel is the tell that the export
        # used --disable-stateful, which the NPU static pipeline requires and
        # which CPU/GPU cannot load at all.
        has_with_past = any(directory.glob("*decoder_with_past*.xml"))
        wrong = has_with_past if kind != "NPU" else not has_with_past
        if wrong:
            results.append(
                Result(
                    FAIL if required else WARN,
                    label,
                    f"{directory} is the wrong export for {kind} — "
                    f"re-run ./scripts/convert_model.sh --variant {variant}",
                )
            )
        else:
            results.append(Result(OK, label, str(directory)))
    return results


def _server() -> list[Result]:
    import requests

    try:
        response = requests.get(
            f"{config.SERVER_URL}/health",
            timeout=(config.CONNECT_TIMEOUT_S, config.MODEL_LOAD_TIMEOUT_S),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        return [
            Result(
                WARN,
                "server",
                f"not reachable ({exc.__class__.__name__}); "
                "systemctl --user status vinowhisper-server.socket",
            )
        ]
    except ValueError:
        return [Result(FAIL, "server", f"{config.SERVER_URL}/health returned non-JSON")]

    results = [Result(OK, "server", f"{config.SERVER_URL} on {payload.get('device', '?')}")]
    if payload.get("version") and payload["version"] != __version__:
        # A stale resident server is easy to miss: it self-exits only after
        # IDLE_TIMEOUT_S, so an upgrade does not take effect until it does.
        results.append(
            Result(
                WARN,
                "server version",
                f"server is {payload['version']}, client is {__version__} — "
                "systemctl --user restart vinowhisper-server.service",
            )
        )
    for warning in payload.get("warnings", []):
        results.append(Result(WARN, "server device", str(warning)))
    return results


def _audio_tools() -> list[Result]:
    backends = capture.available_backends()
    if not backends:
        info = distro.detect()
        return [
            Result(
                FAIL,
                "capture backend",
                "neither pw-record (PipeWire) nor parec (PulseAudio) is installed\n"
                + "\n".join(distro.remediation(distro.AUDIO_PIPEWIRE, info).lines()),
            )
        ]

    active = backends[0]
    results = [
        Result(
            OK,
            "capture backend",
            f"{active.name} via {active.record}"
            + ("" if active.supports_app_capture else " (no per-application capture)"),
        )
    ]
    for tool in ("pactl", "pw-dump"):
        if shutil.which(tool) is None:
            results.append(
                Result(
                    UNKNOWN if tool == "pw-dump" else WARN,
                    f"tool: {tool}",
                    "not installed — some checks below degrade to 'unknown'",
                )
            )
    return results


def _sink() -> list[Result]:
    try:
        sink = capture.default_sink()
    except capture.CaptureError as exc:
        return [Result(FAIL, "sink", str(exc).splitlines()[0])]

    results = [Result(OK, "sink", sink)]

    muted = capture.sink_muted()
    if muted is None:
        results.append(Result(UNKNOWN, "muted", "could not read mute state"))
    else:
        results.append(
            Result(
                WARN if muted else OK,
                "muted",
                # Not "so captions cannot work": measured 2026-08-07, this
                # machine's monitor carries full signal while muted. Whether
                # mute matters is what the level probe below decides.
                "YES (the level probe below decides whether that matters)" if muted else "no",
            )
        )

    monitor_volumes = capture.monitor_channel_volumes(sink)
    # Unset is not unknown on PipeWire: it defaults this to false, so an absent
    # property is a definitive "pre-volume". Reporting it as UNKNOWN sent a
    # real investigation chasing a non-problem on 2026-08-07.
    results.append(
        Result(
            WARN if monitor_volumes else OK,
            "monitor.channel-volumes",
            "true, so the monitor is post-volume: lowering the volume degrades captions"
            if monitor_volumes
            else "false, so the monitor is pre-volume"
            + (" (unset, PipeWire's default)" if monitor_volumes is None else ""),
        )
    )
    return results


def _probe(source: str, target: str | None, label: str) -> Result:
    """Capture for a couple of seconds and report the level actually seen."""
    try:
        with recorder.Recorder(source=source, target=target) as rec:
            time.sleep(_PROBE_S)
            rec.check_alive()
            samples = rec.window(_PROBE_S)
    except capture.CaptureError as exc:
        return Result(FAIL, label, str(exc).splitlines()[0])

    if samples.size == 0:
        return Result(FAIL, label, "captured nothing")

    level = audio.rms(samples)
    if level < config.SILENCE_RMS_THRESHOLD:
        return Result(WARN, label, f"rms {level:.5f}, below the silence gate")
    _, gain = audio.normalize(samples, config.TARGET_RMS, config.MAX_GAIN)
    return Result(OK, label, f"rms {level:.5f} (would be boosted {gain:.1f}x)")


def _levels() -> list[Result]:
    results = [_probe("output", None, "level: default sink")]

    try:
        streams = capture.playback_streams()
    except capture.CaptureError as exc:
        return results + [Result(UNKNOWN, "playback streams", str(exc).splitlines()[0])]

    if not streams:
        results.append(
            Result(UNKNOWN, "playback streams", "nothing playing; start some audio and re-run")
        )
        return results

    for stream in streams[:3]:
        results.append(
            _probe("output", stream["target"], f"level: {stream['app']} ({stream['target']})")
        )
    return results


def _verdict(results: list[Result]) -> str | None:
    by_label = {result.label: result for result in results}
    sink_level = by_label.get("level: default sink")
    app_levels = [
        result
        for label, result in by_label.items()
        if label.startswith("level: ") and result is not sink_level
    ]
    muted = by_label.get("muted")

    if sink_level is None or not app_levels:
        return None
    sink_silent = sink_level.status != OK
    app_audible = any(result.status == OK for result in app_levels)

    if sink_silent and app_audible:
        return (
            "The default sink monitors as silence while an application stream is audible.\n"
            "That is the mute/volume problem, confirmed. Use --target <serial> from the\n"
            "list above and captions will keep working with the system muted."
        )
    if sink_silent and not app_audible:
        return (
            "Nothing is audible anywhere, so this is upstream of vinoWhisper: check that\n"
            "something is actually playing, then re-run."
        )
    if not sink_silent and muted is not None and muted.detail.startswith("YES"):
        return (
            "The sink is muted and its monitor still carries signal, so the monitor is\n"
            "pre-mute on this setup. Sink mute is not the cause of missing captions here.\n"
            "If captions stopped when you 'muted', check whether you muted the app rather\n"
            "than the system: an app writing silence into its own stream cannot be\n"
            "captured from anywhere, including --target."
        )
    return None


def collect(probe: bool = True) -> list[Result]:
    """Every check, in the order that makes a failure readable top to bottom."""
    results: list[Result] = [
        Result(OK, "vinowhisper", __version__),
        _python(),
        _distro(),
    ]
    results += _openvino()
    results += _devices()
    results += _models()
    results += _server()
    results += _audio_tools()
    results += _sink()
    if probe:
        results += _levels()
    return results


def _print_human(results: list[Result]) -> None:
    width = max(len(result.label) for result in results)
    for result in results:
        detail = result.detail.replace("\n", "\n" + " " * (width + 11))
        print(f"  [{result.status:>4}] {result.label:<{width}}  {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check everything vinoWhisper needs from the system.",
        epilog="Exit codes: 0 all good, 1 at least one check failed.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable output, for pasting into a bug report.",
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help=f"Skip the {_PROBE_S:.0f}s-per-target live level capture.",
    )
    parser.add_argument("--version", action="version", version=f"vinowhisper {__version__}")
    args = parser.parse_args(argv)

    if not args.json:
        print(f"vinowhisper-doctor {__version__}\n")
        if not args.no_probe:
            print(f"probing input levels for {_PROBE_S:.0f}s per target...\n", file=sys.stderr)

    results = collect(probe=not args.no_probe)
    verdict = _verdict(results)
    failures = [result for result in results if result.status == FAIL]

    if args.json:
        print(
            json.dumps(
                {
                    "version": __version__,
                    "python": sys.version,
                    "distro": asdict(distro.detect()),
                    "checks": [asdict(result) for result in results],
                    "verdict": verdict,
                    "failed": [result.label for result in failures],
                },
                indent=2,
            )
        )
        return 1 if failures else 0

    _print_human(results)
    if verdict:
        print(f"\n{verdict}")
    if failures:
        print(f"\n{len(failures)} check(s) failed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
