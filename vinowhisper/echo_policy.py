import json
import subprocess
from dataclasses import dataclass, field

from . import capture

AUTO, ON, OFF = "auto", "on", "off"
MODES = (AUTO, ON, OFF)

_BLUETOOTH_API = "bluez5"
_USB_BUS = "usb"
_PERSONAL_PORTS = frozenset({"headphones", "headset", "handset", "handsfree"})
_PW_NODE = "PipeWire:Interface:Node"
_PW_LINK = "PipeWire:Interface:Link"
_FOLLOW_DEPTH = 5


@dataclass(frozen=True)
class Output:
    sink: str = ""
    port: str = ""
    port_type: str = ""
    api: str = ""
    bus: str = ""
    # The sinks a filter-chain or loopback played through on the way here.
    via: tuple[str, ...] = field(default=(), compare=False)

    @property
    def key(self) -> tuple[str, str]:
        return (self.sink, self.port)

    def personal(self) -> str | None:
        if self.api == _BLUETOOTH_API:
            return "Bluetooth"
        if self.bus == _USB_BUS:
            return "USB audio"
        if self.port_type.lower() in _PERSONAL_PORTS:
            return "headphones"
        return None


@dataclass(frozen=True)
class Decision:
    active: bool
    reason: str

    def label(self, mode: str) -> str:
        return f"{'on' if self.active else 'off'}: {self.reason}" if mode == AUTO else self.reason


def decide(mode: str, output: Output | None) -> Decision:
    # (pinned by `test_echo_policy.py::test_on_and_off_ignore_the_output`)
    if mode == OFF:
        return Decision(False, "off in settings")
    if mode == ON:
        return Decision(True, "on in settings")
    if output is None:
        return Decision(True, "default output unknown, assuming speakers")
    personal = output.personal()
    if personal:
        return Decision(False, personal)
    return Decision(True, "speakers")


def _nodes(dump: list[dict]) -> dict[int, dict]:
    return {
        obj["id"]: (obj.get("info") or {}).get("props") or {}
        for obj in dump
        if obj.get("type") == _PW_NODE
    }


def _playback_of(sink: dict, nodes: dict[int, dict]) -> int | None:
    group = sink.get("node.link-group")
    name = str(sink.get("node.name", ""))
    for node_id, props in nodes.items():
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        if group and props.get("node.link-group") == group:
            return node_id
        if name.startswith("effect_input.") and props.get("node.name") == name.replace(
            "effect_input.", "effect_output.", 1
        ):
            return node_id
    return None


def _played_into(node_id: int, dump: list[dict], nodes: dict[int, dict]) -> str | None:
    for obj in dump:
        info = obj.get("info") or {}
        if obj.get("type") == _PW_LINK and info.get("output-node-id") == node_id:
            target = nodes.get(info.get("input-node-id", -1), {})
            return str(target.get("node.name") or "") or None
    return None


def resolve(sink: str, dump: list[dict], sinks: list[dict]) -> Output:
    nodes = _nodes(dump)
    by_name = {str(entry.get("name")): entry for entry in sinks}
    via: list[str] = []
    for _ in range(_FOLLOW_DEPTH):
        entry = by_name.get(sink)
        if entry is None:
            break
        props = entry.get("properties") or {}
        api, bus = str(props.get("device.api", "")), str(props.get("device.bus", ""))
        if api or bus:
            return _output(sink, entry, tuple(via))
        node = next((p for p in nodes.values() if p.get("node.name") == sink), None)
        playback = _playback_of(node, nodes) if node is not None else None
        into = _played_into(playback, dump, nodes) if playback is not None else None
        if into is None or into == sink:
            return _output(sink, entry, tuple(via))
        via.append(sink)
        sink = into
    return Output(sink=sink, via=tuple(via))


def _output(sink: str, entry: dict, via: tuple[str, ...]) -> Output:
    props = entry.get("properties") or {}
    port = str(entry.get("active_port") or "")
    port_type = next(
        (str(p.get("type", "")) for p in entry.get("ports") or [] if p.get("name") == port), ""
    )
    return Output(
        sink=sink,
        port=port,
        port_type=port_type,
        api=str(props.get("device.api", "")),
        bus=str(props.get("device.bus", "")),
        via=via,
    )


def _pactl_sinks() -> list[dict]:
    try:
        result = subprocess.run(
            ["pactl", "--format=json", "list", "sinks"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        parsed = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def current_output() -> Output | None:
    try:
        sink = capture.default_sink()
    except capture.CaptureError:
        return None
    sinks = _pactl_sinks()
    if not sinks:
        return None
    return resolve(sink, capture._pw_dump(), sinks)
