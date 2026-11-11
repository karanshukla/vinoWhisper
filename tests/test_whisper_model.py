import json

import pytest

from vinowhisper import config, source, whisper_model


@pytest.fixture
def models(monkeypatch, tmp_path):
    root = tmp_path / "models"
    for name in ("MODEL_DIR", "STATEFUL_MODEL_DIR", "MULTILINGUAL_MODEL_DIR"):
        monkeypatch.setattr(config, name, root / getattr(config, name).name)
    monkeypatch.setattr(
        config,
        "MULTILINGUAL_STATEFUL_MODEL_DIR",
        root / config.MULTILINGUAL_STATEFUL_MODEL_DIR.name,
    )
    monkeypatch.setattr(source, "SOURCE_ROOT", root / "source")
    stops = []
    monkeypatch.setattr(whisper_model, "stop_server", lambda: stops.append(1) or True)
    return root, stops


def _put(directory, name, size):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(b"x" * size)


def test_status_counts_exports_and_the_source_apart(models):
    root, _ = models
    _put(root / "whisper-small.en-ov-stateful", "openvino_encoder_model.xml", 10)
    _put(root / "whisper-small.en-ov-stateful", "openvino_encoder_model.bin", 90)
    _put(root / "source/openai--whisper-small.en@e872", "model.safetensors", 500)
    _put(root / "source/nvidia--canary-1b-v2@abc", "model.safetensors", 7000)
    # A directory with no IR in it is not an export.
    _put(root / "whisper-small-ov", "notes.txt", 3)
    status = whisper_model.model_status()
    assert status["present"] is True
    assert status["bytes"] == 100
    assert status["source_bytes"] == 500
    assert status["exports"] == ["whisper-small.en-ov-stateful"]
    assert status["in_use"] is True


def test_nothing_on_disk_is_not_present(models):
    status = whisper_model.model_status()
    assert (status["present"], status["bytes"], status["source_bytes"]) == (False, 0, 0)


def test_delete_removes_every_export_and_whispers_source_only(models):
    root, stops = models
    for name in (
        "whisper-small.en-ov",
        "whisper-small.en-ov-stateful",
        "whisper-small-ov",
        "whisper-small-ov-stateful",
    ):
        _put(root / name, "openvino_decoder_model.xml", 1)
    _put(root / "source/openai--whisper-small.en@e872", "config.json", 1)
    _put(root / "source/openai--whisper-small@abcd", "config.json", 1)
    _put(root / "source/nvidia--canary-1b-v2@abc", "config.json", 1)
    _put(root / "canary-1b-v2-ov", "encoder.xml", 1)
    _put(root / "SmolLM3-3B-int4-cw-ov", "openvino_model.xml", 1)
    assert whisper_model.model_delete() is True
    assert stops == [1], "the server ran Whisper, so it is stopped"
    assert sorted(p.name for p in root.iterdir()) == [
        "SmolLM3-3B-int4-cw-ov",
        "canary-1b-v2-ov",
        "source",
    ]
    assert [p.name for p in (root / "source").iterdir()] == ["nvidia--canary-1b-v2@abc"]


def test_delete_leaves_a_canary_server_running(models, monkeypatch, tmp_path):
    root, stops = models
    path = tmp_path / "language.json"
    config.save_language("auto", "translate", path, model=config.CANARY_MODEL)
    monkeypatch.setattr(config, "LANGUAGE_FILE", path)
    _put(root / "whisper-small.en-ov", "openvino_decoder_model.xml", 1)
    assert whisper_model.model_delete() is False
    assert stops == []
    assert not (root / "whisper-small.en-ov").exists()


def test_deleting_the_source_keeps_the_export_and_the_server(models):
    root, stops = models
    _put(root / "whisper-small.en-ov", "openvino_decoder_model.xml", 1)
    _put(root / "source/openai--whisper-small.en@e872", "config.json", 1)
    whisper_model.delete_source()
    assert stops == []
    assert (root / "whisper-small.en-ov").is_dir()
    assert not (root / "source").exists(), "left empty, the source root goes too"


def test_the_cli_prints_json_lines(models, capsys):
    root, _ = models
    _put(root / "whisper-small.en-ov", "openvino_decoder_model.xml", 4)
    assert whisper_model.main(["model", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["bytes"] == 4
    assert whisper_model.main(["model", "delete"]) == 0
    assert json.loads(capsys.readouterr().out) == {"event": "deleted", "server_stopped": True}
    assert whisper_model.main(["model", "delete-source"]) == 0
    assert json.loads(capsys.readouterr().out) == {"event": "deleted"}


def test_a_failed_delete_is_an_error_not_a_success(models, monkeypatch, capsys):
    root, _ = models
    _put(root / "whisper-small.en-ov", "openvino_decoder_model.xml", 1)

    def refuse(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(whisper_model.shutil, "rmtree", refuse)
    assert whisper_model.main(["model", "delete"]) == 1
    line = json.loads(capsys.readouterr().out)
    assert line["event"] == "error" and "Permission denied" in line["message"]


def test_stop_server_asks_systemd_for_the_service_only(monkeypatch):
    seen = []

    class Done:
        returncode = 0

    monkeypatch.setattr(
        whisper_model.subprocess, "run", lambda argv, **kw: seen.append(argv) or Done()
    )
    assert whisper_model.stop_server() is True
    assert seen == [["systemctl", "--user", "stop", "vinowhisper-server.service"]]

    def missing(argv, **kw):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(whisper_model.subprocess, "run", missing)
    assert whisper_model.stop_server() is False
