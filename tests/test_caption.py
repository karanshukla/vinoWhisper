"""The caption loop's user-facing text.

Only the parts that need no audio server: `caption_events` itself drives a
Recorder and an HTTP client, so what is checkable here is the renderer and the
messages. That is not a consolation prize — the silence notice is the single
piece of prose in this project that was wrong for a month.
"""

import json
from types import SimpleNamespace

from tests import pcm
from vinowhisper import caption, events

_EVERY_EVENT = [
    events.Ready(device="CPU", device_full="Intel(R) Core(TM)", degraded=True, warnings=["no NPU"]),
    events.Cycle(
        index=1,
        captured_s=4.0,
        window_s=4.0,
        hop_s=1.0,
        rms=0.02,
        gain=2.5,
        first_piece_s=0.2,
        total_s=1.1,
        transcript="don’t stop",
        confirmed=["don’t"],
        pending=["stop"],
    ),
    events.Silence(elapsed_s=46.0, rms=0.0, sink_muted=True),
    events.Stopped(flushed=["stop"]),
]


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


def test_json_mode_writes_one_ascii_object_per_line(capsys):
    """--json is read line by line from a pipe by vinowhisper-gui. Whisper's
    curly quotes must survive as escapes, so a reader under LANG=C (a login
    autostart) never sees a multi-byte character at all.
    """
    renderer = caption.JsonRenderer()
    for event in _EVERY_EVENT:
        renderer.handle(event)

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(_EVERY_EVENT)
    assert all(line.isascii() for line in lines)
    assert json.loads(lines[1])["confirmed"] == ["don’t"]


def test_the_fields_the_gui_reads_are_on_the_wire(capsys):
    """gui/src/protocol.rs parses these by name, in another language, in
    another process. So each record is `events.to_dict` verbatim, and the
    fields the overlay depends on are named here: renaming one fails this
    test instead of leaving the overlay drawing nothing. If it does go red
    because a field was renamed, protocol.rs needs the same rename.
    """
    reads = {
        "Ready": {"device", "degraded", "warnings", "language", "task"},
        "Cycle": {"confirmed", "pending", "total_s"},
        "Silence": {"elapsed_s", "sink_muted"},
        "Stopped": {"flushed"},
    }
    renderer = caption.JsonRenderer()
    for event in _EVERY_EVENT:
        renderer.handle(event)
        record = json.loads(capsys.readouterr().out)
        assert record == events.to_dict(event)
        assert reads[record["event"]] <= record.keys()


def test_an_error_record_carries_the_reason(capsys):
    """A GUI has no terminal to show stderr on, so the reason travels too."""
    caption.JsonRenderer().error("Capture failed: pw-record exited with status 1")
    assert json.loads(capsys.readouterr().out) == {
        "event": "Error",
        "message": "Capture failed: pw-record exited with status 1",
    }


def test_list_targets_does_not_print_control_characters_a_stream_names(monkeypatch, capsys):
    """media.name is set by the playing app: a browser tab's title, say."""
    monkeypatch.setattr(
        caption.capture, "backend", lambda: SimpleNamespace(name="pw", supports_app_capture=True)
    )
    monkeypatch.setattr(
        caption,
        "playback_streams",
        lambda: [{"target": "42", "app": "Fire‮fox", "media": "tab\x1b]0;owned\x07 title"}],
    )

    assert caption._list_targets() == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "‮" not in out
    assert "Firefox" in out and "tab]0;owned title" in out


class _OneCycleRecorder:
    captured_s = 5.0

    def __init__(self, **_kwargs):
        self._checks = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def check_alive(self):
        self._checks += 1
        if self._checks > 1:
            raise KeyboardInterrupt

    def window(self, seconds):
        return pcm.sine(220.0, seconds, amplitude=0.1)


class _HostileClient:
    def wait_ready(self):
        return {"device": "CPU"}

    def transcribe(self, samples):
        return "hello\x1b[2J ​world‮", 0.1


def test_model_text_is_stripped_of_control_characters_before_any_renderer(monkeypatch):
    monkeypatch.setattr(caption, "Recorder", _OneCycleRecorder)
    monkeypatch.setattr(caption, "TranscriptionClient", _HostileClient)

    cycles = [e for e in caption.caption_events("output", None, 4.0) if isinstance(e, events.Cycle)]

    assert cycles[0].transcript == "hello[2J world"


def test_the_speech_label_shows_the_language_and_translation():
    assert events.speech_label("fr", "transcribe") == "fr"
    assert events.speech_label("auto", "translate") == "auto → en"
    assert events.speech_label("", "translate") == ""


def test_a_stalled_transcription_window_shrinks_and_translate_is_left_alone():
    stall = caption.stall_window_s
    assert stall(12.0, 3.9, "transcribe") == 12.0
    assert stall(12.0, 4.0, "transcribe") == 6.0
    assert stall(12.0, 30.0, "") == 6.0
    assert stall(5.0, 30.0, "transcribe") == 5.0
    assert stall(12.0, 30.0, "translate") == 12.0


class _AdvancingRecorder:
    def __init__(self, **_kwargs):
        self.checks = 0
        self.asked: list[float] = []

    @property
    def captured_s(self):
        return 1.0 + self.checks

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def check_alive(self):
        self.checks += 1
        if self.checks > 6:
            raise KeyboardInterrupt

    def window(self, seconds):
        self.asked.append(seconds)
        return pcm.sine(220.0, 2.0, amplitude=0.1)


def _windows_asked_for(monkeypatch, task):
    recorders = []

    def make(**kwargs):
        recorders.append(_AdvancingRecorder(**kwargs))
        return recorders[-1]

    class Client:
        def wait_ready(self):
            return {"device": "NPU", "task": task}

        def transcribe(self, samples):
            return "", 0.1

    monkeypatch.setattr(caption, "Recorder", make)
    monkeypatch.setattr(caption, "TranscriptionClient", Client)
    list(caption.caption_events("output", None, 12.0))
    return recorders[0].asked


def test_nothing_committing_for_seconds_shrinks_the_window_the_loop_asks_for(monkeypatch):
    assert _windows_asked_for(monkeypatch, "transcribe") == [12.0, 12.0, 6.0, 6.0, 6.0, 6.0]


def test_translate_keeps_the_full_window_while_stalled(monkeypatch):
    assert set(_windows_asked_for(monkeypatch, "translate")) == {12.0}
