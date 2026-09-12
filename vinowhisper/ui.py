import math
import time
from collections import deque
from types import TracebackType

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import config, events

_METER_WIDTH = 14
_SPARK_CHARS = "▁▂▃▄▅▆▇█"
_SPARK_WIDTH = 8
_HISTORY = 32

_METER_FLOOR_DB = -60.0
_METER_CEIL_DB = -5.0

_SILENCE_DB = 20 * math.log10(config.SILENCE_RMS_THRESHOLD)
_QUIET_DB = -35.0

_PARAGRAPH_SILENCE_S = 2.5

_PARAGRAPH_MIN_WORDS = 70

_GUTTER_W = 8
_GUTTER_MIN_WIDTH = 60

_SENTENCE_ENDS = ".?!"
_CLOSERS = "\"'”’)]"

_ABBREVIATIONS = frozenset(
    "mr. mrs. ms. dr. prof. st. jr. sr. vs. etc. e.g. i.e. approx. inc. ltd.".split()  # noqa: SIM905
)


def _ends_sentence(word: str) -> bool:
    stripped = word.rstrip(_CLOSERS)
    if not stripped or stripped[-1] not in _SENTENCE_ENDS:
        return False
    if stripped.lower() in _ABBREVIATIONS:
        return False
    return not all(len(piece) <= 1 for piece in stripped.split("."))


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
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._live: Live | None = None

        self._device = "?"
        self._degraded = False
        self._device_warning = ""
        self._state = ("starting", "yellow")
        self._started_at = time.monotonic()

        self._line: list[str] = []
        self._columns = 0
        self._word_count = 0
        self._gutter = ""
        self._new_paragraph = True
        self._break_pending = False
        self._words_in_paragraph = 0

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

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self._flush_line()
        if self._live is not None:
            self._live.update(Group(), refresh=True)
            self._live.__exit__(exc_type, exc, traceback)
            self._live = None

    def handle(self, event: events.Event) -> None:
        if isinstance(event, events.Ready):
            self._device = event.device
            self._degraded = event.degraded
            self._device_warning = event.warnings[0] if event.warnings else ""
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
            if event.elapsed_s >= _PARAGRAPH_SILENCE_S:
                self._break_pending = True
        elif isinstance(event, events.Stopped):
            self._add_words(event.flushed)
            self._pending = []
            self._state = ("stopped", "dim")

        self._refresh()

    def _add_words(self, words: list[str]) -> None:
        for word in words:
            if self._break_pending and self._words_in_paragraph:
                self._end_paragraph()
            self._break_pending = False

            width = self._width()
            if self._columns and self._columns + 1 + len(word) > width:
                self._flush_line()
            if not self._line:
                self._begin_line()
            if self._columns:
                self._line.append(" ")
                self._columns += 1
            self._line.append(word)
            self._columns += len(word)
            self._word_count += 1
            self._words_in_paragraph += 1

            if self._words_in_paragraph >= _PARAGRAPH_MIN_WORDS and _ends_sentence(word):
                self._break_pending = True

    def _begin_line(self) -> None:
        if not self._gutter_width():
            self._gutter = ""
        elif self._new_paragraph:
            self._gutter = f"[{self._elapsed()}]".ljust(_GUTTER_W)
        else:
            self._gutter = " " * _GUTTER_W
        self._new_paragraph = False

    def _end_paragraph(self) -> None:
        self._flush_line()
        target = self._live.console if self._live is not None else self.console
        target.print()
        self._new_paragraph = True
        self._words_in_paragraph = 0

    def _flush_line(self) -> None:
        if not self._line:
            return
        target = self._live.console if self._live is not None else self.console
        target.print(self._line_text(), markup=False, highlight=False)
        self._line = []
        self._columns = 0

    def _line_text(self) -> Text:
        text = Text()
        if self._gutter:
            text.append(self._gutter, style="dim")
        text.append("".join(self._line), style="bold bright_white")
        return text

    def _gutter_width(self) -> int:
        return _GUTTER_W if self.console.width >= _GUTTER_MIN_WIDTH else 0

    def _width(self) -> int:
        return max(20, self.console.width - 4 - self._gutter_width())

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render(), refresh=True)

    def _render(self) -> Group:
        rows: list[RenderableType] = []
        if self._line:
            rows.append(self._line_text())
        rows.append(self._panel())
        return Group(*rows)

    def _panel(self) -> Panel:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        grid.add_row(self._stats_left(), self._stats_right())

        rows: list[Text | Table] = [grid]
        warning = self._warning_line()
        if warning is not None:
            rows.append(warning)
        pending = self._pending_line()
        if pending is not None:
            rows.append(pending)

        badge = (
            f"[bold red]{self._device}[/bold red]"
            if self._degraded
            else f"[bold]{self._device}[/bold]"
        )
        return Panel(
            Group(*rows),
            title=f"[dim]vinoWhisper[/dim] {badge}",
            title_align="left",
            border_style="red" if self._degraded else "dim",
            padding=(0, 1),
        )

    def _warning_line(self) -> Text | None:
        if not self._degraded or not self._device_warning:
            return None
        return Text(
            f"⚠ {self._device_warning}",
            style="red",
            overflow="ellipsis",
            no_wrap=True,
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
