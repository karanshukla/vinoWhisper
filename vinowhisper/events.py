from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Ready:
    device: str
    device_full: str = ""
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)
    server_version: str = ""


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


@dataclass(frozen=True)
class Silence:
    elapsed_s: float
    rms: float
    sink_muted: bool | None


@dataclass(frozen=True)
class Stopped:
    flushed: list[str] = field(default_factory=list)


Event = Ready | Cycle | Silence | Stopped


def to_dict(event: Event) -> dict:
    return {"event": type(event).__name__, **asdict(event)}
