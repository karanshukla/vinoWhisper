"""The pinned caption bar: transcript above, a status line below.

Rich rather than Textual, deliberately. The commit policy (see stitch.py)
means a word is never revised once printed, so the transcript is append-only
and belongs in the terminal's own scrollback, where it stays after the app
exits and where the terminal's native selection and search still work. Only
the status line needs to redraw, which is exactly what rich.live.Live does.

The one subtlety: partial lines cannot go into scrollback, because Live
redraws its region immediately below whatever was last printed and expects to
start at column 0. So the line currently being built lives *inside* the Live
region and only moves up into scrollback once it is full. Words still appear
the moment they are confirmed, and no word ever gets split across a wrap.

Pending words are shown dimmed on their own line. They are the words that have
been heard once but are waiting on a second cycle to agree, so surfacing them
turns the two-cycle commit delay from "the captions are frozen" into "it is
still deciding", without printing anything that might turn out to be wrong.
"""

import math
import time
from collections import deque

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import config, events

_METER_WIDTH = 14
_SPARK_CHARS = "▁▂▃▄▅▆▇█"
_SPARK_WIDTH = 8
_HISTORY = 32

# dBFS range the meter spans. -60 is well below the silence gate (0.002 rms is
# about -54dBFS), 0 is full scale.
_METER_FLOOR_DB = -60.0
_METER_CEIL_DB = -5.0

_SILENCE_DB = 20 * math.log10(config.SILENCE_RMS_THRESHOLD)
_QUIET_DB = -35.0


def _dbfs(rms: float) -> float:
    return _METER_FLOOR_DB if rms <= 0 else max(_METER_FLOOR_DB, 20 * math.log10(rms))


def _meter(rms: float) -> Text:
    db = _dbfs(rms)
    span = _METER_CEIL_DB - _METER_FLOOR_DB
    filled = int(round((db - _METER_FLOOR_DB) / span * _METER_WIDTH))
    filled = max(0, min(_METER_WIDTH, filled))

    if db < _SILENCE_DB:
        colour = "red"
    elif db < _QUIET_DB:
        colour = "yellow"
    else:
        colour = "green"

    bar = Text("█" * filled, style=colour)
    bar.append("─" * (_METER_WIDTH - filled), style="dim")
    bar.append(f" {db:>5.0f}dB", style="dim")
    return bar


def _sparkline(values: deque[float]) -> str:
    if not values:
        return ""
    recent = list(values)[-_SPARK_WIDTH:]
    peak = max(recent)
    if peak <= 0:
        return _SPARK_CHARS[0] * len(recent)
    return "".join(
        _SPARK_CHARS[min(len(_SPARK_CHARS) - 1, int(value / peak * (len(_SPARK_CHARS) - 1)))]
        for value in recent
    )


class RichRenderer:
    """Consumes the same events as TerminalRenderer, draws a live status bar.

    Use as a context manager so the Live display is torn down on exit.
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._live: Live | None = None

        self._device = "?"
        self._state = ("starting", "yellow")
        self._started_at = time.monotonic()

        self._line: list[str] = []  # the transcript line being built
        self._columns = 0
        self._word_count = 0

        self._pending: list[str] = []
        self._rms = 0.0
        self._gain = 1.0
        self._cycle_s: float | None = None
        self._cycle_history: deque[float] = deque(maxlen=_HISTORY)
        self._silent_for: float | None = None
        self._muted = False

    def __enter__(self) -> "RichRenderer":
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=4,
            transient=False,
        )
        self._live.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._flush_line(force=True)
        if self._live is not None:
            # Drop the status bar on the way out so the final transcript is the
            # last thing left on screen.
            self._live.update(Group(), refresh=True)
            self._live.__exit__(*exc_info)
            self._live = None

    def handle(self, event: events.Event) -> None:
        if isinstance(event, events.Ready):
            self._device = event.device
            self._state = ("live", "green")
        elif isinstance(event, events.Cycle):
            self._state = ("live", "green")
            self._silent_for = None
            self._muted = False
            self._rms = event.rms
            self._gain = event.gain
            self._cycle_s = event.total_s
            self._cycle_history.append(event.total_s)
            self._pending = event.pending
            self._add_words(event.confirmed)
        elif isinstance(event, events.Silence):
            self._rms = event.rms
            self._silent_for = event.elapsed_s
            self._muted = bool(event.sink_muted)
            self._state = ("MUTED", "red") if self._muted else ("no signal", "red")
        elif isinstance(event, events.Stopped):
            self._add_words(event.flushed)
            self._pending = []
            self._state = ("stopped", "dim")

        self._refresh()

    # --- transcript ------------------------------------------------------

    def _add_words(self, words: list[str]) -> None:
        for word in words:
            width = self._width()
            # +1 for the space that would precede it.
            if self._columns and self._columns + 1 + len(word) > width:
                self._flush_line()
            if self._columns:
                self._line.append(" ")
                self._columns += 1
            self._line.append(word)
            self._columns += len(word)
            self._word_count += 1

    def _flush_line(self, force: bool = False) -> None:
        """Move the completed line up into the terminal's scrollback."""
        if not self._line:
            return
        text = "".join(self._line)
        target = self._live.console if self._live is not None else self.console
        target.print(Text(text), markup=False, highlight=False)
        self._line = []
        self._columns = 0
        if force:
            pass  # nothing else to do; kept explicit so callers read clearly

    def _width(self) -> int:
        # Leave room for the panel border the status bar draws below.
        return max(20, self.console.width - 4)

    # --- status bar ------------------------------------------------------

    def _refresh(self) -> None:
        if self._live is not None:
            # refresh=True, not a bare update(): without it the new renderable
            # is only picked up by the auto-refresh thread on its next tick, so
            # any redraw triggered in between (notably the one Live does when
            # the transcript scrolls) paints stale numbers. Auto-refresh stays
            # on regardless, to keep the elapsed clock moving during a decode.
            self._live.update(self._render(), refresh=True)

    def _render(self) -> Group:
        rows = []
        if self._line:
            rows.append(Text("".join(self._line)))
        rows.append(self._panel())
        return Group(*rows)

    def _panel(self) -> Panel:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        grid.add_row(self._stats_left(), self._stats_right())

        pending = self._pending_line()
        body: Group | Table = Group(grid, pending) if pending else Group(grid)
        return Panel(
            body,
            title=f"[dim]vinoWhisper[/dim] [bold]{self._device}[/bold]",
            title_align="left",
            border_style="dim",
            padding=(0, 1),
        )

    def _stats_left(self) -> Text:
        label, colour = self._state
        line = Text("● ", style=colour)
        line.append(f"{label:<9} ", style=colour)
        line.append_text(_meter(self._rms))
        if self._gain > 1.0:
            line.append(f"  ×{self._gain:.0f}", style="dim")
        if self._silent_for is not None and self._silent_for >= 3.0:
            line.append(f"  {self._silent_for:.0f}s", style="red")
        return line

    def _stats_right(self) -> Text:
        line = Text()
        if self._cycle_s is not None:
            line.append(f"⟳ {self._cycle_s:.1f}s ", style="cyan")
            line.append(f"{_sparkline(self._cycle_history)}  ", style="cyan dim")
            # The commit policy needs two cycles to agree and the hop is
            # whatever the last cycle took, so this is the floor on how far
            # behind the audio a caption lands.
            mean = sum(self._cycle_history) / len(self._cycle_history)
            line.append(f"lag ~{2 * mean:.1f}s  ", style="dim")
        if self._pending:
            line.append(f"⏳{len(self._pending)}  ", style="yellow")
        line.append(f"{self._word_count} words  ", style="dim")
        line.append(self._elapsed(), style="dim")
        return line

    def _pending_line(self) -> Text | None:
        if not self._pending:
            return None
        text = " ".join(self._pending)
        budget = max(20, self._width() - 12)
        if len(text) > budget:
            text = "…" + text[-budget:]
        return Text(f"hearing… {text}", style="dim italic", overflow="ellipsis", no_wrap=True)

    def _elapsed(self) -> str:
        seconds = int(time.monotonic() - self._started_at)
        return f"{seconds // 60}:{seconds % 60:02d}"
