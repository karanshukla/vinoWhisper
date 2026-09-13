from difflib import SequenceMatcher

_ANCHOR_WORDS = 40

_MIN_MATCH_WORDS = 3

_MAX_CONSECUTIVE_REPEATS = 3
_MAX_REPEAT_UNIT_CHARS = 50

_MAX_REPEAT_UNIT_WORDS = 6

_MAX_CONFIRMED_WORDS = 200

_REDECODE_RATIO = 0.6

_MIN_REALIGN_WORDS = 2

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


def _confirmed_prefix_len(confirmed: list[str], curr: list[str]) -> int:
    for k in range(min(len(confirmed), len(curr)), 0, -1):
        if all(_norm(a) == _norm(b) for a, b in zip(confirmed[-k:], curr[:k], strict=True)):
            return k
    return 0


def _redecode_len(residual: list[str], curr: list[str]) -> int:
    if not residual or not curr:
        return 0
    target = " ".join(_norm(w) for w in residual)
    best_k, best = 0, _REDECODE_RATIO
    for k in range(1, min(len(curr), len(residual) + 2) + 1):
        probe = " ".join(_norm(w) for w in curr[:k])
        ratio = SequenceMatcher(None, target, probe, autojunk=False).ratio()
        if ratio >= best:
            best_k, best = k, ratio
    return best_k


def _cut(confirmed: list[str], curr: list[str]) -> int:
    if not confirmed:
        return 0

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
        return _confirmed_prefix_len(confirmed, curr)

    match = max(blocks, key=lambda b: b.b + b.size)
    end = match.b + match.size
    # Confirmed words past the match are on screen already; their re-decode must not print again.
    return end + _redecode_len(anchor[match.a + match.size :], curr[end:])


def _realign(pending: list[str], curr: list[str]) -> tuple[int, int]:
    matcher = SequenceMatcher(
        None, [_norm(w) for w in pending], [_norm(w) for w in curr], autojunk=False
    )
    floor = min(_MIN_REALIGN_WORDS, len(pending))
    blocks = [b for b in matcher.get_matching_blocks() if b.size >= floor]
    if not blocks:
        return 0, 0
    best = max(blocks, key=lambda b: b.size)
    offset = best.b - best.a
    return max(offset, 0), max(-offset, 0)


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

        cut = _cut(self._confirmed, curr)
        if cut == 0 and self._confirmed and self._pending:
            # The confirmed tail left the window; the pending words are the only anchor left.
            cut, drop = _realign(self._pending, curr)
            del self._pending[:drop]
        candidate = curr[cut:]

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
