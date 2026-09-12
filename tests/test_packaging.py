"""The pieces that name one another across files, held together.

The version lives in three files (pyproject.toml, gui/Cargo.toml and its lock
entry), and the overlay's release asset name in two (overlay.py and
release.yml). Nothing at runtime notices them drifting: a release would simply
publish a download URL that 404s, and the first person to find out would be a
user running `vinowhisper-setup --gui`.
"""

import tomllib
from pathlib import Path

from vinowhisper import overlay

ROOT = Path(__file__).resolve().parent.parent


def _toml(relative: str) -> dict:
    return tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_python_and_rust_share_one_version():
    version = _toml("pyproject.toml")["project"]["version"]
    cargo = _toml("gui/Cargo.toml")["package"]["version"]
    locked = next(
        package["version"]
        for package in _toml("gui/Cargo.lock")["package"]
        if package["name"] == "vinowhisper-gui"
    )
    assert version == cargo == locked


def test_a_version_bump_moves_all_three():
    files = {entry["filename"] for entry in _toml("pyproject.toml")["tool"]["bumpversion"]["files"]}
    assert {"pyproject.toml", "gui/Cargo.toml", "gui/Cargo.lock"} <= files


def test_the_release_uploads_the_asset_setup_downloads():
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert overlay.asset_name("x86_64") in workflow
    assert "scripts/pin_gui_release.py" in workflow


def test_the_pin_ships_in_the_wheel():
    """Without it, `vinowhisper-setup --gui` refuses to download by design."""
    package_data = _toml("pyproject.toml")["tool"]["setuptools"]["package-data"]["vinowhisper"]
    assert "gui_release.json" in package_data
