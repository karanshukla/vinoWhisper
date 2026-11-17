"""Whether this install is a release or a source build, and the version it reports.

`VERSION` is the number for a release and `dev` for anything else, the same word the Rust
binaries print. `package_version` is always the number: pins and `pip install` requirements
need it even in a source build.
"""

import json
from importlib.metadata import Distribution, PackageNotFoundError
from pathlib import Path

DEV = "dev"
UNKNOWN = "0.0.0+unknown"
NAME = "vinowhisper"

# Written by the Flatpak's release build only (packaging/flatpak), next to its Python.
FLATPAK_STAMP = Path("/app/lib/vinowhisper/release-version")
FLATPAK_INFO = Path("/.flatpak-info")
# Next to the package only in a checkout: a wheel has no pyproject.toml beside it.
CHECKOUT_MARK = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _direct_url(dist: Distribution) -> dict | None:
    text = dist.read_text("direct_url.json")
    if not text:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def installed_from_source(dist: Distribution) -> bool:
    """PEP 610: a local directory (editable or not) or a VCS checkout is a source install.

    A wheel or sdist, by URL or path, and an index install (no `direct_url.json`) are releases.
    """
    record = _direct_url(dist)
    return record is not None and ("dir_info" in record or "vcs_info" in record)


def package_version() -> str:
    try:
        return Distribution.from_name(NAME).version
    except PackageNotFoundError:
        return UNKNOWN


def flatpak_release(stamp: Path = FLATPAK_STAMP, info: Path = FLATPAK_INFO) -> str | None:
    """The version a Flatpak release build stamped, or None for a local Flatpak build.

    Its package is pip-installed from a directory, so the metadata alone says source.
    """
    if not info.is_file():
        return None
    try:
        return stamp.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def resolve(dist: Distribution | None, flatpak: str | None = None, checkout: bool = False) -> str:
    """`checkout`: the code running is a source tree, whatever metadata sits on the path.

    A setuptools `.egg-info` in the tree has no `direct_url.json`, so alone it reads as a release.
    """
    if flatpak is not None:
        return flatpak
    if checkout or dist is None or installed_from_source(dist):
        return DEV
    return dist.version


def _current() -> str:
    try:
        dist: Distribution | None = Distribution.from_name(NAME)
    except PackageNotFoundError:
        dist = None
    return resolve(dist, flatpak_release(), CHECKOUT_MARK.is_file())


VERSION = _current()
