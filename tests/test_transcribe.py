"""vinowhisper-transcribe: a file in, a saved session out, with the server faked.

No decoder is needed for most of this: a 16 kHz WAV is read with the stdlib, and
`which` is injected so the tests decide whether ffmpeg "exists". One test runs the
real ffmpeg when the machine has it.
"""

import json
import shutil
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import requests

from tests import pcm
from vinowhisper import audio, bundle, config, distro, transcribe

RATE = config.SAMPLE_RATE_HZ
NOW = datetime(2026, 11, 9, 14, 5, 12)


def no_tools(name):
    return None


def speech(seconds: float) -> np.ndarray:
    return pcm.sine(440.0, seconds, amplitude=0.1)


def write_wav(path: Path, samples: np.ndarray, rate: int = RATE, channels: int = 1) -> Path:
    data = np.clip(samples * 32767.0, -32768, 32767).astype("<i2")
    if channels > 1:
        data = np.repeat(data, channels)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(data.tobytes())
    return path


def feed(segmenter: transcribe.Segmenter, samples: np.ndarray) -> list[transcribe.Piece]:
    pieces = []
    for chunk in pcm.chunks(samples, transcribe.FRAME_SAMPLES):
        pieces += segmenter.add(chunk)
    return pieces + segmenter.finish()


class FakeClient:
    def __init__(self, texts=None, health=None):
        self.texts = list(texts or [])
        self.health = health or {"device": "NPU", "language": "en", "task": "transcribe"}
        self.seen: list[tuple[int, bool]] = []

    def wait_ready(self):
        return self.health

    def transcribe(self, samples, background=False):
        self.seen.append((samples.size, background))
        index = len(self.seen)
        return (self.texts.pop(0) if self.texts else f"Piece {index} said this."), 0.1


def run(path, client, tmp_path, **kwargs):
    records: list[dict] = []
    saved = transcribe.transcribe_file(
        path,
        client,
        records.append,
        directory=tmp_path / "sessions",
        now=lambda: NOW,
        which=no_tools,
        **kwargs,
    )
    return saved, records


# ── cutting ──────────────────────────────────────────────────────────────────


def test_a_long_recording_is_cut_at_its_quietest_pause_not_at_the_limit():
    """The cut lands inside the one pause the back of the window offers, so no word
    is split, and it is the pause, not the 29.5s ceiling, that decides where."""
    audio = np.concatenate([speech(20.0), pcm.silence(1.0), speech(20.0)])
    first, second = feed(transcribe.Segmenter(), audio)
    assert 20.0 <= first.end_s <= 20.5
    assert 20.5 <= second.start_s <= 21.0
    assert second.end_s == pytest.approx(41.0, abs=0.05)


def test_no_piece_is_longer_than_the_servers_window_even_without_a_pause():
    pieces = feed(transcribe.Segmenter(), speech(95.0))
    assert len(pieces) >= 4
    assert all(piece.samples.size <= config.MAX_WINDOW_S * RATE for piece in pieces)
    assert all(piece.samples.size >= config.FILE_PIECE_MIN_S * RATE for piece in pieces[:-1])


def test_characterization_every_sample_is_decoded_exactly_once():
    """characterization: pieces do not overlap, unlike caption windows.

    Live captions decode overlapping windows and stitch them because they have a
    latency target. A file has none, so each second goes to the model once and
    there is nothing to stitch. "Fixing" this by adding overlap would bring back
    every reprint the stitcher exists to remove, for nothing.
    """
    audio = speech(70.0)
    pieces = feed(transcribe.Segmenter(), audio)
    assert sum(piece.samples.size for piece in pieces) == audio.size
    for before, after in zip(pieces, pieces[1:], strict=False):
        assert after.start_s == pytest.approx(before.end_s)


def test_a_silent_stretch_is_never_sent_and_quiet_edges_are_trimmed():
    audio = np.concatenate([pcm.silence(40.0), speech(5.0), pcm.silence(3.0)])
    pieces = feed(transcribe.Segmenter(), audio)
    assert pieces[0].samples.size == 0
    spoken = [piece for piece in pieces if piece.samples.size]
    assert len(spoken) == 1
    # 5s of sound plus the pad either side, placed where it sits in the file.
    assert spoken[0].samples.size == pytest.approx((5.0 + 2 * config.TRIM_PAD_S) * RATE, rel=0.05)
    assert spoken[0].start_s == pytest.approx(40.0 - config.TRIM_PAD_S, abs=0.11)


def test_quiet_speech_is_still_sent():
    """Only digital-silence levels are skipped: dropping real speech is the worst outcome."""
    quiet = pcm.sine(440.0, 10.0, amplitude=config.SILENCE_RMS_THRESHOLD * 4)
    [piece] = feed(transcribe.Segmenter(), quiet)
    assert piece.samples.size == quiet.size


def test_a_tightened_limit_applies_to_audio_already_buffered():
    """Captions can start mid-import, with most of a long piece already held."""
    segmenter = transcribe.Segmenter()
    audio = speech(40.0)
    pieces = []
    for index, chunk in enumerate(pcm.chunks(audio, transcribe.FRAME_SAMPLES)):
        if index == 250:
            segmenter.limit(config.FILE_PIECE_BUSY_MIN_S, config.FILE_PIECE_BUSY_MAX_S)
        pieces += segmenter.add(chunk)
    pieces += segmenter.finish()
    assert sum(piece.samples.size for piece in pieces) == audio.size
    assert all(piece.samples.size <= config.FILE_PIECE_BUSY_MAX_S * RATE for piece in pieces)


# ── decoding ─────────────────────────────────────────────────────────────────


def test_ffmpeg_is_preferred_and_gstreamer_is_the_fallback(tmp_path):
    path = tmp_path / "a talk: part 1.mp4"
    argv = transcribe.decoder_argv(path, lambda name: f"/usr/bin/{name}")
    assert argv[0] == "ffmpeg"
    # file: keeps the colon in the name from being read as a protocol.
    assert f"file:{path}" in argv
    assert argv[-4:] == [str(RATE), "-f", "f32le", "-"]
    assert "-vn" in argv

    argv = transcribe.decoder_argv(path, lambda name: name if name.startswith("gst") else None)
    assert argv[0] == "gst-launch-1.0"
    assert f"location={path}" in argv
    assert f"audio/x-raw,format=F32LE,channels=1,rate={RATE}" in argv

    assert transcribe.decoder_argv(path, no_tools) is None


def test_a_16k_wav_is_read_without_any_decoder(tmp_path):
    audio = speech(1.25)
    path = write_wav(tmp_path / "memo.wav", audio, channels=2)
    read = np.concatenate(list(transcribe.frames(path, no_tools)))
    assert read.size == audio.size
    assert np.allclose(read, audio, atol=1e-3)


def test_anything_else_without_a_decoder_names_the_package(tmp_path, monkeypatch):
    fedora = distro.Distro(id="fedora", name="Fedora", version="44", family="fedora")
    monkeypatch.setattr(distro, "detect", lambda *args, **kwargs: fedora)
    path = write_wav(tmp_path / "memo.wav", speech(0.5), rate=44_100)
    with pytest.raises(transcribe.TranscribeError, match="sudo dnf install -y ffmpeg-free"):
        list(transcribe.frames(path, no_tools))
    other = tmp_path / "talk.mp3"
    other.write_bytes(b"ID3 not really")
    with pytest.raises(transcribe.TranscribeError, match="ffmpeg or GStreamer"):
        list(transcribe.frames(other, no_tools))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_ffmpeg_resamples_and_downmixes_a_real_file(tmp_path):
    audio = pcm.sine(440.0, 2.0, amplitude=0.1, sample_rate_hz=44_100)
    path = write_wav(tmp_path / "stereo 44k: take 1.wav", audio, rate=44_100, channels=2)
    read = np.concatenate(list(transcribe.frames(path)))
    assert read.size == pytest.approx(2.0 * RATE, abs=RATE * 0.02)
    # ffmpeg's downmix keeps power, so two identical channels come out 3 dB up, not clipped.
    assert 0.065 < float(np.sqrt(np.mean(read**2))) < 0.105
    assert transcribe.probe_duration_s(path) == pytest.approx(2.0, abs=0.05)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_file_ffmpeg_cannot_read_says_why(tmp_path):
    path = tmp_path / "notes.mp3"
    path.write_bytes(b"this is not audio at all" * 50)
    with pytest.raises(transcribe.TranscribeError, match=r"could not decode notes\.mp3"):
        list(transcribe.frames(path))


# ── the session it writes ────────────────────────────────────────────────────


def test_a_file_becomes_a_session_the_sessions_window_can_read(tmp_path):
    audio = np.concatenate([speech(20.0), pcm.silence(4.0), speech(20.0)])
    path = write_wav(tmp_path / "lecture one.wav", audio)
    client = FakeClient(["Welcome to the lecture.", "That was the second part."])
    saved, records = run(path, client, tmp_path)

    assert saved == tmp_path / "sessions" / "2026-11-09-140512.vwsession"
    read = bundle.read(saved)
    assert bundle.verify(read) == []
    assert read.transcript == (
        "vinoWhisper transcript, 2026-11-09 14:05, lecture one.wav, NPU\n\n"
        "[00:00] Welcome to the lecture.\n\n"
        "[00:23] That was the second part.\n"
    )
    assert read.session["duration_s"] == 44
    assert read.session["source"] == "lecture one.wav"
    assert read.session["language"] == "en" and read.session["task"] == "transcribe"
    assert read.session["file"] == {
        "name": "lecture one.wav",
        "sha256": bundle.sha256(path.read_bytes()),
        "bytes": path.stat().st_size,
    }
    entry = read.attestation["files"]["transcript.txt"]
    assert entry["model"] == "openai/whisper-small.en" and entry["device"] == "NPU"
    # Every piece asks the server to let captions and dictation go first.
    assert client.seen and all(background for _, background in client.seen)

    assert records[0]["event"] == "Ready" and records[-1]["event"] == "Done"
    progress = [record for record in records if record["event"] == "Progress"]
    assert progress[-1]["position_s"] == pytest.approx(44.0, abs=0.1)
    assert progress[-1]["duration_s"] == pytest.approx(44.0, abs=0.1)
    assert records[-1]["path"] == str(saved) and records[-1]["words"] == 9


def test_pieces_are_short_while_captions_or_dictation_are_in_use(tmp_path):
    """A caption cycle then never waits behind more than one caption window's decode."""
    path = write_wav(tmp_path / "talk.wav", speech(60.0))
    busy = {"device": "NPU", "language": "en", "task": "transcribe", "live_active": True}
    client = FakeClient(health=busy)
    run(path, client, tmp_path)
    assert all(size <= config.FILE_PIECE_BUSY_MAX_S * RATE for size, _ in client.seen)

    # Captions stop part-way: the pieces go back to full length.
    client = FakeClient(health=dict(busy))
    decode = client.transcribe

    def captions_end(samples, background=False):
        client.health["live_active"] = False
        return decode(samples, background)

    client.transcribe = captions_end
    run(path, client, tmp_path)
    sizes = [size for size, _ in client.seen]
    assert sizes[0] <= config.FILE_PIECE_BUSY_MAX_S * RATE
    assert max(sizes[1:]) >= config.FILE_PIECE_MIN_S * RATE


def test_a_live_session_has_no_file_entry():
    made = bundle.new_attestation(NOW.astimezone(), 5, "system audio", "en", None, "NPU", None)
    assert "file" not in made["session"]


def test_canary_and_the_multilingual_model_are_attested_as_what_ran(tmp_path):
    path = write_wav(tmp_path / "memo.wav", speech(3.0))
    health = {"device": "NPU", "model": config.CANARY_MODEL, "language": "de", "task": "translate"}
    saved, _ = run(path, FakeClient(health=health), tmp_path)
    read = bundle.read(saved)
    assert read.attestation["files"]["transcript.txt"]["model"] == config.CANARY_MODEL_ID
    assert (read.session["language"], read.session["task"]) == ("de", "translate")
    assert (
        transcribe.model_id_for({"language": "fr", "task": "transcribe"})
        == config.MULTILINGUAL_MODEL_ID
    )


def test_a_break_inside_a_piece_is_stamped_by_where_its_words_fall(tmp_path):
    path = write_wav(tmp_path / "long.wav", speech(20.0))
    words = " ".join([f"w{i}" for i in range(79)] + ["end."] + [f"m{i}" for i in range(20)])
    saved, _ = run(path, FakeClient([words]), tmp_path)
    paragraphs = bundle.read(saved).transcript.strip().split("\n\n")[1:]
    assert len(paragraphs) == 2
    # 80 of 100 words into a 20s piece.
    assert paragraphs[1].startswith("[00:16] m0 m1")


def noise(seconds: float) -> np.ndarray:
    """A dog barking or bells: as loud as speech or louder, and not speech."""
    return pcm.sine(900.0, seconds, amplitude=0.3)


def hears_speech(chunk: np.ndarray) -> float:
    """A stand-in for Silero: speech() above is speech, noise() is not."""
    return 0.9 if audio.rms(chunk) < 0.15 else 0.1


def paragraphs_of(saved: Path) -> list[str]:
    return bundle.read(saved).transcript.strip().split("\n\n")[1:]


def test_sound_between_talks_breaks_the_paragraph_like_silence_does(tmp_path):
    """#180: three talks with barking and bells between them, in a 91 s file."""
    track = np.concatenate(
        [speech(8.0), noise(18.0), speech(8.0), noise(30.0), speech(5.0), noise(4.0)]
    )
    path = write_wav(tmp_path / "barking.mp4.wav", track)
    client = FakeClient(["Sourdough needs time.", "Oil the chain.", "Taxes are due."])
    saved, _ = run(path, client, tmp_path, speech=hears_speech)
    assert paragraphs_of(saved) == [
        "[00:00] Sourdough needs time.",
        "[00:25] Oil the chain.",
        "[01:03] Taxes are due.",
    ]
    # Nothing but the talks reached Whisper, which writes sentences for noise.
    assert len(client.seen) == 3
    assert sum(size for size, _ in client.seen) <= (21.0 + 6 * config.TRIM_PAD_S) * RATE


def test_without_a_detector_noise_is_still_sound(tmp_path):
    """characterization: with no speech detector (it could not be fetched), barking counts
    as sound, as before #180. The import still works; only its paragraphs merge."""
    track = np.concatenate([speech(8.0), noise(18.0), speech(8.0)])
    path = write_wav(tmp_path / "barking.wav", track)
    saved, _ = run(path, FakeClient(["One. Two."]), tmp_path)
    assert len(paragraphs_of(saved)) == 1


def test_a_long_silence_inside_a_piece_now_ends_it(tmp_path):
    track = np.concatenate([speech(5.0), pcm.silence(4.0), speech(5.0)])
    path = write_wav(tmp_path / "pause.wav", track)
    client = FakeClient(["Before the pause.", "After the pause."])
    saved, _ = run(path, client, tmp_path)
    assert paragraphs_of(saved) == ["[00:00] Before the pause.", "[00:08] After the pause."]


def test_a_short_pause_keeps_one_piece(tmp_path):
    track = np.concatenate([speech(5.0), pcm.silence(2.0), speech(5.0)])
    path = write_wav(tmp_path / "breath.wav", track)
    client = FakeClient(["All of it."])
    run(path, client, tmp_path, speech=hears_speech)
    assert len(client.seen) == 1


def test_characterization_noise_keeps_the_pieces_long(tmp_path):
    """characterization: non-speech is scaled down, not zeroed. At zero every pause ties,
    the earliest wins, and pieces shrink toward FILE_PIECE_MIN_S: on 19 min of a reading
    that was 63 pieces against 52 and three lines dropped at the extra boundaries."""
    beat = np.concatenate([speech(3.0), noise(0.5)])
    track = np.concatenate([beat] * 17)
    loudness = np.repeat(np.linspace(1.0, 0.5, track.size // 1600), 1600)
    path = write_wav(tmp_path / "talk.wav", track * np.resize(loudness, track.size))
    client = FakeClient()
    run(path, client, tmp_path, speech=hears_speech)
    assert client.seen[0][0] > 25.0 * RATE


def test_repeats_and_non_speech_tags_are_cleaned_like_a_dictation(tmp_path):
    path = write_wav(tmp_path / "memo.wav", np.concatenate([speech(20.0), speech(20.0)]))
    client = FakeClient(["[BLANK_AUDIO]", "you you you you you you fine."])
    saved, _ = run(path, client, tmp_path)
    assert "BLANK" not in bundle.read(saved).transcript
    assert bundle.read(saved).transcript.count("you") <= 3


def test_a_file_with_no_speech_saves_nothing_and_says_so(tmp_path):
    path = write_wav(tmp_path / "quiet.wav", pcm.silence(5.0))
    client = FakeClient()
    with pytest.raises(transcribe.TranscribeError, match="no speech found in quiet.wav"):
        run(path, client, tmp_path)
    assert client.seen == []
    assert not (tmp_path / "sessions").exists()


def test_a_second_import_in_the_same_second_gets_its_own_name(tmp_path):
    path = write_wav(tmp_path / "memo.wav", speech(2.0))
    first, _ = run(path, FakeClient(), tmp_path)
    second, _ = run(path, FakeClient(), tmp_path)
    assert first != second and first.exists() and second.exists()
    assert bundle.is_bundle_name(second)


def test_a_flag_the_server_is_not_running_is_refused_with_how_to_switch(tmp_path):
    """The server holds one model, so a flag is a check, not a switch: transcribing
    French with the English-only model would write a confident wrong transcript."""
    path = write_wav(tmp_path / "memo.wav", speech(2.0))
    client = FakeClient()
    with pytest.raises(transcribe.TranscribeError, match=r"not --language fr.*\n.*Settings"):
        run(path, client, tmp_path, language="fr")
    assert client.seen == []
    saved, _ = run(path, client, tmp_path, language="en", task="transcribe", model="whisper-small")
    assert saved.exists()


def test_a_file_moved_away_during_the_import_is_still_saved(tmp_path):
    """The hash is taken before decoding: a long import must not be lost at the last step."""
    path = write_wav(tmp_path / "memo.wav", speech(2.0))
    expected = bundle.sha256(path.read_bytes())
    client = FakeClient()
    decode = client.transcribe

    def moved_meanwhile(samples, background=False):
        path.unlink(missing_ok=True)
        return decode(samples, background)

    client.transcribe = moved_meanwhile
    saved, _ = run(path, client, tmp_path)
    assert bundle.read(saved).session["file"]["sha256"] == expected


class Flaky(FakeClient):
    def __init__(self, failures, error=None, **kwargs):
        super().__init__(**kwargs)
        self.failures = failures
        self.error = error or requests.ConnectionError("server went away")
        self.readies = 0

    def wait_ready(self):
        self.readies += 1
        return self.health

    def transcribe(self, samples, background=False):
        if self.failures:
            self.failures -= 1
            raise self.error
        return super().transcribe(samples, background)


@pytest.fixture
def no_pause(monkeypatch):
    monkeypatch.setattr(transcribe, "PIECE_RETRY_PAUSE_S", 0.0)


def test_a_piece_is_tried_again_after_the_server_comes_back(tmp_path, no_pause):
    path = write_wav(tmp_path / "memo.wav", speech(2.0))
    client = Flaky(failures=2)
    saved, _ = run(path, client, tmp_path)
    assert bundle.read(saved).transcript.endswith("said this.\n")
    assert len(client.seen) == 1


def test_a_server_that_stays_down_ends_the_import(tmp_path, no_pause):
    path = write_wav(tmp_path / "memo.wav", speech(2.0))
    with pytest.raises(requests.ConnectionError):
        run(path, Flaky(failures=transcribe.PIECE_ATTEMPTS), tmp_path)
    assert not (tmp_path / "sessions").exists()


def test_a_server_error_is_not_called_unreachable(tmp_path, no_pause):
    path = write_wav(tmp_path / "memo.wav", speech(2.0))
    client = Flaky(failures=transcribe.PIECE_ATTEMPTS, error=requests.HTTPError("500 Server Error"))
    with pytest.raises(
        transcribe.TranscribeError, match="server failed on a piece.*\n.*journalctl"
    ):
        run(path, client, tmp_path)


def test_a_model_switched_mid_import_stops_it_instead_of_mixing_two(tmp_path, no_pause):
    """Half a file in English and half through another model is worse than no file."""
    path = write_wav(tmp_path / "memo.wav", speech(2.0))
    client = Flaky(failures=1)
    first = client.wait_ready

    def switched():
        first()
        if client.readies > 1:
            return {**client.health, "language": "fr"}
        return client.health

    client.wait_ready = switched
    with pytest.raises(transcribe.TranscribeError, match="changed during the import"):
        run(path, client, tmp_path)


def test_a_link_keeps_the_name_the_user_gave_it(command, tmp_path, capsys):
    real = write_wav(tmp_path / "0f3a9c.bin.wav", speech(1.0))
    link = tmp_path / "lecture.wav"
    link.symlink_to(real)
    assert transcribe.main([str(link)]) == 0
    saved = Path(capsys.readouterr().out.strip())
    assert bundle.read(saved).session["source"] == "lecture.wav"


def test_a_detector_that_breaks_mid_file_is_dropped_with_one_warning(tmp_path):
    """An inference error at minute 40 must not cost the 40 minutes: the rest of the file
    is cut on silence alone, as it would be with no detector."""
    heard = []

    def breaks(chunk: np.ndarray) -> float:
        heard.append(chunk.size)
        if len(heard) > 50:
            raise RuntimeError("Infer Request was canceled\n  at infer_request.cpp:245")
        return 0.9

    said: list[str] = []
    path = write_wav(tmp_path / "memo.wav", np.concatenate([speech(8.0), speech(8.0)]))
    saved, records = run(path, FakeClient(["One. Two."]), tmp_path, speech=breaks, warn=said.append)
    assert bundle.read(saved).transcript
    assert len(heard) == 51, "not asked again after it failed"
    assert said == [
        "the speech detector stopped at 0:05 (Infer Request was canceled); "
        "the rest of this file breaks paragraphs on silence only"
    ]
    # gui/src/sessions.rs reads this record: "event" and "message".
    assert [r for r in records if r["event"] == "Warning"] == [
        {"event": "Warning", "message": said[0]}
    ]


def test_the_detector_is_built_only_after_the_file_and_server_checks(tmp_path):
    """Its first run downloads the model. A mistyped path or a server holding the wrong
    model must fail before that, not after a fetch."""
    built = []

    def detector(say):
        built.append(True)
        return hears_speech

    with pytest.raises(transcribe.TranscribeError, match="is not a file"):
        run(tmp_path / "typo.wav", FakeClient(), tmp_path, detector=detector)
    path = write_wav(tmp_path / "memo.wav", speech(2.0))
    with pytest.raises(transcribe.TranscribeError):
        run(path, FakeClient(), tmp_path, detector=detector, model=config.CANARY_MODEL)
    assert built == []
    run(path, FakeClient(), tmp_path, detector=detector)
    assert built == [True]


def test_a_detector_that_is_off_is_a_warning_on_the_ready_record(tmp_path):
    """Ready's warnings are the one notice shape the import has; --json readers get the
    detector's absence there instead of on a stderr the sessions window discards."""

    def detector(say):
        say("could not fetch the speech detector (offline); paragraphs break on silence only")
        return None

    path = write_wav(tmp_path / "memo.wav", speech(2.0))
    _, records = run(path, FakeClient(), tmp_path, detector=detector)
    assert records[0]["event"] == "Ready"
    assert records[0]["warnings"] == [
        "could not fetch the speech detector (offline); paragraphs break on silence only"
    ]
    assert records[1] == {
        "event": "Warning",
        "message": "could not fetch the speech detector (offline); paragraphs break on silence only",
    }


# ── the command ──────────────────────────────────────────────────────────────


@pytest.fixture
def command(monkeypatch, tmp_path):
    client = FakeClient()
    monkeypatch.setattr(transcribe, "TranscriptionClient", lambda: client)
    monkeypatch.setattr(transcribe.shutil, "which", no_tools)
    monkeypatch.setattr(transcribe.vad, "detector", lambda say: None)
    kicked = []
    monkeypatch.setattr(transcribe.summary, "queue_auto", lambda: kicked.append(True))
    client.kicked = kicked
    return client


def test_the_command_prints_the_bundle_and_queues_its_summary(command, tmp_path, capsys):
    path = write_wav(tmp_path / "memo.wav", speech(2.0))
    assert transcribe.main([str(path)]) == 0
    out = capsys.readouterr()
    saved = Path(out.out.strip())
    assert saved.parent == config.TRANSCRIPT_DIR and bundle.read(saved).transcript
    assert "transcribing on NPU" in out.err
    assert command.kicked == [True]


def test_json_mode_is_one_record_per_line_ending_in_done(command, tmp_path, capsys):
    path = write_wav(tmp_path / "memo.wav", speech(2.0))
    assert transcribe.main([str(path), "--json", "-o", str(tmp_path / "out")]) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [record["event"] for record in records] == ["Ready", "Progress", "Done"]
    assert Path(records[-1]["path"]).parent == tmp_path / "out"
    # Somewhere the summariser does not look, so there is nothing to queue.
    assert command.kicked == []


def test_a_failure_is_an_error_record_and_a_nonzero_exit(command, tmp_path, capsys):
    assert transcribe.main([str(tmp_path / "missing.mp3"), "--json"]) == 1
    out = capsys.readouterr()
    assert json.loads(out.out)["event"] == "Error"
    assert "is not a file" in out.err


def test_an_unreachable_server_names_the_doctor(command, tmp_path, capsys, monkeypatch):
    def refuse():
        raise requests.ConnectionError("no socket")

    monkeypatch.setattr(command, "wait_ready", refuse)
    path = write_wav(tmp_path / "memo.wav", speech(1.0))
    assert transcribe.main([str(path)]) == 1
    assert "vinowhisper-doctor" in capsys.readouterr().err


def test_cancelling_saves_nothing(command, tmp_path, capsys, monkeypatch):
    def interrupted(samples, background=False):
        raise KeyboardInterrupt

    monkeypatch.setattr(command, "transcribe", interrupted)
    path = write_wav(tmp_path / "memo.wav", speech(1.0))
    assert transcribe.main([str(path)]) == 130
    assert "nothing was saved" in capsys.readouterr().err
    assert not config.TRANSCRIPT_DIR.exists()
