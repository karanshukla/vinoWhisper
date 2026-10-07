import json
import re
import sys
from pathlib import Path

from . import config


def read(path: Path | None = None) -> tuple[dict[str, str], str | None]:
    target = path or config.REPLACEMENTS_FILE
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as exc:
        return {}, str(exc)
    if not isinstance(raw, dict):
        return {}, "top level must be an object mapping spoken forms to written forms"
    for key, value in raw.items():
        if not isinstance(value, str):
            return {}, f"the value for {key!r} must be a string"
    return {key: value for key, value in raw.items() if key.split()}, None


def load(path: Path | None = None) -> dict[str, str]:
    table, problem = read(path)
    if problem is not None:
        print(
            f"[vinowhisper] ignoring {path or config.REPLACEMENTS_FILE}: {problem}",
            file=sys.stderr,
        )
    return table


def apply(text: str, table: dict[str, str]) -> str:
    if not table or not text:
        return text
    spoken = {" ".join(key.lower().split()): value for key, value in table.items()}
    keys = sorted(spoken, key=len, reverse=True)
    pattern = "|".join(r"\s+".join(re.escape(word) for word in key.split(" ")) for key in keys)
    matcher = re.compile(rf"(?<!\w)(?:{pattern})(?!\w)", re.IGNORECASE)
    return matcher.sub(lambda m: spoken[" ".join(m.group().lower().split())], text)
