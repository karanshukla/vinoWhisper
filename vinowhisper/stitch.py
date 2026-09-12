from difflib import SequenceMatcher

_ANCHOR_WORDS = 40

_MIN_MATCH_WORDS = 3

_MAX_CONSECUTIVE_REPEATS = 3
_MAX_REPEAT_UNIT_CHARS = 50

_MAX_REPEAT_UNIT_WORDS = 6

_MAX_CONFIRMED_WORDS = 200

_COMPARE_STRIP = ".,!?;:\"'“”‘’()[]—–-…"


def _norm(word: str) -> str:
    # A punctuation-only token falls back to itself so it never matches everything.
    return word.strip(_COMPARE_STRIP).lower() or word.lower()


def collapse_repeats(text: str) -> str:
    n = len(text)
    out: list[str] = []
    i = 0
    while i < n:
        collapsed = False
        for unit_len in range(1, _MAX_REPEAT_UNIT_CHARS + 1):
            if i + unit_len > n:
                break
            unit = text[i : i + unit_len]
            reps = 1
            j = i + unit_len
            while text[j : j + unit_len] == unit:
                reps += 1
                j += unit_len
            if reps > _MAX_CONSECUTIVE_REPEATS:
                out.append(unit * _MAX_CONSECUTIVE_REPEATS)
                i = j
                collapsed = True
                break
        if not collapsed:
            out.append(text[i])
            i += 1
    return "".join(out)


def collapse_word_repeats(words: list[str]) -> list[str]:
    keys = [_norm(word) for word in words]
    total = len(words)
    out: list[str] = []
    i = 0
    while i < total:
        collapsed = False
        for unit_len in range(1, _MAX_REPEAT_UNIT_WORDS + 1):
            if i + unit_len > total:
                break
            unit = keys[i : i + unit_len]
            reps = 1
            j = i + unit_len
            while keys[j : j + unit_len] == unit:
                reps += 1
                j += unit_len
            if reps > _MAX_CONSECUTIVE_REPEATS:
                out.extend(words[i : i + unit_len * _MAX_CONSECUTIVE_REPEATS])
                i = j
                collapsed = True
                break
        if not collapsed:
            out.append(words[i])
            i += 1
    return out


def _strip_confirmed_prefix(confirmed: list[str], curr: list[str]) -> list[str]:
    limit = min(len(confirmed), len(curr))
    tail = confirmed[-limit:] if limit else []
    n = 0
    while n < limit and _norm(tail[n]) == _norm(curr[n]):
        n += 1
    return curr[n:]


def _candidate_tail(confirmed: list[str], curr: list[str]) -> list[str]:
    if not confirmed:
        return curr

    anchor = confirmed[-_ANCHOR_WORDS:]

    matcher = SequenceMatcher(
        # autojunk would discard the common words that hold an overlap together.
        None,
        [_norm(w) for w in anchor],
        [_norm(w) for w in curr],
        autojunk=False,
    )

    min_match = min(_MIN_MATCH_WORDS, len(curr))
    # Filter by size before taking the furthest reach; the reverse lets one stray word reprint everything.
    blocks = [b for b in matcher.get_matching_blocks() if b.size >= min_match]
    if not blocks:
        return _strip_confirmed_prefix(confirmed, curr)

    match = max(blocks, key=lambda b: b.b + b.size)
    return curr[match.b + match.size :]


class Stitcher:
    def __init__(self) -> None:
        self._confirmed: list[str] = []
        self._pending: list[str] = []

    @property
    def pending(self) -> list[str]:
        return list(self._pending)

    def push(self, transcript: str) -> list[str]:
        # Cleaned before anchoring, so _confirmed matches what the next cycle's curr holds.
        curr = collapse_word_repeats(collapse_repeats(transcript).split())
        if not curr:
            return []

        candidate = _candidate_tail(self._confirmed, curr)

        agree_len = 0
        # Different lengths by design: this cycle's words against the last one's.
        for old_word, new_word in zip(self._pending, candidate, strict=False):
            if _norm(old_word) != _norm(new_word):
                break
            agree_len += 1

        newly_confirmed = candidate[:agree_len]
        self._pending = candidate[agree_len:]
        self._commit(newly_confirmed)
        return newly_confirmed

    def flush(self) -> list[str]:
        tail, self._pending = self._pending, []
        self._commit(tail)
        return tail

    def _commit(self, words: list[str]) -> None:
        if not words:
            return
        self._confirmed.extend(words)
        del self._confirmed[:-_MAX_CONFIRMED_WORDS]
