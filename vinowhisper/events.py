from dataclasses import asdict, dataclass, field


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


@dataclass(frozen=True)
class Silence:
    elapsed_s: float
    rms: float
    sink_muted: bool | None


@dataclass(frozen=True)
class Stopped:
    flushed: list[str] = field(default_factory=list)


Event = Ready | Cycle | Silence | Stopped


def speech_label(language: str, task: str, heard: str = "") -> str:
    if not language:
        return ""
    if heard:
        language = f"{language}: {heard}"
    return f"{language} → en" if task == "translate" else language


def to_dict(event: Event) -> dict:
    return {"event": type(event).__name__, **asdict(event)}
