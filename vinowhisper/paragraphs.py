PARAGRAPH_SILENCE_S = 2.5

PARAGRAPH_MIN_WORDS = 70

_SENTENCE_ENDS = ".?!"
_CLOSERS = "\"'”’)]"

_ABBREVIATIONS = frozenset(
    "mr. mrs. ms. dr. prof. st. jr. sr. vs. etc. e.g. i.e. approx. inc. ltd.".split()  # noqa: SIM905
)


def ends_sentence(word: str) -> bool:
    stripped = word.rstrip(_CLOSERS)
    if not stripped or stripped[-1] not in _SENTENCE_ENDS:
        return False
    if stripped.lower() in _ABBREVIATIONS:
        return False
    return not all(len(piece) <= 1 for piece in stripped.split("."))


class ParagraphBreaker:
    def __init__(self) -> None:
        self._break_pending = False
        self._words_in_paragraph = 0

    def silence(self, elapsed_s: float) -> None:
        if elapsed_s >= PARAGRAPH_SILENCE_S:
            self._break_pending = True

    def word(self, word: str) -> bool:
        breaks = self._break_pending and self._words_in_paragraph > 0
        if breaks:
            self._words_in_paragraph = 0
        self._break_pending = False
        self._words_in_paragraph += 1
        if self._words_in_paragraph >= PARAGRAPH_MIN_WORDS and ends_sentence(word):
            self._break_pending = True
        return breaks
