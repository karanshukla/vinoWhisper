import json
from pathlib import Path

import requests

from . import config, source

PINS_PATH = Path(__file__).resolve().parent / "echo_model.json"
MODEL_DIR = config.MODEL_ROOT / "vinoecho"
GATE_FILE = "v12s_T2.xml"


class EchoModelError(RuntimeError):
    pass


def pinned(path: Path | None = None) -> source.Source | None:
    try:
        raw = json.loads((path or PINS_PATH).read_text(encoding="utf-8"))
        files = raw["files"]
        return source.Source(
            model_id=str(raw["repo"]),
            revision=str(raw.get("revision") or ""),
            recorded=str(raw.get("recorded", "")),
            sha256={name: str(meta["sha256"]) for name, meta in files.items()},
            sizes={name: int(meta["size"]) for name, meta in files.items()},
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def model_path(directory: Path | None = None, session: requests.Session | None = None) -> Path:
    pin = pinned()
    if pin is None:
        raise EchoModelError(f"{PINS_PATH} is missing or unreadable; reinstall vinowhisper")
    directory = directory or MODEL_DIR
    # (pinned by `test_echo_model.py::test_files_that_match_the_pin_are_used_without_the_network`)
    if directory.is_dir() and not source.problems(pin, directory):
        return directory / GATE_FILE
    if not pin.revision:
        raise EchoModelError(
            f"the echo gate model is not published yet, and {directory} does not hold it"
        )
    try:
        source.fetch(pin, directory, session=session, say=lambda line: None)
    except (requests.RequestException, OSError, source.SourceError) as exc:
        raise EchoModelError(f"could not fetch the echo gate model ({exc})") from exc
    return directory / GATE_FILE
