"""Captioning the microphone and the system audio at once (#118): each is a lane,
transcribed and stitched on its own, and every consumer can tell whose words they are."""

import json
from dataclasses import replace
from datetime import datetime

import numpy as np

from tests import pcm
from vinowhisper import caption, events, replay, session
from vinowhisper.ui import RichRenderer


class _LaneRecorder:
    made: list["_LaneRecorder"] = []

    def __init__(self, source, target, tap=None):
        self.source, self.target, self.tap = source, target, tap
        self.checks = 0
        self.made.append(self)

    @property
    def captured_s(self):
        return 3.0 + self.checks

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def check_alive(self):
        self.checks += 1
        if self.checks > 3:
            raise KeyboardInterrupt

    def window(self, seconds):
        # The two lanes carry different tones so the client can tell them apart.
        hz = 220.0 if self.source == "mic" else 440.0
        return pcm.sine(hz, 2.0, amplitude=0.1)


class _LaneClient:
    heard_language = ""

    def __init__(self, **_kwargs):
        pass

    def wait_ready(self):
        return {"device": "CPU"}

    def transcribe(self, samples):
        zero_crossings = int(np.count_nonzero(np.diff(np.signbit(samples))))
        return ("mic words here" if zero_crossings < 1000 else "output words there"), 0.1


def _run(monkeypatch, **kwargs):
    _LaneRecorder.made = []
    monkeypatch.setattr(caption, "Recorder", _LaneRecorder)
    monkeypatch.setattr(caption, "TranscriptionClient", _LaneClient)
    return list(caption.caption_events("both", "42", 12.0, **kwargs))


def test_both_sources_are_captured_as_a_you_lane_and_a_them_lane(monkeypatch):
    stream = _run(monkeypatch)

    assert {(r.source, r.target) for r in _LaneRecorder.made} == {("mic", None), ("output", "42")}
    cycles = [e for e in stream if isinstance(e, events.Cycle)]
    assert {c.lane for c in cycles} == {"you", "them"}
    assert [c.index for c in cycles] == list(range(1, len(cycles) + 1))


def test_each_lane_is_stitched_on_its_own_and_flushed_when_the_session_ends(monkeypatch):
    stream = _run(monkeypatch)

    flushed = {e.lane: e.words for e in stream if isinstance(e, events.Flushed)}
    assert flushed.keys() == {"you", "them"}
    assert isinstance(stream[-1], events.Stopped)
    assert stream[-1].flushed == []
    for lane, words in flushed.items():
        said = {c.transcript for c in stream if isinstance(c, events.Cycle) and c.lane == lane}
        assert len(said) == 1
        assert set(words) <= set(said.pop().split())


def test_one_source_is_still_one_unlabelled_lane(monkeypatch):
    _LaneRecorder.made = []
    monkeypatch.setattr(caption, "Recorder", _LaneRecorder)
    monkeypatch.setattr(caption, "TranscriptionClient", _LaneClient)

    stream = list(caption.caption_events("mic", None, 12.0))

    assert {getattr(e, "lane", "") for e in stream} == {""}
    assert not any(isinstance(e, events.Flushed) for e in stream)


def test_a_lane_is_a_field_on_the_json_the_tray_reads():
    cycle = events.Cycle(
        index=1, captured_s=1.0, window_s=1.0, hop_s=1.0, rms=0.1, gain=1.0,
        first_piece_s=None, total_s=0.5, transcript="hi", lane="you",
    )  # fmt: skip
    assert json.loads(json.dumps(events.to_dict(cycle)))["lane"] == "you"
    assert events.to_dict(events.Silence(1.0, 0.0, None))["lane"] == ""


def test_the_terminal_starts_a_labelled_line_each_time_the_speaker_changes(capsys):
    renderer = caption.TerminalRenderer()
    for lane, words in [("them", ["hello", "there"]), ("them", ["again"]), ("you", ["hi"])]:
        renderer.handle(events.Flushed(lane=lane, words=words))

    assert capsys.readouterr().out == "them: hello there again\nyou: hi"


def test_nothing_is_said_about_your_own_microphone_going_quiet(capsys):
    renderer = caption.TerminalRenderer()
    renderer.handle(events.Silence(elapsed_s=90.0, rms=0.0, sink_muted=False, lane="you"))

    assert capsys.readouterr().err == ""


def _transcript(target, source="both"):
    ticks = iter(range(0, 1000, 5))
    return session.TranscriptWriter(
        target, source, clock=lambda: float(next(ticks)), now=lambda: datetime(2026, 11, 22, 9, 0)
    )


def _cycle(lane, words):
    return events.Cycle(
        index=1, captured_s=1.0, window_s=1.0, hop_s=1.0, rms=0.1, gain=1.0,
        first_piece_s=None, total_s=0.5, transcript=" ".join(words), confirmed=words, lane=lane,
    )  # fmt: skip


def test_each_lane_of_a_call_starts_its_own_labelled_paragraph(tmp_path):
    writer = _transcript(tmp_path / "call.txt")
    writer.event(events.Ready(device="CPU"))
    writer.event(_cycle("them", ["How", "are", "you?"]))
    writer.event(_cycle("them", ["Fine", "here."]))
    writer.event(_cycle("you", ["Good,", "thanks."]))
    writer.event(_cycle("them", ["Great."]))
    writer.event(events.Stopped())

    lines = (tmp_path / "call.txt").read_text().split("\n\n")
    assert lines[0].startswith("vinoWhisper transcript, 2026-11-22 09:00, microphone and system")
    assert [line.strip().split(" ", 1)[1] for line in lines[1:4]] == [
        "them: How are you? Fine here.",
        "you: Good, thanks.",
        "them: Great.",
    ]


def test_a_two_lane_recording_keeps_one_audio_file_per_lane_and_restitches_each(tmp_path, capsys):
    writer = session.SessionWriter(tmp_path, lanes=("you", "them"))
    writer.lane_tap("you")(np.full(1600, 0.1, dtype=np.float32))
    writer.lane_tap("them")(np.full(3200, 0.2, dtype=np.float32))
    writer.event(_cycle("you", ["hi", "there"]))
    writer.event(replace(_cycle("them", ["hello"]), index=2))
    writer.close()

    assert session.read_audio(tmp_path, "you").size == 1600
    assert session.read_audio(tmp_path, "them").size == 3200
    assert [r["index"] for r in session.read_cycles(tmp_path, "them")] == [2]

    assert replay._restitch(tmp_path, verbose=False) == 0
    assert capsys.readouterr().out.splitlines() == ["you: hi there", "them: hello"]


def test_the_status_bar_labels_a_paragraph_by_who_spoke():
    from io import StringIO

    from rich.console import Console

    out = StringIO()
    renderer = RichRenderer(console=Console(file=out, width=100, force_terminal=False))
    renderer.handle(events.Ready(device="CPU"))
    renderer.handle(_cycle("them", ["Hello", "there."]))
    renderer.handle(_cycle("you", ["Hi."]))
    renderer.__exit__()

    text = out.getvalue()
    assert "them: Hello there." in text
    assert "you: Hi." in text
