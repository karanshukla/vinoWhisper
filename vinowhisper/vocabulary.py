import sys
from pathlib import Path

from . import config

# The decoder reads the whole prompt before every piece, and it shares Whisper's
# 448 positions with the answer (docs/tray.md, "A personal vocabulary").
MAX_TERMS = 100
MAX_TERM_CHARS = 64


def read(path: Path | None = None) -> tuple[list[str], str | None]:
    target = path or config.VOCABULARY_FILE
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as exc:
        return [], str(exc)
    terms: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        term = " ".join(line.split())
        if not term or term.startswith("#") or len(term) > MAX_TERM_CHARS:
            continue
        # A special token written out would be read as one.
        if "<|" in term or term.casefold() in seen:
            continue
        seen.add(term.casefold())
        terms.append(term)
    problem = None
    if len(terms) > MAX_TERMS:
        problem = f"{len(terms)} terms, only the first {MAX_TERMS} are used"
    return terms[:MAX_TERMS], problem


def stamp(path: Path | None = None) -> tuple[int, int] | None:
    try:
        status = (path or config.VOCABULARY_FILE).stat()
    except OSError:
        return None
    return status.st_mtime_ns, status.st_size


def load(path: Path | None = None) -> list[str]:
    terms, problem = read(path)
    if problem is not None:
        print(f"[vinowhisper] {path or config.VOCABULARY_FILE}: {problem}", file=sys.stderr)
    return terms


def prompt(terms: list[str]) -> str:
    return ", ".join(terms)


def unsupported(kind: str, model: str) -> str | None:
    if model != config.WHISPER_MODEL:
        return f"{model} takes no decoder prompt"
    if kind == "NPU":
        return "OpenVINO GenAI refuses a decoder prompt on the NPU"
    return None
