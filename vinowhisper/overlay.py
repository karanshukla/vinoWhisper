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

RELEASES_URL = "https://github.com/karanshukla/vinoWhisper/releases/download"
_PACKAGE = Path(__file__).resolve().parent

DOWNLOAD_TIMEOUT_S = 120.0

_BUILD_INSTEAD = "build it from a checkout with cargo instead (docs/tray.md)"


class OverlayError(Exception):
    pass


@dataclass(frozen=True)
class Component:
    binary: str
    pin_file: Path
    manifest: Path


TRAY = Component(
    "vinowhisper-tray", _PACKAGE / "tray_release.json", _PACKAGE.parent / "tray" / "Cargo.toml"
)
# The settings window: a glibc build, since egui cannot run from a static musl one.
SETTINGS = Component(
    "vinowhisper-gui", _PACKAGE / "gui_release.json", _PACKAGE.parent / "gui" / "Cargo.toml"
)
COMPONENTS = (TRAY, SETTINGS)


def asset_name(arch: str, component: Component = TRAY) -> str:
    return f"{component.binary}-{arch}-linux"


def component_for_asset(name: str, arch: str) -> Component:
    for component in COMPONENTS:
        if asset_name(arch, component) == name:
            return component
    expected = ", ".join(asset_name(arch, component) for component in COMPONENTS)
    raise OverlayError(f"{name} is not a release asset; expected one of {expected}")


@dataclass(frozen=True)
class Pin:
    version: str
    arch: str
    sha256: str
    component: Component = TRAY

    @property
    def name(self) -> str:
        return asset_name(self.arch, self.component)

    @property
    def url(self) -> str:
        return f"{RELEASES_URL}/v{self.version}/{self.name}"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def pin_record(
    binary: Path, version: str, arch: str, component: Component = TRAY
) -> dict[str, Any]:
    return {
        "version": version,
        "assets": {arch: {"name": asset_name(arch, component), "sha256": sha256_of(binary)}},
    }


def availability(
    pin_file: Path | None = None,
    machine: str | None = None,
    version: str = __version__,
    component: Component = TRAY,
) -> Pin | str:
    pin_file = pin_file or component.pin_file
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
    return Pin(version=version, arch=arch, sha256=str(asset["sha256"]), component=component)


def installed(bin_dir: Path, component: Component = TRAY) -> Path | None:
    found = shutil.which(component.binary)
    # The settings window's name was the tray's until 1.0, and a symlink to it may be left over.
    if found and not (component is SETTINGS and _is_tray(Path(found))):
        return Path(found)
    candidate = bin_dir / component.binary
    if candidate.is_file() and os.access(candidate, os.X_OK) and not _is_tray(candidate):
        return candidate
    return None


def _is_tray(path: Path) -> bool:
    return path.is_symlink() and path.resolve().name == TRAY.binary


def can_build(component: Component = TRAY) -> bool:
    return component.manifest.is_file() and shutil.which("cargo") is not None


def build_argv(component: Component = TRAY) -> list[str]:
    return ["cargo", "build", "--release", "--locked", "--manifest-path", str(component.manifest)]


def built_binary(component: Component = TRAY) -> Path:
    return component.manifest.parent / "target" / "release" / component.binary


def install_binary(source: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    shutil.copyfile(source, partial)
    return _commit(partial, dest)


def fetch(pin: Pin, dest: Path, get: Callable[..., Any] | None = None) -> Path:
    return download_verified(
        pin.url,
        pin.sha256,
        dest,
        f"{pin.name} does not match the sha256 pinned in vinowhisper {pin.version}",
        "Please report it at https://github.com/karanshukla/vinoWhisper/issues. "
        "A cargo build from a checkout avoids the download (docs/tray.md).",
        get,
    )


def download_verified(
    url: str,
    sha256: str,
    dest: Path,
    mismatch: str,
    advice: str,
    get: Callable[..., Any] | None = None,
) -> Path:
    if get is None:
        # Lazy, so scripts/pin_tray_release.py runs on the standard library alone.
        import requests

        get = requests.get
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    try:
        with get(
            url, stream=True, timeout=(config.CONNECT_TIMEOUT_S, DOWNLOAD_TIMEOUT_S)
        ) as response:
            response.raise_for_status()
            with partial.open("wb") as out:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    digest.update(chunk)
                    out.write(chunk)
    except OSError as exc:  # requests' exceptions are OSErrors too
        partial.unlink(missing_ok=True)
        raise OverlayError(f"could not download {url}: {exc}") from exc

    actual = digest.hexdigest()
    if actual != sha256:
        partial.unlink(missing_ok=True)
        raise OverlayError(
            f"{mismatch}, so nothing was installed.\n"
            f"    expected {sha256}\n"
            f"    got      {actual}\n"
            f"  {advice}"
        )
    return _commit(partial, dest)


def _commit(partial: Path, dest: Path) -> Path:
    partial.chmod(0o755)  # nosec B103 - a program has to be executable
    # Rename, not overwrite: writing over a running binary fails with "Text file busy".
    os.replace(partial, dest)
    return dest
