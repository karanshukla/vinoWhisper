"""vinowhisper-dictate: one utterance, recorded until told to stop, decoded once.

The recorder and the client are fakes, so this runs with no microphone and no
server; what is under test is the command handling and what gets emitted.
"""

import numpy as np
import pytest
import requests

from tests import pcm
from vinowhisper import config, dictate
from vinowhisper.recorder import CaptureError

SPEECH = pcm.sine(220.0, 2.0, amplitude=0.1)


class FakeRecording:
    def __init__(self, tap, signal: np.ndarray, fail_on_enter: bool = False) -> None:
        self._tap = tap
        self._signal = signal
        self._fail = fail_on_enter
        self.entered = False
        self.exited = False

    def __enter__(self):
        if self._fail:
            raise CaptureError("pw-record not found")
        self.entered = True
        for chunk in pcm.chunks(self._signal):
            self._tap(chunk)
        return self

    def __exit__(self, *exc_info):
        self.exited = True

    def check_alive(self) -> None:
        pass

    @property
    def captured_s(self) -> float:
        return self._signal.size / config.SAMPLE_RATE_HZ

    def window(self, seconds: float) -> np.ndarray:
        return self._signal[-int(seconds * config.SAMPLE_RATE_HZ) :]


class FakeClient:
    def __init__(self, transcript: str = "Hello there.", error: Exception | None = None) -> None:
        self.transcript = transcript
        self.error = error
        self.decoded: list[np.ndarray] = []

    def wait_ready(self) -> dict:
        if self.error is not None:
            raise self.error
        return {"device": "NPU", "degraded": False, "warnings": []}

    def transcribe(self, samples: np.ndarray) -> tuple[str, float | None]:
        self.decoded.append(samples)
        return self.transcript, 0.2


@pytest.fixture(autouse=True)
def _no_tail(monkeypatch):
    monkeypatch.setattr(dictate, "TAIL_S", 0.0)


def _run(commands, signal=SPEECH, client=None, fail_on_enter=False):
    records: list[dict] = []
    recordings: list[FakeRecording] = []
    client = client or FakeClient()

    def recorder(tap):
        recordings.append(FakeRecording(tap, signal, fail_on_enter))
        return recordings[-1]

    dictation = dictate.Dictation(records.append, client=client, recorder=recorder)
    dictation.feed(commands).join()
    dictation.run()
    return [r for r in records if r["event"] != "Level"], recordings, client


def test_start_then_stop_decodes_once_and_reports_the_device():
    records, recordings, client = _run(["start", "stop"])
    assert [r["event"] for r in records] == ["Listening", "Transcribing", "Ready", "Dictated"]
    assert records[-1]["text"] == "Hello there."
    assert records[2]["device"] == "NPU"
    assert len(client.decoded) == 1
    assert recordings[0].exited


def test_audio_is_normalized_before_it_is_decoded():
    quiet = pcm.sine(220.0, 2.0, amplitude=0.01)
    _, _, client = _run(["start", "stop"], signal=quiet)
    assert dictate.audio.rms(client.decoded[0]) > dictate.audio.rms(quiet)


def test_silence_or_a_bare_tap_costs_no_decode():
    for signal in (pcm.silence(2.0), pcm.sine(220.0, 0.1, amplitude=0.1)):
        records, _, client = _run(["start", "stop"], signal=signal)
        assert records[-1] == {**records[-1], "event": "Dictated", "text": ""}
        assert client.decoded == []


def test_audio_just_under_the_minimum_costs_no_decode():
    just_under = pcm.sine(220.0, dictate.MIN_AUDIO_S - 0.05, amplitude=0.1)
    _, _, client = _run(["start", "stop"], signal=just_under)
    assert client.decoded == []


def test_audio_just_over_the_minimum_is_decoded():
    just_over = pcm.sine(220.0, dictate.MIN_AUDIO_S + 0.05, amplitude=0.1)
    _, _, client = _run(["start", "stop"], signal=just_over)
    assert len(client.decoded) == 1


def test_cancel_discards_the_recording():
    records, recordings, client = _run(["start", "cancel"])
    assert [r["event"] for r in records] == ["Listening", "Cancelled"]
    assert client.decoded == []
    assert recordings[0].exited


def test_stop_without_start_and_a_second_start_are_ignored():
    records, recordings, _ = _run(["stop", "start", "start", "stop"])
    assert [r["event"] for r in records] == ["Listening", "Transcribing", "Ready", "Dictated"]
    assert len(recordings) == 1


def test_a_full_buffer_stops_and_decodes_by_itself():
    records: list[dict] = []
    client = FakeClient()
    long = pcm.sine(220.0, config.MAX_WINDOW_S + 1.0, amplitude=0.1)
    dictation = dictate.Dictation(
        records.append, client=client, recorder=lambda tap: FakeRecording(tap, long)
    )
    dictation.handle("start")
    dictation.handle(dictation._commands.get_nowait())
    assert records[-1]["event"] == "Dictated"
    assert len(client.decoded) == 1


def test_a_stale_full_signal_does_not_stop_the_next_recording():
    records: list[dict] = []
    dictation = dictate.Dictation(
        records.append,
        client=FakeClient(),
        recorder=lambda tap: FakeRecording(tap, SPEECH),
    )
    dictation.handle("start")
    dictation.handle("stop")
    dictation.handle("start")
    dictation.handle("full 1")
    assert [r["event"] for r in records if r["event"] != "Level"][-1] == "Listening"


class LiveRecording(FakeRecording):
    def __enter__(self):
        self.entered = True
        return self

    def feed(self, signal: np.ndarray) -> None:
        self._signal = np.concatenate([self._signal, signal])
        for chunk in pcm.chunks(signal):
            self._tap(chunk)


def _live():
    records: list[dict] = []
    recordings: list[LiveRecording] = []

    def recorder(tap):
        recordings.append(LiveRecording(tap, np.zeros(0, dtype=np.float32)))
        return recordings[-1]

    client = FakeClient()
    dictation = dictate.Dictation(records.append, client=client, recorder=recorder)
    dictation.handle("start")
    return dictation, recordings[0], records, client


def _talk(seconds: float) -> np.ndarray:
    return pcm.sine(220.0, seconds, amplitude=0.1)


def _quiet(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * config.SAMPLE_RATE_HZ), dtype=np.float32)


def test_hands_free_silence_before_any_speech_never_stops():
    dictation, recording, _, _ = _live()
    dictation.handle("hands-free")
    recording.feed(_quiet(10.0))
    assert dictation._commands.empty()


def test_hands_free_speech_then_the_silence_timeout_stops_and_decodes():
    dictation, recording, records, client = _live()
    dictation.handle("hands-free")
    recording.feed(_talk(1.0))
    recording.feed(_quiet(config.HANDS_FREE_SILENCE_S - 0.1))
    assert dictation._commands.empty()
    recording.feed(_quiet(0.1))
    dictation.handle(dictation._commands.get_nowait())
    assert records[-1]["event"] == "Dictated"
    assert len(client.decoded) == 1


def test_a_held_key_never_stops_on_silence():
    dictation, recording, _, _ = _live()
    recording.feed(_talk(1.0))
    recording.feed(_quiet(10.0))
    assert dictation._commands.empty()


def test_pauses_shorter_than_the_timeout_do_not_stop():
    dictation, recording, _, _ = _live()
    dictation.handle("hands-free")
    pause = config.HANDS_FREE_SILENCE_S - 0.5
    for _ in range(3):
        recording.feed(_talk(1.0))
        recording.feed(_quiet(pause))
    assert dictation._commands.empty()


def test_less_speech_than_the_minimum_does_not_arm_the_timeout():
    dictation, recording, _, _ = _live()
    dictation.handle("hands-free")
    recording.feed(_talk(config.HANDS_FREE_MIN_SPEECH_S - 0.2))
    recording.feed(_quiet(10.0))
    assert dictation._commands.empty()


def test_latching_after_the_silence_has_already_passed_stops_at_once():
    dictation, recording, records, client = _live()
    recording.feed(_talk(1.0))
    recording.feed(_quiet(config.HANDS_FREE_SILENCE_S))
    assert dictation._commands.empty()
    dictation.handle("hands-free")
    dictation.handle(dictation._commands.get_nowait())
    assert records[-1]["event"] == "Dictated"
    assert len(client.decoded) == 1


def test_a_stale_quiet_signal_does_not_stop_the_next_recording():
    records: list[dict] = []
    dictation = dictate.Dictation(
        records.append, client=FakeClient(), recorder=lambda tap: FakeRecording(tap, SPEECH)
    )
    dictation.handle("start")
    dictation.handle("stop")
    dictation.handle("start")
    dictation.handle("quiet 1")
    assert [r["event"] for r in records if r["event"] != "Level"][-1] == "Listening"


def test_a_microphone_that_will_not_open_is_an_error_not_a_crash():
    records, _, _ = _run(["start", "stop"], fail_on_enter=True)
    assert [r["event"] for r in records] == ["Error"]
    assert "pw-record" in records[0]["message"]


def test_an_unreachable_server_names_the_doctor():
    client = FakeClient(error=requests.ConnectionError("refused"))
    records, _, _ = _run(["start", "stop"], client=client)
    assert records[-1]["event"] == "Error"
    assert "vinowhisper-doctor" in records[-1]["message"]


def test_unknown_commands_are_reported():
    records, _, _ = _run(["frobnicate"])
    assert records == [{"event": "Error", "message": "unknown command 'frobnicate'"}]


def test_the_tail_waits_for_audio_after_the_key_comes_up(monkeypatch):
    monkeypatch.setattr(dictate, "TAIL_S", 0.25)

    class Growing:
        reads = 0

        @property
        def captured_s(self) -> float:
            self.reads += 1
            return 1.0 + 0.1 * self.reads

    growing = Growing()
    dictate._wait_for_tail(growing)  # type: ignore[arg-type]
    assert growing.captured_s >= 1.35


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Hello there. ", "Hello there."),
        ("[BLANK_AUDIO]", ""),
        ("(silence)", ""),
        ("you" * 40, "youyouyou"),
        ("I said (quietly) hello", "I said (quietly) hello"),
    ],
)
def test_clean_drops_whisper_non_speech_and_runaway_repeats(raw, expected):
    assert dictate.clean(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hello\x1b[201~\x1b[200~ world", "hello[201~[200~ world"),
        ("sudo\u202e rm", "sudo rm"),
        ("zero\u200bwidth\u200d", "zerowidth"),
        ("line\none\x00", "line one"),
    ],
)
def test_clean_keeps_control_characters_off_the_clipboard(raw, expected):
    assert dictate.clean(raw) == expected


def test_every_field_the_overlay_reads_is_emitted():
    """gui/src/protocol.rs `Dictate` reads these; renaming one blanks the pill silently."""
    records, _, _ = _run(["start", "stop"])
    by_event = {r["event"]: r for r in records}
    assert {"Listening", "Transcribing", "Ready", "Dictated"} <= by_event.keys()
    assert isinstance(by_event["Dictated"]["text"], str)
    assert {"device", "degraded"} <= by_event["Ready"].keys()

    levels: list[dict] = []
    dictation = dictate.Dictation(
        levels.append, client=FakeClient(), recorder=lambda tap: FakeRecording(tap, SPEECH)
    )
    dictation.handle("start")
    assert any(r["event"] == "Level" and isinstance(r["rms"], float) for r in levels)
    dictation.handle("cancel")
    assert levels[-1] == {"event": "Cancelled"}
    assert _run(["bogus"])[0][0].keys() == {"event", "message"}
