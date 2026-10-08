"""Recording a session and reading it back.

A --record directory is the fixture format for every offline replay, so the
round trip has to hold: what went in as float32 comes back close enough to
re-transcribe, and the event log stays parseable after a Ctrl+C mid-write.
"""

import json

import numpy as np

from vinowhisper import config, events, session


def test_events_round_trip_through_json():
    ready = events.Ready(
        device="CPU",
        device_full="12th Gen Intel",
        degraded=True,
        warnings=["Running on the CPU."],
        server_version="0.2.0",
    )
    payload = events.to_dict(ready)
    assert payload["event"] == "Ready"
    assert payload["degraded"] is True
    assert payload["warnings"] == ["Running on the CPU."]
    # Must survive the writer, which json.dumps() straight into the log.
    assert json.loads(json.dumps(payload)) == payload


def test_audio_round_trips_through_the_wav(tmp_path):
    writer = session.SessionWriter(tmp_path)
    original = (np.sin(np.linspace(0, 40, 16000)) * 0.5).astype(np.float32)
    writer.audio_chunk(original)
    writer.close()

    read_back = session.read_audio(tmp_path)
    assert read_back.size == original.size
    # int16 on the way through, so exactness is not the bar; audible fidelity is.
    assert np.max(np.abs(read_back - original)) < 1e-3


def test_events_are_flushed_per_line(tmp_path):
    """A Ctrl+C mid-session must still leave a usable log."""
    writer = session.SessionWriter(tmp_path)
    writer.event(events.Ready(device="NPU"))
    writer.event(events.Silence(elapsed_s=1.0, rms=0.0, sink_muted=None))

    # Deliberately reading before close().
    lines = (tmp_path / session.EVENTS_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "Ready"
    writer.close()


def test_read_cycles_filters_to_cycle_events(tmp_path):
    writer = session.SessionWriter(tmp_path)
    writer.event(events.Ready(device="NPU"))
    for index in range(3):
        writer.event(
            events.Cycle(
                index=index,
                captured_s=float(index),
                window_s=12.0,
                hop_s=1.0,
                rms=0.02,
                gain=1.0,
                first_piece_s=0.2,
                total_s=1.1,
                transcript=f"cycle {index}",
                confirmed=[f"cycle{index}"],
                pending=[],
            )
        )
    writer.event(events.Stopped(flushed=[]))
    writer.close()

    cycles = list(session.read_cycles(tmp_path))
    assert [record["index"] for record in cycles] == [0, 1, 2]
    assert len(session.read_events(tmp_path)) == 5


def test_the_wav_is_written_at_the_capture_rate(tmp_path):
    import wave

    writer = session.SessionWriter(tmp_path)
    writer.audio_chunk(np.zeros(100, dtype=np.float32))
    writer.close()
    with wave.open(str(tmp_path / session.AUDIO_NAME), "rb") as handle:
        assert handle.getframerate() == config.SAMPLE_RATE_HZ
        assert handle.getnchannels() == 1


def test_a_recording_is_private_whatever_the_umask(tmp_path):
    """SECURITY.md calls a recording as sensitive as whatever was playing."""
    import os
    import stat

    directory = tmp_path / "session"
    old_umask = os.umask(0o022)
    try:
        writer = session.SessionWriter(directory)
        writer.close()
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in (session.AUDIO_NAME, session.EVENTS_NAME):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600


def test_an_overwritten_recording_loses_its_old_permissions(tmp_path):
    for name in (session.AUDIO_NAME, session.EVENTS_NAME):
        (tmp_path / name).write_bytes(b"old")
        (tmp_path / name).chmod(0o644)

    writer = session.SessionWriter(tmp_path)
    writer.close()

    for name in (session.AUDIO_NAME, session.EVENTS_NAME):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _transcript(target, clock=None, source="output"):
    from datetime import datetime

    return session.TranscriptWriter(
        target,
        source,
        clock=clock or _Clock(),
        now=lambda: datetime(2026, 9, 29, 14, 2, 7),
    )


def _cycle(confirmed):
    return events.Cycle(
        index=1,
        captured_s=1.0,
        window_s=12.0,
        hop_s=1.0,
        rms=0.1,
        gain=1.0,
        first_piece_s=0.1,
        total_s=0.7,
        transcript=" ".join(confirmed),
        confirmed=confirmed,
        pending=["not", "yet"],
    )


def test_a_transcript_has_a_header_stamped_paragraphs_and_no_trailing_blank_line(tmp_path):
    clock = _Clock()
    writer = _transcript(tmp_path, clock)
    writer.event(events.Ready(device="NPU"))
    writer.event(_cycle(["First", "paragraph."]))
    clock.now = 71.0
    writer.event(events.Silence(elapsed_s=2.5, rms=0.0, sink_muted=False))
    writer.event(_cycle(["Next", "one."]))
    writer.event(events.Stopped(flushed=["Tail."]))

    assert writer.path == tmp_path / "2026-09-29-140207.txt"
    assert writer.path.read_text(encoding="utf-8") == (
        "vinoWhisper transcript, 2026-09-29 14:02, system audio, NPU\n\n"
        "[00:00] First paragraph.\n\n"
        "[01:11] Next one. Tail.\n"
    )


def test_pending_words_are_never_written(tmp_path):
    writer = _transcript(tmp_path)
    writer.event(_cycle(["Seen."]))
    writer.close()
    assert "not yet" not in writer.path.read_text(encoding="utf-8")


def test_a_session_with_no_words_leaves_no_file(tmp_path):
    target = tmp_path / "transcripts"
    writer = _transcript(target)
    writer.event(events.Ready(device="NPU"))
    writer.event(events.Silence(elapsed_s=30.0, rms=0.0, sink_muted=False))
    writer.event(_cycle([]))
    writer.event(events.Stopped(flushed=[]))
    assert writer.path is None
    assert not target.exists()


def test_words_reach_the_file_each_cycle_before_the_session_ends(tmp_path):
    writer = _transcript(tmp_path)
    writer.event(_cycle(["Killed", "mid-session"]))
    assert writer.path.read_text(encoding="utf-8").endswith("[00:00] Killed mid-session")


def test_a_long_paragraph_breaks_at_a_sentence_end(tmp_path):
    writer = _transcript(tmp_path)
    words = ["word"] * 69 + ["end.", "Next"]
    writer.event(_cycle(words))
    writer.close()
    body = writer.path.read_text(encoding="utf-8").split("\n\n", 1)[1]
    assert len(body.split("\n\n")) == 2
    assert body.split("\n\n")[1].startswith("[00:00] Next")


def test_the_transcript_breaks_exactly_where_the_terminal_does(tmp_path):
    """characterization: both consumers take their paragraph breaks from
    ParagraphBreaker, so one run of events must give the same break points in
    the file and in the terminal. A copy of the rules in either one drifts.
    """
    import io

    from rich.console import Console

    from vinowhisper.ui import RichRenderer

    script = [
        _cycle(["a"] * 40 + ["dr."]),
        events.Silence(elapsed_s=2.5, rms=0.0, sink_muted=False),
        _cycle(["b"] * 69 + ["end."]),
        _cycle(["c", "d"]),
        events.Silence(elapsed_s=1.0, rms=0.0, sink_muted=False),
        _cycle(["e"]),
        events.Stopped(flushed=["f"]),
    ]

    import re

    console = Console(file=io.StringIO(), width=100000, force_terminal=False)
    renderer = RichRenderer(console=console)
    writer = _transcript(tmp_path)
    for event in script:
        renderer.handle(event)
        writer.event(event)
    renderer._flush_line()

    def paragraphs(text):
        return [
            re.sub(r"^\[\d+:\d+\]\s*", "", block).split() for block in text.strip().split("\n\n")
        ]

    terminal = paragraphs(console.file.getvalue())
    saved = paragraphs(writer.path.read_text(encoding="utf-8").split("\n\n", 1)[1])
    assert len(saved) == 3
    assert saved == terminal


def test_a_transcript_is_private_whatever_the_umask(tmp_path):
    import os
    import stat

    directory = tmp_path / "transcripts"
    old_umask = os.umask(0o022)
    try:
        writer = _transcript(directory)
        writer.event(_cycle(["secret"]))
        writer.close()
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(writer.path.stat().st_mode) == 0o600


def test_an_existing_loose_file_is_tightened_when_appended_to(tmp_path):
    import os
    import stat

    target = tmp_path / "notes.txt"
    target.write_text("old\n")
    target.chmod(0o644)
    writer = _transcript(target)
    writer.event(_cycle(["new"]))
    writer.close()

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_a_file_path_is_appended_to_not_overwritten(tmp_path):
    target = tmp_path / "notes" / "talks.txt"
    for word in ("One.", "Two."):
        writer = _transcript(target)
        writer.event(_cycle([word]))
        writer.close()

    text = target.read_text(encoding="utf-8")
    assert text.count("vinoWhisper transcript") == 2
    assert "[00:00] One.\n\nvinoWhisper" in text
    assert text.endswith("[00:00] Two.\n")
