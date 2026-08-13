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
