import configparser
import os
from pathlib import Path

INFO = Path("/.flatpak-info")
# A release's bundle; a build from a checkout is `master` and does not update itself.
RELEASE_BRANCH = "stable"


def info_value(section: str, key: str, info: Path | None = None) -> str | None:
    # A GKeyFile: `=` only, keys as written, and `;` is a list separator, not a comment.
    parser = configparser.RawConfigParser(
        delimiters=("=",), comment_prefixes=("#",), strict=False, interpolation=None
    )
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        parser.read_string((info or INFO).read_text(encoding="utf-8"))
    except (OSError, configparser.Error):
        return None
    return parser.get(section, key, fallback=None) or None


def flatpak_id(info: Path | None = None) -> str | None:
    # The file marks a sandbox: FLATPAK_ID alone can be left in the environment of
    # something started from one.
    info = info or INFO
    if not info.is_file():
        return None
    return os.environ.get("FLATPAK_ID") or info_value("Application", "name", info)


def host_argv(argv: list[str]) -> list[str]:
    return ["flatpak-spawn", "--host", *argv]


def instance_argv(argv: list[str]) -> list[str]:
    # A sandbox of its own, of the newest installed build: what is started plainly dies
    # with the window or command whose sandbox it was started in.
    return ["flatpak-spawn", "--latest-version", *argv]
