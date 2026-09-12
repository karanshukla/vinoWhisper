"""Finding, fetching and verifying the optional caption overlay.

`vinowhisper-gui` is a Rust binary (gui/), and it deliberately does not ship
on PyPI: a Rust GUI has no business inside a Python wheel, and the terminal
tool must stay installable without it. So `vinowhisper-setup --gui` gets it
from one of three places, in this order:

1. **Already installed.** A distro package in /usr/bin, or an earlier run of
   this into ~/.local/bin.
2. **The GitHub release matching this package's version**, checked against
   the sha256 the release workflow wrote into this wheel (gui_release.json)
   before building it. The wheel on PyPI and the binary on GitHub come out of
   one workflow run, so the pin is exactly as trustworthy as the wheel.
3. **A cargo build**, when this is a git checkout and cargo is installed.

A download that does not match its pin installs nothing. A wheel with no pin
(a source checkout, or one built outside the release workflow) never
downloads at all: an unverified binary installed by a setup tool is exactly
what the pin exists to rule out, the same line model_digests.json draws for
the model export.
"""

import hashlib
import json
import os
import platform
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__, config

BINARY = "vinowhisper-gui"
PIN_FILE = Path(__file__).resolve().parent / "gui_release.json"
RELEASES_URL = "https://github.com/karanshukla/vinoWhisper/releases/download"
GUI_MANIFEST = Path(__file__).resolve().parent.parent / "gui" / "Cargo.toml"

# About 6MB, over whatever connection someone happens to be running setup on.
DOWNLOAD_TIMEOUT_S = 120.0

_BUILD_INSTEAD = "build it from a checkout with cargo instead (docs/gui.md)"


class OverlayError(Exception):
    """A download or install that did not happen, and what to do instead."""


def asset_name(arch: str) -> str:
    """The release asset for one architecture. release.yml uploads it under
    the same name, and tests/test_packaging.py holds the two together, since
    drift here is a 404 discovered by a user rather than by CI.
    """
    return f"{BINARY}-{arch}-linux"


@dataclass(frozen=True)
class Pin:
    version: str
    arch: str
    sha256: str

    @property
    def name(self) -> str:
        return asset_name(self.arch)

    @property
    def url(self) -> str:
        return f"{RELEASES_URL}/v{self.version}/{self.name}"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def pin_record(binary: Path, version: str, arch: str) -> dict[str, Any]:
    """What scripts/pin_gui_release.py writes into gui_release.json."""
    return {
        "version": version,
        "assets": {arch: {"name": asset_name(arch), "sha256": sha256_of(binary)}},
    }


def availability(
    pin_file: Path = PIN_FILE, machine: str | None = None, version: str = __version__
) -> Pin | str:
    """The pinned download for this machine, or a sentence saying why there
    is none and what to do instead.
    """
    arch = machine or platform.machine()
    try:
        record = json.loads(pin_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (
            "this vinowhisper has no pinned overlay binary (a source checkout, or a "
            "wheel built outside the release workflow), so nothing will be downloaded; "
            + _BUILD_INSTEAD
        )
    except (OSError, ValueError) as exc:
        return f"{pin_file} is unreadable ({exc}), so nothing will be downloaded; {_BUILD_INSTEAD}"
    if record.get("version") != version:
        return (
            f"the overlay pin is for vinowhisper {record.get('version')}, not {version}, "
            f"so nothing will be downloaded; {_BUILD_INSTEAD}"
        )
    asset = record.get("assets", {}).get(arch) or {}
    if not asset.get("sha256"):
        return f"there is no prebuilt overlay for {arch}; {_BUILD_INSTEAD}"
    return Pin(version=version, arch=arch, sha256=str(asset["sha256"]))


def installed(bin_dir: Path) -> Path | None:
    """An overlay already on this machine: on PATH, or where setup puts it."""
    found = shutil.which(BINARY)
    if found:
        return Path(found)
    candidate = bin_dir / BINARY
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    return None


def can_build() -> bool:
    return GUI_MANIFEST.is_file() and shutil.which("cargo") is not None


def build_argv() -> list[str]:
    return ["cargo", "build", "--release", "--locked", "--manifest-path", str(GUI_MANIFEST)]


def built_binary() -> Path:
    return GUI_MANIFEST.parent / "target" / "release" / BINARY


def install_binary(source: Path, dest: Path) -> Path:
    """Copy a local build into place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    shutil.copyfile(source, partial)
    return _commit(partial, dest)


def fetch(pin: Pin, dest: Path, get: Callable[..., Any] | None = None) -> Path:
    """Download the pinned release binary to `dest`, or install nothing."""
    if get is None:
        # Here rather than at the top, so scripts/pin_gui_release.py can use
        # this module on a bare release runner with the standard library alone.
        import requests

        get = requests.get
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    try:
        with get(
            pin.url, stream=True, timeout=(config.CONNECT_TIMEOUT_S, DOWNLOAD_TIMEOUT_S)
        ) as response:
            response.raise_for_status()
            with partial.open("wb") as out:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    digest.update(chunk)
                    out.write(chunk)
    except OSError as exc:  # requests' exceptions are OSErrors too
        partial.unlink(missing_ok=True)
        raise OverlayError(f"could not download {pin.url}: {exc}") from exc

    actual = digest.hexdigest()
    if actual != pin.sha256:
        partial.unlink(missing_ok=True)
        raise OverlayError(
            f"{pin.name} does not match the sha256 pinned in vinowhisper {pin.version}, "
            "so nothing was installed.\n"
            f"    expected {pin.sha256}\n"
            f"    got      {actual}\n"
            "  Please report it at https://github.com/karanshukla/vinoWhisper/issues. "
            "A cargo build from a checkout avoids the download (docs/gui.md)."
        )
    return _commit(partial, dest)


def _commit(partial: Path, dest: Path) -> Path:
    """Into place by rename. Writing over the old file instead would fail with
    "Text file busy" whenever the overlay is running, and at login it
    usually is.
    """
    partial.chmod(0o755)  # nosec B103 - a program has to be executable
    os.replace(partial, dest)
    return dest
