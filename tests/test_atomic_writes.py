"""Each JSON file a reader may see mid-write is renamed into place, never written in place.

The three writers differ on purpose: language.json and failed-devices.json are ordinary
config files (the umask decides), sessions.json is the summariser's private state (0600
in a 0700 directory). These pin each site's mode, bytes and what a failed rename leaves.
"""

import json
import os
import stat

import pytest

from vinowhisper import config, failures, integrity, summary


@pytest.fixture
def umask_022():
    old = os.umask(0o022)
    yield
    os.umask(old)


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def _refuse_rename(monkeypatch):
    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)


def test_the_language_file_is_written_whole_with_the_umasks_mode(tmp_path, umask_022):
    path = tmp_path / "config" / "language.json"
    config.save_language("fr", "translate", path)

    assert path.read_text() == (
        '{\n  "language": "fr",\n  "task": "translate",\n  "model": "whisper-small"\n}\n'
    )
    assert _mode(path) == 0o644
    assert [p.name for p in path.parent.iterdir()] == ["language.json"]


def test_a_failed_rename_keeps_the_old_language_file_and_no_partial(tmp_path, monkeypatch):
    path = tmp_path / "language.json"
    config.save_language("fr", "transcribe", path)
    before = path.read_bytes()
    _refuse_rename(monkeypatch)

    with pytest.raises(OSError):
        config.save_language("de", "transcribe", path)

    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["language.json"]


def test_the_language_file_is_written_through_a_symlink_not_over_it(tmp_path):
    real = tmp_path / "dotfiles" / "language.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = tmp_path / "language.json"
    link.symlink_to(real)

    config.save_language("es", "transcribe", link)

    assert link.is_symlink()
    assert json.loads(real.read_text())["language"] == "es"


def test_the_failed_devices_file_is_written_whole_with_the_umasks_mode(tmp_path, umask_022):
    path = tmp_path / "config" / "failed-devices.json"
    failures.record("NPU", "compile failed", path)

    assert set(json.loads(path.read_text())) == {"NPU"}
    assert path.read_text().endswith("}\n")
    assert _mode(path) == 0o644
    assert [p.name for p in path.parent.iterdir()] == ["failed-devices.json"]


def test_a_failed_rename_keeps_the_old_failed_devices_file(tmp_path, monkeypatch):
    path = tmp_path / "failed-devices.json"
    failures.record("NPU", "compile failed", path)
    before = path.read_bytes()
    _refuse_rename(monkeypatch)

    with pytest.raises(OSError):
        failures.record("GPU", "segfault", path)

    assert path.read_bytes() == before


def test_the_summarisers_state_is_private_whatever_the_umask(tmp_path, umask_022):
    path = tmp_path / "state" / "sessions.json"
    summary.save_state({"since": 1.5}, path)

    assert path.read_text() == '{\n  "since": 1.5\n}\n'
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["sessions.json"]


def test_a_failed_rename_keeps_the_old_state_and_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "sessions.json"
    summary.save_state({"since": 1.0}, path)
    before = path.read_bytes()
    _refuse_rename(monkeypatch)

    with pytest.raises(OSError):
        summary.save_state({"since": 2.0}, path)

    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["sessions.json"]


_KNOWN = (b"abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


def test_a_file_hashes_to_the_known_sha256_whatever_its_size(tmp_path):
    data, digest = _KNOWN
    small = tmp_path / "small"
    small.write_bytes(data)
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    large = tmp_path / "large"
    large.write_bytes(b"\0" * ((1 << 20) + 7))

    assert integrity.sha256_file(small) == digest
    assert integrity.sha256_file(empty) == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert integrity.sha256_and_size(small) == (digest, 3)
    assert integrity.sha256_and_size(large)[1] == (1 << 20) + 7
    assert integrity.sha256_and_size(large)[0] == integrity.sha256_file(large)
