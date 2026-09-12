"""Getting the optional overlay binary onto a machine without trusting the network.

Nothing here touches the network: every download goes through a fake `get`.
The overlay is a Rust binary that never ships on PyPI, so the Python side's
whole job is deciding where it may come from and refusing anything unverified.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

from vinowhisper import __version__, overlay, wizard


def _binary(tmp_path, content=b"\x7fELF a fake overlay"):
    path = tmp_path / overlay.asset_name("x86_64")
    path.write_bytes(content)
    return path


def _pin_file(tmp_path, binary, version=__version__, arch="x86_64"):
    path = tmp_path / "gui_release.json"
    path.write_text(json.dumps(overlay.pin_record(binary, version, arch)), encoding="utf-8")
    return path


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]


def _serving(body: bytes, seen: list | None = None):
    def get(url, **kwargs):
        if seen is not None:
            seen.append((url, kwargs))
        return _Response(body)

    return get


def _pin_for(body: bytes) -> overlay.Pin:
    return overlay.Pin(version=__version__, arch="x86_64", sha256=hashlib.sha256(body).hexdigest())


# --- where it may come from ----------------------------------------------


def test_a_release_pin_names_the_asset_and_its_digest(tmp_path):
    binary = _binary(tmp_path)
    pin = overlay.availability(_pin_file(tmp_path, binary), machine="x86_64")
    assert isinstance(pin, overlay.Pin)
    assert pin.url == (
        "https://github.com/karanshukla/vinoWhisper/releases/download/"
        f"v{__version__}/vinowhisper-gui-x86_64-linux"
    )
    assert pin.sha256 == hashlib.sha256(binary.read_bytes()).hexdigest()


def test_characterization_no_pin_means_no_download(tmp_path):
    """characterization: a package without a pin never downloads the overlay.

    On a source checkout that reads like an unhelpful refusal, and it is the
    point: an unpinned download is an unverified binary installed by a setup
    tool, which is what the pin exists to rule out (the same line
    model_digests.json draws for the model export). The way forward is a
    cargo build, and the message says so. If this goes red, the question is
    whether installing an unverified binary was meant to become acceptable.
    """
    reason = overlay.availability(tmp_path / "absent.json", machine="x86_64")
    assert isinstance(reason, str)
    assert "nothing will be downloaded" in reason
    assert "cargo" in reason


def test_a_pin_from_another_release_is_refused(tmp_path):
    reason = overlay.availability(
        _pin_file(tmp_path, _binary(tmp_path), version="0.0.1"), machine="x86_64"
    )
    assert isinstance(reason, str)
    assert "0.0.1" in reason


def test_other_architectures_are_pointed_at_cargo(tmp_path):
    reason = overlay.availability(_pin_file(tmp_path, _binary(tmp_path)), machine="aarch64")
    assert isinstance(reason, str)
    assert "aarch64" in reason and "cargo" in reason


# --- the download --------------------------------------------------------


def test_a_verified_download_is_installed_executable(tmp_path):
    body = b"\x7fELF the real thing"
    seen: list = []
    dest = overlay.fetch(
        _pin_for(body), tmp_path / "bin" / "vinowhisper-gui", get=_serving(body, seen)
    )
    assert dest.read_bytes() == body
    assert os.access(dest, os.X_OK)
    url, kwargs = seen[0]
    assert url == _pin_for(body).url
    assert kwargs["timeout"], "never without a timeout"


def test_a_mismatched_download_installs_nothing_at_all(tmp_path):
    """Not even the partial file: a half-written binary on PATH is worse
    than none.
    """
    dest = tmp_path / "bin" / "vinowhisper-gui"
    with pytest.raises(overlay.OverlayError, match="nothing was installed"):
        overlay.fetch(_pin_for(b"what was pinned"), dest, get=_serving(b"what arrived"))
    assert list(dest.parent.iterdir()) == []


def test_a_failed_download_leaves_nothing_either(tmp_path):
    def offline(url, **kwargs):
        raise ConnectionError("network unreachable")

    dest = tmp_path / "bin" / "vinowhisper-gui"
    with pytest.raises(overlay.OverlayError, match="could not download"):
        overlay.fetch(_pin_for(b"x"), dest, get=offline)
    assert list(dest.parent.iterdir()) == []


def test_replacing_a_running_copy_goes_through_a_rename(tmp_path):
    """A rename, not a write into the old file, which Linux refuses with
    "Text file busy" while that file is running, as the tray usually is.
    """
    dest = tmp_path / "vinowhisper-gui"
    dest.write_bytes(b"old")
    before = dest.stat().st_ino
    source = tmp_path / "freshly-built"
    source.write_bytes(b"new")

    overlay.install_binary(source, dest)
    assert dest.read_bytes() == b"new"
    assert dest.stat().st_ino != before
    assert os.access(dest, os.X_OK)


# --- the wizard step -----------------------------------------------------


def test_setup_never_downloads_without_a_pin(monkeypatch):
    monkeypatch.setattr(overlay, "installed", lambda bin_dir: None)
    monkeypatch.setattr(overlay, "availability", lambda: "no pin in this build")
    monkeypatch.setattr(overlay, "can_build", lambda: False)
    monkeypatch.setattr(overlay, "fetch", lambda *a, **k: pytest.fail("downloaded without a pin"))

    outcome = wizard.Wizard(assume_yes=True).install_overlay()
    assert outcome.ok is None
    assert "no pin in this build" in outcome.summary


def test_a_digest_mismatch_fails_setup(monkeypatch):
    monkeypatch.setattr(overlay, "installed", lambda bin_dir: None)
    monkeypatch.setattr(overlay, "availability", lambda: _pin_for(b"pinned"))

    def tampered(pin, dest, get=None):
        raise overlay.OverlayError("does not match the sha256 pinned")

    monkeypatch.setattr(overlay, "fetch", tampered)
    outcome = wizard.Wizard(assume_yes=True).install_overlay()
    assert outcome.ok is False


def test_a_copy_in_home_gets_a_launcher_and_a_packaged_one_does_not(monkeypatch, capsys):
    """A distro package ships its own launcher in /usr/share; writing a
    second one into the home directory would only shadow it.
    """
    home_copy = Path.home() / ".local/bin/vinowhisper-gui"
    monkeypatch.setattr(overlay, "installed", lambda bin_dir: home_copy)
    wizard.Wizard(dry_run=True).install_overlay()
    printed = capsys.readouterr().out
    assert f"{home_copy} --install" in printed
    assert f"{home_copy} --autostart" in printed

    packaged = Path("/usr/bin/vinowhisper-gui")
    monkeypatch.setattr(overlay, "installed", lambda bin_dir: packaged)
    wizard.Wizard(dry_run=True).install_overlay()
    printed = capsys.readouterr().out
    assert "--install" not in printed
    assert f"{packaged} --autostart" in printed


def test_declining_an_optional_step_is_not_unfinished_setup(capsys):
    instance = wizard.Wizard(dry_run=True)
    instance.step("Caption overlay", lambda: wizard.Outcome(None, "declined"), optional=True)
    assert instance.skipped == []


def test_setup_gui_runs_the_overlay_step_and_nothing_else(monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(wizard.Wizard, "run_all", lambda self: pytest.fail("ran the full setup"))
    monkeypatch.setattr(
        wizard.Wizard,
        "install_overlay",
        lambda self: ran.append("overlay") or wizard.Outcome(True, "fine"),
    )
    assert wizard.main(["--gui", "--dry-run"]) == 0
    assert ran == ["overlay"]
