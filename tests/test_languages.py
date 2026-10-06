"""French, German and Spanish: the multilingual model, picked by --language.

Checked 2026-10-06 on the CPU device with OpenVINO/whisper-small-int8-ov and
synthetic (espeak-ng) speech: the pipeline transcribes fr/de/es, takes a forced
language, auto-detects it, and its translate task turns them into English. None
of that touched an NPU, and the NPU export of the multilingual model has never
been built, so the pins in model_digests.json do not cover it yet.
"""

import pytest

from vinowhisper import config, doctor, integrity, wizard
from vinowhisper import source as model_source
from vinowhisper.transcriber import WhisperTranscriber


def test_english_keeps_the_english_only_model():
    assert not config.is_multilingual("en")
    assert config.model_dir("NPU") == config.MODEL_DIR
    assert config.model_dir("CPU") == config.STATEFUL_MODEL_DIR
    assert config.model_id() == config.MODEL_ID


@pytest.mark.parametrize("language", ["fr", "de", "es", "auto"])
def test_any_other_language_needs_the_multilingual_model(language):
    assert config.is_multilingual(language)
    assert config.model_dir("NPU", True) == config.MULTILINGUAL_MODEL_DIR
    assert config.model_dir("GPU", True) == config.MULTILINGUAL_STATEFUL_MODEL_DIR
    assert config.MULTILINGUAL_MODEL_DIR != config.MODEL_DIR


def test_the_multilingual_exports_never_share_a_directory_with_the_english_ones():
    dirs = {
        config.MODEL_DIR,
        config.STATEFUL_MODEL_DIR,
        config.MULTILINGUAL_MODEL_DIR,
        config.MULTILINGUAL_STATEFUL_MODEL_DIR,
    }
    assert len(dirs) == 4


@pytest.mark.parametrize(
    ("language", "task"),
    [("xx", "transcribe"), ("fr", "summarise"), ("en", "translate")],
)
def test_nonsense_choices_are_refused(language, task):
    with pytest.raises(ValueError):
        config.check_language(language, task)


@pytest.mark.parametrize("language", ["fr", "de", "es", "auto"])
def test_translate_is_allowed_from_any_foreign_language(language):
    config.check_language(language, "translate")


def test_the_english_model_gets_no_language_or_task_tokens():
    # whisper-small.en has neither, and the pipeline refuses them.
    assert WhisperTranscriber(language="en").generate_options() == {}


def test_a_named_language_is_forced_with_its_whisper_token():
    options = WhisperTranscriber(language="fr").generate_options()
    assert options == {"task": "transcribe", "language": "<|fr|>"}


def test_auto_leaves_detection_to_the_model():
    assert WhisperTranscriber(language="auto").generate_options() == {"task": "transcribe"}


def test_translate_is_passed_through():
    options = WhisperTranscriber(language="de", task="translate").generate_options()
    assert options == {"task": "translate", "language": "<|de|>"}


def test_the_transcriber_loads_the_multilingual_export_and_names_it_when_missing(tmp_path):
    transcriber = WhisperTranscriber(language="es")
    with pytest.raises(FileNotFoundError) as excinfo:
        transcriber._check_export("CPU", tmp_path / "absent")
    message = str(excinfo.value)
    assert "no stateful model export" in message
    assert config.export_command("stateful", multilingual=True) in message


def test_describe_reports_the_language_and_task():
    info = WhisperTranscriber(language="fr", task="translate").describe()
    assert (info["language"], info["task"]) == ("fr", "translate")


def test_the_token_cap_has_more_room_for_multilingual_speech_and_stays_below_the_limit():
    english = config.max_new_tokens(config.WINDOW_S)
    foreign = config.max_new_tokens(config.WINDOW_S, multilingual=True)
    assert foreign > english
    assert foreign < config.WHISPER_MAX_TOKENS / 2
    assert config.max_new_tokens(config.MAX_WINDOW_S, True) <= config.WHISPER_MAX_TOKENS


def test_the_server_takes_language_and_task(monkeypatch, tmp_path):
    # Flask is a runtime dependency, not a dev one, so CI has no server module.
    server = pytest.importorskip("vinowhisper.server")
    monkeypatch.setattr(config, "LANGUAGE_FILE", tmp_path / "absent.json")
    args = server._parse_args(["--language", "de", "--task", "translate"])
    assert (args.language, args.task) == ("de", "translate")
    defaults = server._parse_args([])
    assert (defaults.language, defaults.task) == ("en", "transcribe")


def test_the_server_rejects_translating_english(capsys):
    server = pytest.importorskip("vinowhisper.server")
    with pytest.raises(SystemExit):
        server._parse_args(["--language", "en", "--task", "translate"])
    assert "translate" in capsys.readouterr().err


def test_the_unit_never_carries_the_language():
    # The choice lives in language.json, so the tray can change it without editing a unit.
    service, _socket = wizard.unit_files("auto")
    assert "--language" not in service
    assert "--task" not in service


def test_the_language_file_round_trips(tmp_path):
    path = tmp_path / "vinowhisper/language.json"
    config.save_language("fr", "translate", path)
    assert config.load_language(path) == ("fr", "translate")


@pytest.mark.parametrize(
    "text",
    ["", "not json", "[]", '{"language": "xx"}', '{"language": "en", "task": "translate"}'],
)
def test_a_missing_or_bad_language_file_means_english_not_a_dead_server(tmp_path, text):
    path = tmp_path / "language.json"
    if text:
        path.write_text(text)
    assert config.load_language(path) == ("en", "transcribe")


def test_flags_beat_the_file(monkeypatch, tmp_path):
    path = tmp_path / "language.json"
    config.save_language("fr", "translate", path)
    monkeypatch.setattr(config, "LANGUAGE_FILE", path)
    assert config.resolve_language(None, None) == ("fr", "translate")
    assert config.resolve_language("de", None) == ("de", "transcribe")
    assert config.resolve_language(None, "transcribe") == ("fr", "transcribe")
    assert config.resolve_language("en", None) == ("en", "transcribe")


def test_the_server_reads_the_saved_choice(monkeypatch, tmp_path):
    server = pytest.importorskip("vinowhisper.server")
    path = tmp_path / "language.json"
    config.save_language("es", "translate", path)
    monkeypatch.setattr(config, "LANGUAGE_FILE", path)
    args = server._parse_args([])
    assert (args.language, args.task) == ("es", "translate")


def test_setup_writes_the_language_only_when_asked(monkeypatch, tmp_path):
    path = tmp_path / "language.json"
    monkeypatch.setattr(config, "LANGUAGE_FILE", path)
    assert wizard.Wizard(assume_yes=True).save_language().ok is True
    assert not path.exists()
    explicit = wizard.Wizard(assume_yes=True, language="de", task="translate")
    assert explicit.save_language().ok is True
    assert config.load_language(path) == ("de", "translate")
    assert explicit.multilingual


def test_a_plain_rerun_keeps_what_the_tray_chose(monkeypatch, tmp_path):
    path = tmp_path / "language.json"
    config.save_language("fr", "transcribe", path)
    monkeypatch.setattr(config, "LANGUAGE_FILE", path)
    rerun = wizard.Wizard()
    assert (rerun.language, rerun.multilingual) == ("fr", True)


def test_the_wizard_exports_the_multilingual_model_for_a_foreign_language(tmp_path):
    argv = wizard.export_argv("npu", multilingual=True)
    assert config.MULTILINGUAL_MODEL_ID in argv
    assert str(config.MULTILINGUAL_MODEL_DIR) == argv[-1]
    assert "--disable-stateful" in argv
    stateful = wizard.export_argv("stateful", multilingual=True)
    assert stateful[-1] == str(config.MULTILINGUAL_STATEFUL_MODEL_DIR)


def test_the_english_export_command_is_unchanged():
    english = wizard.export_argv("npu")
    assert config.MODEL_ID in english
    assert english[-1] == str(config.MODEL_DIR)


def test_the_multilingual_source_is_pinned_like_the_english_one():
    source = model_source.load(config.MULTILINGUAL_MODEL_ID)
    assert source is not None
    assert len(source.revision) == 40
    assert "model.safetensors" in source.sha256
    assert all(len(digest) == 64 for digest in source.sha256.values())


def test_an_unpinned_multilingual_export_is_flagged_not_failed(tmp_path):
    # Nobody has exported it on the pinned toolchain yet (see the module docstring).
    result = integrity.verify(tmp_path, "npu", config.MULTILINGUAL_MODEL_ID)
    assert result.status == integrity.UNPINNED
    assert not result.severe


def test_the_doctor_ignores_an_export_nobody_made(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MULTILINGUAL_MODEL_DIR", tmp_path / "a")
    monkeypatch.setattr(config, "MULTILINGUAL_STATEFUL_MODEL_DIR", tmp_path / "b")
    labels = [result.label for result in doctor._models()]
    assert not [label for label in labels if "multilingual" in label]
