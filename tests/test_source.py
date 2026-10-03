"""The Hugging Face source every export is made from: a pinned revision, and a
sha256 per file checked before optimum reads any of it.

The export digests (test_integrity.py) depend on the toolchain, so a fresh
install can only ever warn about them. These do not, so a mismatch here is a
hard failure. No test touches the network: the session is a fake.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from vinowhisper import config, source, wizard

ROOT = Path(__file__).resolve().parent.parent


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]


class _Session:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.fetched: list[str] = []

    def get(self, url, stream, timeout):
        assert stream
        name = url.rsplit("/", 1)[1]
        self.fetched.append(url)
        return _Response(self.files[name])


def _pinned(files: dict[str, bytes], revision: str = "a" * 40) -> source.Source:
    return source.Source(
        model_id="org/model",
        revision=revision,
        sha256={name: hashlib.sha256(body).hexdigest() for name, body in files.items()},
        sizes={name: len(body) for name, body in files.items()},
    )


FILES = {"config.json": b'{"a": 1}', "model.safetensors": b"\x00" * 3000}


def test_the_shipped_pin_names_a_commit_and_every_file_the_export_reads():
    pinned = source.load(config.MODEL_ID)
    assert pinned is not None
    # A branch name would move under the pin; only a commit sha cannot.
    assert re.fullmatch(r"[0-9a-f]{40}", pinned.revision)
    for name in (
        "config.json",
        "generation_config.json",
        "model.safetensors",
        "preprocessor_config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.json",
        "merges.txt",
    ):
        assert re.fullmatch(r"[0-9a-f]{64}", pinned.sha256[name]), name
    # One weights format only: the others are another 2.9GB nobody reads.
    assert not {"pytorch_model.bin", "tf_model.h5", "flax_model.msgpack"} & set(pinned.sha256)


def test_the_source_pins_ship_in_the_wheel():
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "model_sources.json" in pyproject["tool"]["setuptools"]["package-data"]["vinowhisper"]


def test_every_download_names_the_pinned_revision(tmp_path):
    pinned = _pinned(FILES)
    session = _Session(FILES)
    source.fetch(pinned, tmp_path, session=session, say=lambda _: None)
    assert session.fetched
    assert all(f"/org/model/resolve/{'a' * 40}/" in url for url in session.fetched)


def test_matching_files_are_kept_and_not_downloaded_again(tmp_path):
    pinned = _pinned(FILES)
    source.fetch(pinned, tmp_path, session=_Session(FILES), say=lambda _: None)
    again = _Session(FILES)
    assert source.fetch(pinned, tmp_path, session=again, say=lambda _: None) == tmp_path
    assert again.fetched == []


def test_a_tampered_download_fails_and_leaves_nothing_to_export(tmp_path):
    pinned = _pinned(FILES)
    tampered = {**FILES, "model.safetensors": b"\x01" * 3000}
    with pytest.raises(source.SourceError, match="model.safetensors"):
        source.fetch(pinned, tmp_path, session=_Session(tampered), say=lambda _: None)
    assert not (tmp_path / "model.safetensors").exists()
    assert not list(tmp_path.glob("*.part"))


def test_a_file_changed_on_disk_is_downloaded_again(tmp_path):
    pinned = _pinned(FILES)
    source.fetch(pinned, tmp_path, session=_Session(FILES), say=lambda _: None)
    (tmp_path / "config.json").write_bytes(b'{"a": 2}')
    session = _Session(FILES)
    source.fetch(pinned, tmp_path, session=session, say=lambda _: None)
    assert [url.rsplit("/", 1)[1] for url in session.fetched] == ["config.json"]
    assert (tmp_path / "config.json").read_bytes() == FILES["config.json"]


def test_a_file_outside_the_pin_refuses_the_directory(tmp_path):
    """The exporter reads the whole directory, so an extra file is as bad as a changed one."""
    pinned = _pinned(FILES)
    (tmp_path / "tokenizer_config.json").write_text("{}")
    with pytest.raises(source.SourceError, match="not in the pin"):
        source.fetch(pinned, tmp_path, session=_Session(FILES), say=lambda _: None)


def test_an_unpinned_model_is_its_own_exit_code(tmp_path, monkeypatch):
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"sources": {}}))
    monkeypatch.setattr(source, "SOURCES_PATH", sources)
    assert source.load("someone/else") is None
    assert source.main(["--model", "someone/else"]) == source.EXIT_UNPINNED


def test_the_export_reads_the_checked_snapshot_offline(monkeypatch, tmp_path, capsys):
    cpu = wizard.devices.Device(name="CPU", kind="CPU", full_name="fake")
    monkeypatch.setattr(wizard.devices, "available", lambda: [cpu])
    monkeypatch.setattr(config, "STATEFUL_MODEL_DIR", tmp_path / "stateful")
    monkeypatch.setattr(wizard, "optimum_cli", lambda: "/venv/bin/optimum-cli")

    wizard.Wizard(dry_run=True).check_model()
    out = capsys.readouterr().out
    pinned = source.load(config.MODEL_ID)
    assert pinned is not None
    export_line = next(line for line in out.splitlines() if "optimum-cli export" in line)
    assert "HF_HUB_OFFLINE=1" in export_line
    assert f"--model {pinned.directory}" in export_line
    assert pinned.revision[:12] in out


def test_the_convert_script_exports_from_the_checked_snapshot():
    script = (ROOT / "scripts/convert_model.sh").read_text(encoding="utf-8")
    assert "-m vinowhisper.source" in script
    assert '--model "$SOURCE"' in script
    assert "HF_HUB_OFFLINE=1" in script
