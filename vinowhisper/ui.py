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

The transcript above is broken into timestamped paragraphs. Punctuation is not
inferred here and never was — whisper-small.en emits it, so the words arrive
already punctuated and capitalized. Paragraphs it does not emit, but both
signals needed to place them are already on the event stream: a Silence event
is a real pause in speech, and sentence-final punctuation says where a break
would land cleanly. Everything about the break decision is forward-only,
because a line in scrollback cannot be taken back: a break is *scheduled* by a
pause or a sentence end and only applied when the next word actually arrives,
so a session never ends on a stray blank line, and a paragraph never opens that
nothing goes into.

The same append-only rule is why already-printed lines keep the wrap they were
born with if the terminal is later resized. That is the standing cost of
putting the transcript in scrollback rather than in a widget, and it is the
trade this file takes on purpose.
"""

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

# dBFS range the meter spans. -60 is well below the silence gate (0.002 rms is
# about -54dBFS), 0 is full scale.
_METER_FLOOR_DB = -60.0
_METER_CEIL_DB = -5.0

_SILENCE_DB = 20 * math.log10(config.SILENCE_RMS_THRESHOLD)
_QUIET_DB = -35.0

# --- Paragraphing --------------------------------------------------------
#
# A pause this long reads as a paragraph boundary rather than as breath. Well
# above a normal inter-sentence gap so ordinary speech rhythm doesn't shred the
# transcript into two-line stanzas, and comfortably above MIN_HOP_S so it takes
# several consecutive silent cycles to trip.
_PARAGRAPH_SILENCE_S = 2.5

# Continuous speech never pauses, so silence alone would let one paragraph run
# for the whole session. Past this many words, break at the next sentence end.
# Chosen as a screenful-ish of prose rather than measured.
_PARAGRAPH_MIN_WORDS = 70

# "[MM:SS] ", elapsed rather than wall clock so it agrees with the clock on the
# status bar. Suppressed entirely below _GUTTER_MIN_WIDTH: on a narrow pinned
# window those 8 columns are worth more as text than as a timestamp.
_GUTTER_W = 8
_GUTTER_MIN_WIDTH = 60

_SENTENCE_ENDS = ".?!"
_CLOSERS = "\"'”’)]"

# Sentence-final punctuation that isn't. The word-count floor above means an
# occasional miss just delays a break to the next sentence, so this only needs
# to cover what's common in speech, not every abbreviation in English.
_ABBREVIATIONS = frozenset(
    # One string rather than a list literal: this is a word list, and it reads
    # like one.
    "mr. mrs. ms. dr. prof. st. jr. sr. vs. etc. e.g. i.e. approx. inc. ltd.".split()  # noqa: SIM905
)


def _ends_sentence(word: str) -> bool:
    stripped = word.rstrip(_CLOSERS)
    if not stripped or stripped[-1] not in _SENTENCE_ENDS:
        return False
    if stripped.lower() in _ABBREVIATIONS:
        return False
    # An initialism, not a sentence end: every period-separated piece is a
    # single letter ("U.S.", "F.B.I.", "A."). Catches the whole family without
    # needing them enumerated, unlike the abbreviation set above.
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
    """Consumes the same events as TerminalRenderer, draws a live status bar.

    Use as a context manager so the Live display is torn down on exit.
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._live: Live | None = None

        self._device = "?"
        self._degraded = False
        self._device_warning = ""
        self._state = ("starting", "yellow")
        self._started_at = time.monotonic()

        self._line: list[str] = []  # the transcript line being built
        self._columns = 0
        self._word_count = 0
        self._gutter = ""  # prefix for the line being built; "" until it starts
        self._new_paragraph = True  # next line started gets a timestamp
        self._break_pending = False  # a pause/sentence end is waiting on a word
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
            # Drop the status bar on the way out so the final transcript is the
            # last thing left on screen.
            self._live.update(Group(), refresh=True)
            self._live.__exit__(exc_type, exc, traceback)
            self._live = None

    def handle(self, event: events.Event) -> None:
        if isinstance(event, events.Ready):
            self._device = event.device
            self._degraded = event.degraded
            # First warning only: the panel title is one line and the rest is
            # in the server's journal and in vinowhisper-doctor. What matters
            # on screen is that this is not the NPU, not the full essay.
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
                # Scheduled, not applied: Silence repeats every cycle it stays
                # quiet, and applying here would end the session on a blank
                # line every time. _add_words spends it on the next real word.
                self._break_pending = True
        elif isinstance(event, events.Stopped):
            self._add_words(event.flushed)
            self._pending = []
            self._state = ("stopped", "dim")

        self._refresh()

    # --- transcript ------------------------------------------------------

    def _add_words(self, words: list[str]) -> None:
        for word in words:
            # Spend a scheduled break here rather than where it was decided, so
            # the blank line only ever appears with something following it.
            if self._break_pending and self._words_in_paragraph:
                self._end_paragraph()
            self._break_pending = False

            width = self._width()
            # +1 for the space that would precede it.
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
        """Fix this line's gutter: a timestamp to open a paragraph, blank to
        continue one, so wrapped text stays in a single hanging-indent column.
        """
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
        """Move the completed line up into the terminal's scrollback."""
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
        # Bold and full-brightness: the confirmed transcript is the thing
        # actually being read, so it gets the strongest legibility Rich text
        # styling can give it. The pending "hearing…" line stays dim/italic on
        # purpose (see module docstring), this only touches committed words.
        text.append("".join(self._line), style="bold bright_white")
        return text

    def _gutter_width(self) -> int:
        return _GUTTER_W if self.console.width >= _GUTTER_MIN_WIDTH else 0

    def _width(self) -> int:
        # Leave room for the panel border the status bar draws below, and for
        # the gutter, so a wrapped line still fits once indented.
        return max(20, self.console.width - 4 - self._gutter_width())

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

        # A degraded device is a property of the whole session, not a passing
        # state, so it colours the frame rather than blinking in a corner.
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
