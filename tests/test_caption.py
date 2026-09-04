"""The caption loop's user-facing text.

Only the parts that need no audio server: `caption_events` itself drives a
Recorder and an HTTP client, so what is checkable here is the renderer and the
messages. That is not a consolation prize — the silence notice is the single
piece of prose in this project that was wrong for a month.
"""

from vinowhisper import caption, events


def test_characterization_the_silence_notice_does_not_blame_mute_or_volume():
    """characterization: the sink monitor is pre-volume AND pre-mute.

    This is the one every instinct gets backwards, and this repo asserted the
    opposite of it in seven places — including two messages printed to the
    user — until it was measured on 2026-08-07: muted, with audio playing, the
    monitor read 0.08578 against the app's 0.08781, a ratio of 0.98. It moves
    with neither the slider nor the mute button.

    So "you are muted" is not the explanation for a silent capture, and this
    message must never drift back to saying it is. What can actually silence
    the capture is the list below: nothing playing, the *application* muted at
    its own volume, or --target aimed at effect_output.bass_eq.
    """
    notice = caption._SILENCE_NOTICE.lower()

    # Matched as separate tokens: the notice is hard-wrapped, so the phrase
    # can land with a newline through the middle of it.
    assert "pre-volume" in notice
    assert "pre-mute" in notice
    assert "2026-08-07" in notice  # measured claims carry a date
    assert "nothing is actually playing" in notice
    assert "muted the *application*" in notice
    assert "effect_output.bass_eq" in notice


def test_characterization_the_muted_line_is_context_and_not_a_diagnosis():
    """characterization: mute is reported because it is cheap and someone will
    ask, not because it explains anything. Saying "that is the cause" was the
    wrong version.
    """
    assert "does not silence the monitor" in caption._MUTED_LINE


def test_the_silence_notice_waits_before_saying_anything(capsys):
    """Silence is normal. Most of a minute of it while someone believes
    captions are running is the part worth naming.
    """
    renderer = caption.TerminalRenderer()
    renderer.handle(events.Silence(elapsed_s=1.0, rms=0.0, sink_muted=None))
    assert capsys.readouterr().err == ""

    renderer.handle(
        events.Silence(elapsed_s=caption._SILENCE_NOTICE_AFTER_S, rms=0.0, sink_muted=False)
    )
    first = capsys.readouterr().err
    assert "no signal on the capture target" in first
    assert "does not silence the monitor" not in first  # not muted, so not mentioned

    # Said once, not every cycle it stays quiet.
    renderer.handle(events.Silence(elapsed_s=90.0, rms=0.0, sink_muted=False))
    assert capsys.readouterr().err == ""


def test_a_muted_sink_is_mentioned_but_still_not_blamed(capsys):
    renderer = caption.TerminalRenderer()
    renderer.handle(
        events.Silence(elapsed_s=caption._SILENCE_NOTICE_AFTER_S, rms=0.0, sink_muted=True)
    )
    reported = capsys.readouterr().err
    assert "The default sink is muted" in reported
    assert "does not silence the monitor" in reported


def test_a_cycle_rearms_the_silence_notice(capsys):
    renderer = caption.TerminalRenderer()
    renderer.handle(events.Silence(elapsed_s=60.0, rms=0.0, sink_muted=False))
    capsys.readouterr()

    renderer.handle(
        events.Cycle(
            index=1,
            captured_s=1.0,
            window_s=12.0,
            hop_s=1.0,
            rms=0.02,
            gain=1.0,
            first_piece_s=0.2,
            total_s=1.0,
            transcript="audio came back",
            confirmed=["audio", "came", "back"],
        )
    )
    capsys.readouterr()

    renderer.handle(events.Silence(elapsed_s=60.0, rms=0.0, sink_muted=False))
    assert "no signal on the capture target" in capsys.readouterr().err
