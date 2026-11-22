from dataclasses import asdict, dataclass, field

LANE_YOU = "you"
LANE_THEM = "them"


@dataclass(frozen=True)
class Ready:
    device: str
    device_full: str = ""
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)
    server_version: str = ""
    language: str = ""
    task: str = ""
    model: str = ""
    # Two-lane sessions: how the mic lane is echo-cancelled, and why (#216).
    echo_mode: str = ""
    echo_active: bool = False
    echo_reason: str = ""


@dataclass(frozen=True)
class Cycle:
    index: int
    captured_s: float
    window_s: float
    hop_s: float
    rms: float
    gain: float
    first_piece_s: float | None
    total_s: float
    transcript: str
    confirmed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    # Auto mode only: what two windows in a row heard, so the label does not flicker.
    language: str = ""
    # "" for a single source; "you" (mic) or "them" (output) when both are captioned.
    lane: str = ""
    # Words the text dedup took out of the `you` lane because `them` had just said them.
    dropped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Silence:
    elapsed_s: float
    rms: float
    sink_muted: bool | None
    # Sound, but the speech detector heard nobody talking in it.
    no_speech: bool = False
    lane: str = ""


# Words a lane still held back when the session ended; only two-lane sessions send it.
@dataclass(frozen=True)
class Flushed:
    lane: str
    words: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EchoStatus:
    mode: str
    active: bool
    reason: str


@dataclass(frozen=True)
class Hint:
    kind: str
    message: str


@dataclass(frozen=True)
class Stopped:
    flushed: list[str] = field(default_factory=list)


Event = Ready | Cycle | Silence | Flushed | EchoStatus | Hint | Stopped


def speech_label(language: str, task: str, heard: str = "") -> str:
    if not language:
        return ""
    if heard:
        language = f"{language}: {heard}"
    return f"{language} → en" if task == "translate" else language


def to_dict(event: Event) -> dict:
    return {"event": type(event).__name__, **asdict(event)}
