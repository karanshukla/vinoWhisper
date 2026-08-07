"""Answers the questions that currently need five hand-run commands.

Most of what can go wrong here is environmental, not code: the NPU not
enumerating, the model exported the wrong way, the sink muted, the monitor
being post-volume. `vinowhisper-doctor` checks each one directly and says what
it found, including a live level probe that settles the mute question by
measurement rather than by reasoning. That probe is the point: the
`monitor.channel-volumes` property answers only the *volume* half, and on
2026-08-07 it read pre-volume on a machine whose monitor still died on mute.

Run it once with audio playing normally, then again with the system muted. If
the sink monitor drops to silence while an application stream stays audible,
that is the whole mute problem, confirmed, and --target is the fix.
"""

import sys
import time
from dataclasses import dataclass

from . import audio, config, recorder

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


def _openvino() -> list[Result]:
    try:
        import openvino
    except ImportError as exc:
        return [Result(FAIL, "openvino", str(exc))]

    results = [Result(OK, "openvino", openvino.__version__)]
    try:
        devices = openvino.Core().available_devices
    except Exception as exc:  # noqa: BLE001 - any driver problem shows up here
        return results + [Result(FAIL, "devices", str(exc))]

    npu = [device for device in devices if device.startswith("NPU")]
    results.append(
        Result(
            OK if npu else FAIL,
            "npu",
            ", ".join(devices) if npu else f"no NPU among {devices}",
        )
    )
    try:
        import openvino_genai

        results.append(Result(OK, "openvino-genai", openvino_genai.__version__))
    except ImportError as exc:
        results.append(Result(FAIL, "openvino-genai", str(exc)))
    return results


def _model() -> Result:
    directory = config.MODEL_DIR
    if not directory.is_dir():
        return Result(FAIL, "model", f"{directory} missing; run scripts/convert_model.sh")
    # The separate decoder_with_past submodel is the tell that the export used
    # --disable-stateful, which the NPU static pipeline requires.
    with_past = list(directory.glob("*decoder_with_past*.xml"))
    if not with_past:
        return Result(
            FAIL,
            "model",
            "no decoder_with_past submodel; re-export with --disable-stateful",
        )
    return Result(OK, "model", str(directory))


def _server() -> Result:
    import requests

    try:
        response = requests.get(
            f"{config.SERVER_URL}/health",
            timeout=(config.CONNECT_TIMEOUT_S, config.MODEL_LOAD_TIMEOUT_S),
        )
        response.raise_for_status()
        return Result(OK, "server", f"{config.SERVER_URL} on {response.json().get('device')}")
    except requests.RequestException as exc:
        return Result(
            WARN,
            "server",
            f"not reachable ({exc.__class__.__name__}); "
            "systemctl --user status vinowhisper-server.socket",
        )


def _sink() -> list[Result]:
    try:
        sink = recorder.default_sink()
    except recorder.CaptureError as exc:
        return [Result(FAIL, "sink", str(exc))]

    results = [Result(OK, "sink", sink)]

    muted = recorder.sink_muted()
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

    monitor_volumes = recorder.monitor_channel_volumes(sink)
    # Unset is not unknown: PipeWire defaults this to false, so an absent
    # property is a definitive "pre-volume". Reporting it as UNKNOWN sent a
    # real investigation chasing a non-problem on 2026-08-07.
    #
    # Deliberately says nothing about mute either way. Mute is a separate
    # mechanism in PipeWire, and this machine reads false here (monitor
    # confirmed pre-volume by level probe) while still monitoring 0.00000 when
    # muted. The level probe below is what answers the mute question.
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
    except recorder.CaptureError as exc:
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
        streams = recorder.playback_streams()
    except recorder.CaptureError as exc:
        return results + [Result(UNKNOWN, "playback streams", str(exc))]

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
        result for label, result in by_label.items() if label.startswith("level: ") and result is not sink_level
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


def main() -> int:
    results: list[Result] = [_python()]
    results += _openvino()
    results.append(_model())
    results.append(_server())
    results += _sink()

    print("vinowhisper-doctor\n")
    width = 0
    for result in results:
        width = max(width, len(result.label))

    print(f"probing input levels for {_PROBE_S:.0f}s per target...\n", file=sys.stderr)
    results += _levels()
    width = max(width, *(len(result.label) for result in results))

    for result in results:
        print(f"  [{result.status:>4}] {result.label:<{width}}  {result.detail}")

    verdict = _verdict(results)
    if verdict:
        print(f"\n{verdict}")

    failures = [result for result in results if result.status == FAIL]
    if failures:
        print(f"\n{len(failures)} check(s) failed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
