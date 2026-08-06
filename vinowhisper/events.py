"""What the caption loop emits, instead of printing.

The loop yields these; consumers decide what to do with them. Today that's a
terminal renderer and an optional session recorder. The point of the split is
that a pinned-on-top TUI is another consumer, not a rewrite of the loop: every
number worth putting on a status bar is already on Cycle, and Silence carries
enough to render a "no signal" indicator with a cause.
"""

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Ready:
    """Server answered its health check; the loop is about to start."""

    device: str


@dataclass(frozen=True)
class Cycle:
    """One transcription round trip."""

    index: int
    captured_s: float  # position in the session, for replay
    window_s: float
    hop_s: float
    rms: float
    gain: float
    first_piece_s: float | None
    total_s: float
    transcript: str  # raw, pre-stitch
    confirmed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Silence:
    """A window below the silence gate. Emitted every cycle it stays that way,
    so a UI can show a live counter; the terminal renderer throttles it.
    """

    elapsed_s: float
    rms: float
    sink_muted: bool | None  # None when it couldn't be determined


@dataclass(frozen=True)
class Stopped:
    """Session over. `flushed` is whatever agreed once but never got a
    confirming cycle, released rather than dropped.
    """

    flushed: list[str] = field(default_factory=list)


Event = Ready | Cycle | Silence | Stopped


def to_dict(event: Event) -> dict:
    return {"event": type(event).__name__, **asdict(event)}
