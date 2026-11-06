"""Canary 1B v2 as a second speech model (#143). No OpenVINO: features, the tokenizer,
chunking, config and the server's choice. The NPU decode was checked by hand on
2026-11-06 against transformers' generate() (same tokens on a 12 s window)."""

import json
from pathlib import Path

import numpy as np
import pytest

from vinowhisper import canary, config, doctor
from vinowhisper.stitch import Stitcher, compression_ratio

FIXTURES = Path(__file__).parent / "fixtures"


def test_features_match_transformers_parakeet_extractor():
    """canary_features.npz: 0.5 s of two sines and LCG noise, run through
    transformers 5.22.2's ParakeetFeatureExtractor (torch) on 2026-11-06."""
    fixture = np.load(FIXTURES / "canary_features.npz")
    feats, mask = canary.features(fixture["audio"])
    want = fixture["features"]
    assert feats.shape == (1, canary.FRAMES, canary.N_MELS)
    assert int(mask.sum()) == len(want) == fixture["audio"].size // canary.HOP
    np.testing.assert_allclose(feats[0, : len(want)], want, atol=1e-3)


def test_padding_frames_are_zero_and_masked_out():
    feats, mask = canary.features(np.load(FIXTURES / "canary_features.npz")["audio"])
    real = int(mask.sum())
    assert not feats[0, real:].any()
    assert not mask[0, real:].any()


def test_a_full_window_fills_all_but_the_last_frame():
    """The extractor counts len // hop real frames, one short of the 1 + len // hop it
    computes; the encoder must be told, or its output moves by up to 2.5."""
    _, mask = canary.features(np.zeros(int(canary.WINDOW_S * 16000), np.float32) + 0.01)
    assert int(mask.sum()) == canary.FRAMES - 1


def _tokenizer(tmp_path: Path) -> canary.Detokenizer:
    pieces = ["<unk>", "▁Hello", ",", "▁caf", "<0xC3>", "<0xA9>", "▁world"]
    added = [
        {"id": 10, "content": "<|en|>", "special": True},
        {"id": 11, "content": "<|fr|>", "special": True},
    ]
    path = tmp_path / "tokenizer.json"
    path.write_text(
        json.dumps(
            {"model": {"vocab": {p: i for i, p in enumerate(pieces)}}, "added_tokens": added}
        )
    )
    return canary.Detokenizer(path)


def test_decoding_joins_word_pieces_and_skips_special_tokens(tmp_path):
    tok = _tokenizer(tmp_path)
    assert tok.decode([10, 1, 2, 6]) == "Hello, world"


def test_byte_fallback_tokens_become_one_character(tmp_path):
    tok = _tokenizer(tmp_path)
    assert tok.decode([3, 4, 5]) == "café"
    # Half a character decodes to a replacement mark, which the streamer waits out.
    assert tok.decode([3, 4]).endswith("�")


def test_auto_translate_names_a_fixed_non_english_source(tmp_path):
    pieces = [*canary.PROMPT_HEAD, *canary.PROMPT_TAIL, "<|en|>", "<|fr|>"]
    vocab = {p: i for i, p in enumerate(pieces)}
    (tmp_path / "tokenizer.json").write_text(json.dumps({"model": {"vocab": vocab}}))
    t = canary.CanaryTranscriber(language="auto", task="translate")
    t._tok = canary.Detokenizer(tmp_path / "tokenizer.json")
    assert t.prompt()[3:5] == [vocab[f"<|{config.CANARY_AUTO_SOURCE}|>"], vocab["<|en|>"]]
    assert config.CANARY_AUTO_SOURCE != "en"


def test_the_prompt_names_source_and_target(tmp_path):
    pieces = [*canary.PROMPT_HEAD, *canary.PROMPT_TAIL, "<|en|>", "<|fr|>"]
    vocab = {p: i for i, p in enumerate(pieces)}
    (tmp_path / "tokenizer.json").write_text(json.dumps({"model": {"vocab": vocab}}))
    for task, target in (("transcribe", "<|fr|>"), ("translate", "<|en|>")):
        t = canary.CanaryTranscriber(language="fr", task=task)
        t._tok = canary.Detokenizer(tmp_path / "tokenizer.json")
        prompt = t.prompt()
        assert len(prompt) == 9
        assert prompt[3:5] == [vocab["<|fr|>"], vocab[target]]


def test_long_audio_is_cut_into_encoder_windows_at_a_quiet_point():
    rate = canary.SAMPLE_RATE_HZ
    audio = np.full(25 * rate, 0.2, np.float32)
    audio[int(10.5 * rate) : int(10.6 * rate)] = 0.0
    pieces = canary.chunks(audio)
    assert sum(p.size for p in pieces) == audio.size
    assert all(p.size <= canary.WINDOW_S * rate for p in pieces)
    assert abs(pieces[0].size - int(10.55 * rate)) <= rate // 10


def test_a_window_that_fits_is_left_whole():
    audio = np.ones(int(canary.WINDOW_S * canary.SAMPLE_RATE_HZ), np.float32)
    assert [p.size for p in canary.chunks(audio)] == [audio.size]


def test_canary_takes_its_own_languages_and_auto_only_to_translate():
    config.check_language("it", "translate", config.CANARY_MODEL)
    config.check_language("auto", "translate", config.CANARY_MODEL)
    with pytest.raises(ValueError, match="cannot detect"):
        config.check_language("auto", "transcribe", config.CANARY_MODEL)
    with pytest.raises(ValueError):
        config.check_language("it", "transcribe", config.WHISPER_MODEL)
    assert set(config.LANGUAGES) <= set(config.CANARY_LANGUAGES)


def test_canary_has_its_own_export_per_device_class():
    npu = config.model_dir("NPU", model=config.CANARY_MODEL)
    cpu = config.model_dir("CPU", model=config.CANARY_MODEL)
    assert npu == config.CANARY_MODEL_DIR
    assert cpu == config.model_dir("GPU", model=config.CANARY_MODEL) == config.CANARY_FP16_MODEL_DIR
    assert npu != cpu
    assert config.MODEL_DIR not in (npu, cpu)


def test_the_model_round_trips_through_the_language_file(tmp_path):
    path = tmp_path / "language.json"
    config.save_language("de", "translate", path, model=config.CANARY_MODEL)
    assert json.loads(path.read_text())["model"] == config.CANARY_MODEL
    assert config.load_language(path) == ("de", "translate", config.CANARY_MODEL)


@pytest.mark.parametrize(
    ("body", "want"),
    [
        ('{"language": "fr"}', ("fr", "transcribe", "whisper-small")),
        ('{"language": "de", "model": "parakeet"}', ("de", "transcribe", "whisper-small")),
        ('{"language": "it", "model": "whisper-small"}', ("en", "transcribe", "whisper-small")),
        ('{"language": "auto", "model": "canary-1b-v2"}', ("en", "transcribe", "canary-1b-v2")),
        (
            '{"language": "auto", "task": "translate", "model": "canary-1b-v2"}',
            ("auto", "translate", "canary-1b-v2"),
        ),
    ],
)
def test_a_bad_choice_falls_back_without_losing_the_model(tmp_path, body, want):
    path = tmp_path / "language.json"
    path.write_text(body)
    assert config.load_language(path) == want


def test_a_model_flag_drops_a_saved_language_it_lacks(monkeypatch, tmp_path):
    path = tmp_path / "language.json"
    config.save_language("it", "translate", path, model=config.CANARY_MODEL)
    monkeypatch.setattr(config, "LANGUAGE_FILE", path)
    assert config.resolve_language(None, None, config.WHISPER_MODEL) == (
        "en",
        "transcribe",
        "whisper-small",
    )
    assert config.resolve_language(None, None) == ("it", "translate", "canary-1b-v2")


def test_the_server_loads_the_model_the_file_names(monkeypatch, tmp_path):
    server = pytest.importorskip("vinowhisper.server")
    path = tmp_path / "language.json"
    config.save_language("fr", "translate", path, model=config.CANARY_MODEL)
    monkeypatch.setattr(config, "LANGUAGE_FILE", path)
    args = server._parse_args([])
    assert (args.language, args.task, args.model) == ("fr", "translate", "canary-1b-v2")
    assert server._parse_args(["--model", "whisper-small"]).language == "fr"


def test_a_missing_canary_export_names_the_export_command(tmp_path):
    t = canary.CanaryTranscriber(model_dir=tmp_path, device="NPU")
    with pytest.raises(FileNotFoundError, match="export_canary.py"):
        t._check_export("NPU", tmp_path)
    assert t.describe()["model"] == config.CANARY_MODEL


def test_the_doctor_fails_a_selected_canary_with_no_export(monkeypatch, tmp_path):
    path = tmp_path / "language.json"
    config.save_language("en", "transcribe", path, model=config.CANARY_MODEL)
    monkeypatch.setattr(config, "LANGUAGE_FILE", path)
    monkeypatch.setattr(config, "CANARY_MODEL_DIR", tmp_path / "missing")
    monkeypatch.setattr(doctor.devices, "available", lambda: [])
    monkeypatch.setattr(doctor.devices, "select", lambda *a, **k: type("S", (), {"kind": "NPU"})())
    [result] = doctor._speech_model()
    assert result.status == doctor.FAIL
    assert "export_canary.py" in result.detail


# Two consecutive cycles of a 2026-11-06 stitched Canary session at t=270 s and 273.8 s.
_LOOP = (
    "The two of them were very well known, and the two of them were very well known. "
    "The two of them were very well known, and the two of them were very well known."
)


def test_characterization_a_looping_decode_is_dropped_whole():
    """characterization: a decode that compresses past _MAX_COMPRESSION_RATIO pushes
    nothing, not even its first few real words.

    Looks wasteful: the loop often starts with words that were spoken. But two looping
    decodes in a row agree with each other, so LocalAgreement commits the invented
    sentence, which is how Canary went from 5.8% to 10.5% WER on a stitched session.
    Real text peaked at 1.66 for both models; Canary's loops at 2.4-2.9."""
    assert compression_ratio(_LOOP) > 2.0
    stitcher = Stitcher()
    assert stitcher.push(_LOOP) == []
    assert stitcher.push(_LOOP) == []
    assert stitcher.pending == []


def test_ordinary_speech_is_nowhere_near_the_loop_ratio():
    text = (
        "Mr Bennet made no answer. Do you not want to know who has taken it? cried his "
        "wife impatiently. You want to tell me, and I have no objection to hearing it."
    )
    assert compression_ratio(text) < 1.7
    stitcher = Stitcher()
    stitcher.push(text)
    assert stitcher.push(text)


def test_the_pins_name_both_published_exports():
    raw = json.loads(canary.PINS_PATH.read_text())["models"]
    assert set(raw) == {canary.NPU_REPO, canary.FP16_REPO}
    for entry in raw.values():
        assert entry["base"].startswith("nvidia/canary-1b-v2@")
        assert set(entry["files"]) == set(canary._EXPORT_FILES)
        assert len(entry["revision"]) == 40


def _point_at(monkeypatch, tmp_path, repo=canary.NPU_REPO):
    directory = tmp_path / "canary-1b-v2-ov"
    monkeypatch.setattr(canary, "_target", lambda kind=None: (repo, directory))
    return directory


def test_status_before_any_download_offers_it(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)
    status = canary.model_status()
    assert (status["present"], status["verified"], status["bytes"]) == (False, False, 0)
    assert status["download_bytes"] > 900_000_000
    assert status["label"] == "Canary 1B v2"


def test_a_local_export_counts_as_present_but_not_verified(monkeypatch, tmp_path):
    directory = _point_at(monkeypatch, tmp_path)
    directory.mkdir()
    for name in canary._EXPORT_FILES:
        (directory / name).write_bytes(b"x" * 10)
    status = canary.model_status()
    assert (status["present"], status["verified"], status["bytes"]) == (True, False, 50)


def test_fetch_downloads_the_pinned_files_and_marks_them(monkeypatch, tmp_path):
    import hashlib

    from vinowhisper import llm
    from vinowhisper import source as model_source

    blobs = {name: name.encode() * 3 for name in canary._EXPORT_FILES}
    pin = model_source.Source(
        model_id=canary.NPU_REPO,
        revision="r" * 40,
        sha256={n: hashlib.sha256(b).hexdigest() for n, b in blobs.items()},
        sizes={n: len(b) for n, b in blobs.items()},
    )
    monkeypatch.setattr(llm, "pinned", lambda repo, path=None: pin)

    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield self.body

    class Session:
        def get(self, url, **kwargs):
            return Response(blobs[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(model_source.requests, "Session", Session)
    directory = _point_at(monkeypatch, tmp_path)
    seen = []
    canary.model_fetch(lambda done, total: seen.append((done, total)))
    assert seen[-1] == (sum(pin.sizes.values()),) * 2
    status = canary.model_status()
    assert (status["present"], status["verified"]) == (True, True)
    assert {p.name for p in directory.iterdir()} >= set(canary._EXPORT_FILES)


def test_delete_removes_both_exports(monkeypatch, tmp_path):
    npu, cpu = tmp_path / "npu", tmp_path / "cpu"
    for d in (npu, cpu):
        d.mkdir()
        (d / "encoder.xml").write_text("x")
    monkeypatch.setattr(config, "CANARY_MODEL_DIR", npu)
    monkeypatch.setattr(config, "CANARY_FP16_MODEL_DIR", cpu)
    canary.model_delete()
    assert not npu.exists() and not cpu.exists()


def _wizard(monkeypatch, tmp_path, **kwargs):
    from vinowhisper import wizard

    path = tmp_path / "language.json"
    config.save_language("auto", "translate", path, model=config.CANARY_MODEL)
    monkeypatch.setattr(config, "LANGUAGE_FILE", path)
    monkeypatch.setattr(wizard.devices, "select", lambda *a, **k: type("S", (), {"kind": "NPU"})())
    return wizard.Wizard(**kwargs)


def test_setup_checks_the_canary_export_not_whispers(monkeypatch, tmp_path):
    w = _wizard(monkeypatch, tmp_path, dry_run=True)
    seen = []
    monkeypatch.setattr(
        canary,
        "model_status",
        lambda kind=None: (
            seen.append(kind)
            or {
                "dir": "/m/canary-1b-v2-ov",
                "verified": True,
                "present": True,
                "download_bytes": 1,
                "repo": canary.NPU_REPO,
                "revision": "r" * 40,
            }
        ),
    )
    outcome = w.check_model()
    assert outcome.ok is True and "Canary" in outcome.summary
    assert seen == ["NPU"]


def test_setup_offers_the_canary_download_and_fetches_on_yes(monkeypatch, tmp_path):
    w = _wizard(monkeypatch, tmp_path, assume_yes=True)
    monkeypatch.setattr(
        canary,
        "model_status",
        lambda kind=None: {
            "dir": "/m/canary-1b-v2-ov",
            "verified": False,
            "present": False,
            "download_bytes": 972_131_242,
            "repo": canary.NPU_REPO,
            "revision": "r" * 40,
        },
    )
    fetched = []
    monkeypatch.setattr(
        canary, "model_fetch", lambda progress, kind=None: fetched.append(kind) or progress(1, 1)
    )
    outcome = w.check_model()
    assert outcome.ok is True and "downloaded" in outcome.summary
    assert fetched == ["NPU"]


def test_a_dry_run_never_downloads_canary(monkeypatch, tmp_path):
    w = _wizard(monkeypatch, tmp_path, dry_run=True)
    monkeypatch.setattr(
        canary,
        "model_status",
        lambda kind=None: {
            "dir": "/m",
            "verified": False,
            "present": False,
            "download_bytes": 1,
            "repo": canary.NPU_REPO,
            "revision": "r" * 40,
        },
    )
    monkeypatch.setattr(canary, "model_fetch", lambda *a, **k: pytest.fail("downloaded"))
    assert w.check_model().ok is None


def test_setup_model_flag_is_saved_beside_the_language(monkeypatch, tmp_path):
    w = _wizard(monkeypatch, tmp_path, model=config.WHISPER_MODEL)
    assert w.save_language().ok is True
    assert config.load_language() == ("auto", "translate", "whisper-small")
