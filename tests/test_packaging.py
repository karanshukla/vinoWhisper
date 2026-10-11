"""The pieces that name one another across files, held together.

The version lives in pyproject.toml and in each Rust crate's manifest and
lock entries (tray, gui, settings), and each desktop binary's release asset
name in two places (overlay.py and release.yml). Nothing at runtime notices them drifting: a release would simply
publish a download URL that 404s, and the first person to find out would be a
user running `vinowhisper-setup --gui`.
"""

import tomllib
from pathlib import Path

from vinowhisper import overlay

ROOT = Path(__file__).resolve().parent.parent


def _toml(relative: str) -> dict:
    return tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))


# Each crate's own lock entry, and its entry in every lock that depends on it
_CRATE_VERSIONS = [
    ("tray/Cargo.toml", "tray/Cargo.lock", "vinowhisper-tray"),
    ("gui/Cargo.toml", "gui/Cargo.lock", "vinowhisper-gui"),
    ("settings/Cargo.toml", "settings/Cargo.lock", "vinowhisper-settings"),
    (None, "tray/Cargo.lock", "vinowhisper-settings"),
    (None, "gui/Cargo.lock", "vinowhisper-settings"),
]


def _locked(lock: str, name: str) -> str:
    return next(package["version"] for package in _toml(lock)["package"] if package["name"] == name)


def test_python_and_rust_share_one_version():
    version = _toml("pyproject.toml")["project"]["version"]
    for manifest, lock, name in _CRATE_VERSIONS:
        if manifest is not None:
            assert _toml(manifest)["package"]["version"] == version, manifest
        assert _locked(lock, name) == version, f"{name} in {lock}"


def test_a_version_bump_moves_every_one():
    entries = {
        (entry["filename"], entry.get("search", ""))
        for entry in _toml("pyproject.toml")["tool"]["bumpversion"]["files"]
    }
    for manifest, lock, name in _CRATE_VERSIONS:
        for filename in filter(None, (manifest, lock)):
            assert any(f == filename and f'"{name}"' in search for f, search in entries), (
                f"{name} in {filename}"
            )


def test_the_release_uploads_the_assets_setup_downloads():
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    for component in overlay.COMPONENTS:
        assert overlay.asset_name("x86_64", component) in workflow
    assert "scripts/pin_tray_release.py" in workflow


def test_the_pins_ship_in_the_wheel():
    """Without them, `vinowhisper-setup --gui` refuses to download by design."""
    package_data = _toml("pyproject.toml")["tool"]["setuptools"]["package-data"]["vinowhisper"]
    for component in overlay.COMPONENTS:
        assert component.pin_file.name in package_data
    assert "ovfetch_release.json" in package_data
