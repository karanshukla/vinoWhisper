"""The status bar, rendered to a fixed-width buffer.

Rendering into a StringIO console is the only way this gets checked at all —
it has never been verified on a physical terminal (see CLAUDE.md's open
questions), so the things that are cheap to assert are asserted: that no word
is ever split by a wrap, that the timestamp gutter behaves as a hanging
indent, that a scheduled paragraph break is only spent when a word actually
arrives, and that a non-NPU device is impossible to miss.
"""

import io

from rich.console import Console

from vinowhisper import events
from vinowhisper.ui import _PARAGRAPH_SILENCE_S, RichRenderer


def make_console(width: int = 80) -> Console:
    return Console(file=io.StringIO(), width=width, force_terminal=False, highlight=False)


def cycle(confirmed: list[str], pending: list[str] | None = None, **kwargs) -> events.Cycle:
    fields = {
        "index": 1,
        "captured_s": 10.0,
        "window_s": 12.0,
        "hop_s": 1.5,
        "rms": 0.02,
        "gain": 2.5,
        "first_piece_s": 0.2,
        "total_s": 1.2,
        "transcript": " ".join(confirmed),
        "confirmed": confirmed,
        "pending": pending or [],
    }
    fields.update(kwargs)
    return events.Cycle(**fields)


def rendered(console: Console) -> str:
    return console.file.getvalue()  # type: ignore[union-attr]


def render_panel(renderer: RichRenderer) -> str:
    """The status panel as text, without going through Live."""
    console = make_console(renderer.console.width)
    console.print(renderer._panel())
    return rendered(console)


def test_confirmed_words_reach_scrollback_and_are_never_split():
    console = make_console(width=40)
    renderer = RichRenderer(console=console)
    words = ["antidisestablishmentarianism", "is", "a", "very", "long", "word", "indeed"]
    renderer.handle(cycle(words))
    renderer._flush_line()

    output = rendered(console)
    for word in words:
        assert word in output
    for line in output.splitlines():
        assert len(line) <= 40


def test_the_first_line_of_a_paragraph_gets_a_timestamp_gutter():
    console = make_console()
    renderer = RichRenderer(console=console)
    renderer.handle(cycle(["hello", "there"]))
    renderer._flush_line()
    line = rendered(console).splitlines()[0]
    assert line.startswith("[0:00]")


def test_the_gutter_is_dropped_on_a_narrow_terminal():
    """Below 60 columns those 8 columns are worth more as text."""
    console = make_console(width=40)
    renderer = RichRenderer(console=console)
    renderer.handle(cycle(["hello", "there"]))
    renderer._flush_line()
    assert not rendered(console).startswith("[")


def test_a_pause_breaks_the_paragraph_only_once_a_word_follows():
    console = make_console()
    renderer = RichRenderer(console=console)
    renderer.handle(cycle(["first", "sentence."]))
    renderer.handle(events.Silence(elapsed_s=5.0, rms=0.0, sink_muted=False))
    # Nothing may be emitted yet: scrollback is append-only, so a session must
    # never end on a stray blank line.
    assert "\n\n" not in rendered(console)

    renderer.handle(cycle(["second"]))
    renderer._flush_line()
    output = rendered(console)
    assert "" in output.splitlines()
    assert output.splitlines()[-1].startswith("[0:00]")


def test_a_pause_shorter_than_the_paragraph_threshold_is_just_breath():
    """_PARAGRAPH_SILENCE_S sits well above a normal inter-sentence gap on
    purpose: a threshold low enough to catch ordinary speech rhythm shreds the
    transcript into two-line stanzas. Both sides of it are pinned here, since
    only the far side was covered.
    """
    console = make_console()
    renderer = RichRenderer(console=console)
    renderer.handle(cycle(["first", "sentence."]))
    renderer.handle(events.Silence(elapsed_s=_PARAGRAPH_SILENCE_S - 0.1, rms=0.0, sink_muted=False))
    renderer.handle(cycle(["still", "the", "same", "thought"]))
    renderer._flush_line()

    assert "" not in rendered(console).splitlines()


def test_a_pause_at_the_paragraph_threshold_breaks():
    console = make_console()
    renderer = RichRenderer(console=console)
    renderer.handle(cycle(["first", "sentence."]))
    renderer.handle(events.Silence(elapsed_s=_PARAGRAPH_SILENCE_S, rms=0.0, sink_muted=False))
    renderer.handle(cycle(["a", "new", "thought"]))
    renderer._flush_line()

    assert "" in rendered(console).splitlines()


def test_a_pause_before_any_words_does_not_open_with_a_blank_line():
    """The break is only ever spent on a word, and there is no paragraph to
    end yet — so silence at the very start of a session leaves no gap.
    """
    console = make_console()
    renderer = RichRenderer(console=console)
    renderer.handle(events.Silence(elapsed_s=30.0, rms=0.0, sink_muted=False))
    renderer.handle(cycle(["first", "words"]))
    renderer._flush_line()

    assert rendered(console).splitlines()[0].startswith("[0:00]")


def test_stopped_flushes_the_pending_tail():
    console = make_console()
    renderer = RichRenderer(console=console)
    renderer.handle(events.Stopped(flushed=["last", "words"]))
    renderer._flush_line()
    assert "last words" in rendered(console)


def test_the_panel_shows_the_device_and_the_live_stats():
    renderer = RichRenderer(console=make_console())
    renderer.handle(events.Ready(device="NPU", device_full="Intel(R) AI Boost"))
    renderer.handle(cycle(["a", "word"], pending=["still", "deciding"]))
    panel = render_panel(renderer)
    assert "NPU" in panel
    assert "live" in panel
    assert "hearing…" in panel
    assert "still deciding" in panel
    # The lag estimate is 2x mean cycle time — the floor the commit policy imposes.
    assert "lag ~2.4s" in panel


def test_a_degraded_device_is_impossible_to_miss():
    renderer = RichRenderer(console=make_console(width=100))
    renderer.handle(
        events.Ready(
            device="CPU",
            device_full="12th Gen Intel",
            degraded=True,
            warnings=["Running on the CPU. Captions will lag further behind."],
        )
    )
    panel = render_panel(renderer)
    assert "CPU" in panel
    assert "⚠" in panel
    assert "lag further" in panel


def test_no_warning_line_on_the_npu():
    renderer = RichRenderer(console=make_console())
    renderer.handle(events.Ready(device="NPU", device_full="Intel(R) AI Boost"))
    assert "⚠" not in render_panel(renderer)


def test_silence_flips_the_state_label():
    renderer = RichRenderer(console=make_console())
    renderer.handle(events.Ready(device="NPU"))
    renderer.handle(events.Silence(elapsed_s=10.0, rms=0.0, sink_muted=True))
    assert "MUTED" in render_panel(renderer)

    renderer.handle(events.Silence(elapsed_s=10.0, rms=0.0, sink_muted=False))
    assert "no signal" in render_panel(renderer)


def test_long_pending_text_is_truncated_from_the_left():
    renderer = RichRenderer(console=make_console(width=60))
    renderer.handle(cycle([], pending=["word"] * 40))
    assert "…" in render_panel(renderer)


class RecordingLive:
    """Stands in for rich.live.Live, recording how update() was called."""

    def __init__(self) -> None:
        self.refreshes: list[bool] = []

    def update(self, renderable, refresh: bool = False) -> None:
        self.refreshes.append(refresh)

    def __exit__(self, *exc_info: object) -> None:
        return None


def test_characterization_every_live_update_passes_refresh_true():
    """characterization: `Live.update()` is never called bare here.

    Reads like a redundant argument — auto-refresh is on at 4/s, so the new
    renderable will be picked up anyway. It will, on the *next tick*, and any
    redraw triggered in between paints the previous renderable. The one that
    matters is the redraw Live does when the transcript scrolls, which lands
    between ticks constantly and repainted stale numbers onto the status bar.

    This was a real bug caught in rendering tests, so removing the argument
    costs nothing visible in CI and quietly regresses the bar on hardware.
    """
    renderer = RichRenderer(console=make_console())
    renderer._live = RecordingLive()

    renderer.handle(events.Ready(device="NPU"))
    renderer.handle(cycle(["a", "word"]))
    renderer.handle(events.Silence(elapsed_s=1.0, rms=0.0, sink_muted=False))

    assert renderer._live.refreshes == [True, True, True]


def test_characterization_the_status_bar_is_torn_down_with_a_refresh_too():
    """characterization: same rule on the way out. The final update clears the
    panel so the transcript is the last thing on screen, and a bare update
    would leave the bar painted until a tick that never comes.
    """
    renderer = RichRenderer(console=make_console())
    live = RecordingLive()
    renderer._live = live
    renderer.__exit__()

    assert live.refreshes[-1] is True
    assert renderer._live is None
