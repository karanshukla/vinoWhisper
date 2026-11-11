import re
import sys
from difflib import SequenceMatcher
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


def unprompted(
    kind: str, model: str, step_export: bool = False, language: str = config.DEFAULT_LANGUAGE
) -> str | None:
    if model != config.WHISPER_MODEL:
        return f"{model} takes no decoder prompt"
    if kind == "NPU" and not step_export:
        return "OpenVINO GenAI refuses a decoder prompt on the NPU (scripts/export_whisper.py lifts it)"
    # Our loop builds the prompt itself, and auto-detect has no language token to put in it.
    if kind == "NPU" and language == config.AUTO_LANGUAGE:
        return (
            "on the NPU the prompt needs a named language, and it is set to auto "
            "(pick one in Settings > Speech)"
        )
    return None


_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")
# Below this a decode that is nothing but listed terms is taken for the prompt talking.
ECHO_SIMILARITY = 0.4


def _distance(a: str, b: str, limit: int) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        diagonal, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            diagonal, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, diagonal + (x != y))
    return row[-1]


def _slack(term: str) -> int:
    return 0 if len(term) < 5 else 1 if len(term) < 8 else 2


# A word one or two letters off a listed term is that term misspelled (docs/tray.md).
def correct(text: str, terms: list[str]) -> str:
    single = [(term.casefold(), term) for term in terms if " " not in term and _slack(term)]
    if not single or not text:
        return text
    listed = {term.casefold() for term in terms}

    def fix(match: re.Match[str]) -> str:
        word = match.group(0)
        stem = word[:-2] if word.casefold().endswith("'s") else word
        folded = stem.casefold()
        if folded in listed or len(folded) < 4:
            return word
        best: tuple[int, str] | None = None
        for key, term in single:
            if key[0] != folded[0]:
                continue
            distance = _distance(folded, key, _slack(key))
            if distance <= _slack(key) and (best is None or distance < best[0]):
                best = (distance, term)
        return word if best is None else best[1] + word[len(stem) :]

    return _WORD.sub(fix, text)


def only_terms(text: str, terms: list[str]) -> bool:
    listed = {word.casefold() for term in terms for word in _WORD.findall(term)}
    words = [word.casefold() for word in _WORD.findall(text)]
    return bool(words) and all(word in listed for word in words)


def echoes(biased: str, plain: str) -> bool:
    ratio = SequenceMatcher(None, biased.casefold().strip(), plain.casefold().strip()).ratio()
    return ratio < ECHO_SIMILARITY
