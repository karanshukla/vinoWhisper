"""The gate model's pin: used from disk when it matches, never guessed, never fetched unpinned."""

import hashlib
import json

import pytest

from vinowhisper import echo_model


class _NoNetwork:
    def get(self, *args, **kwargs):
        raise AssertionError("fetched")


def _pins(tmp_path, monkeypatch, revision):
    files = {name: b"x" * 10 for name in ("v12s_T2.xml", "v12s_T2.bin")}
    pins = tmp_path / "echo_model.json"
    pins.write_text(
        json.dumps(
            {
                "repo": "someone/echo",
                "revision": revision,
                "files": {
                    name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                    for name, data in files.items()
                },
            }
        )
    )
    monkeypatch.setattr(echo_model, "PINS_PATH", pins)
    return files


def test_files_that_match_the_pin_are_used_without_the_network(tmp_path, monkeypatch):
    files = _pins(tmp_path, monkeypatch, revision=None)
    models = tmp_path / "models"
    models.mkdir()
    for name, data in files.items():
        (models / name).write_bytes(data)

    assert echo_model.model_path(models, _NoNetwork()) == models / "v12s_T2.xml"


def test_an_unpublished_pin_with_no_files_says_so_instead_of_fetching(tmp_path, monkeypatch):
    _pins(tmp_path, monkeypatch, revision=None)

    with pytest.raises(echo_model.EchoModelError, match="not published"):
        echo_model.model_path(tmp_path / "empty", _NoNetwork())


def test_a_file_that_differs_from_the_pin_is_not_used(tmp_path, monkeypatch):
    files = _pins(tmp_path, monkeypatch, revision=None)
    models = tmp_path / "models"
    models.mkdir()
    for name in files:
        (models / name).write_bytes(b"tampered")

    with pytest.raises(echo_model.EchoModelError):
        echo_model.model_path(models, _NoNetwork())


def test_the_shipped_pin_names_both_files_with_real_hashes():
    pin = echo_model.pinned()

    assert pin is not None
    assert set(pin.sha256) == {"v12s_T2.xml", "v12s_T2.bin"}
    assert all(len(digest) == 64 for digest in pin.sha256.values())
