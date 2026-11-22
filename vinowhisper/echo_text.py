import re
import time
from collections import deque
from collections.abc import Callable

MIN_WORDS = 3
MATCH = 90.0
KEEP_S = 20.0
_RECENT = 6

_NON_WORD = re.compile(r"[^\w\s]")


def _normal(words: list[str]) -> str:
    return " ".join(_NON_WORD.sub("", " ".join(words)).lower().split())


class EchoDedup:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._them: deque[tuple[float, str]] = deque(maxlen=_RECENT)

    def heard_them(self, words: list[str]) -> None:
        if words:
            self._them.append((self._clock(), _normal(words)))
        self._forget()

    # (pinned by `test_echo_text.py::test_a_you_line_that_repeats_a_recent_them_line_is_dropped`)
    def echoed(self, words: list[str]) -> bool:
        self._forget()
        if len(words) < MIN_WORDS:
            return False
        from rapidfuzz import fuzz

        mine = _normal(words)
        recent = " ".join(text for _, text in self._them)
        return bool(recent) and fuzz.partial_ratio(mine, recent) >= MATCH

    def _forget(self) -> None:
        now = self._clock()
        while self._them and now - self._them[0][0] > KEEP_S:
            self._them.popleft()
