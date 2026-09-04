"""Turning a stream of overlapping window transcripts into one transcript.

Each caption cycle re-transcribes a window that overlaps the previous one
heavily, so consecutive transcripts mostly restate the same words. Two things
have to happen before anything reaches the screen:

1. Find where the new transcript stops repeating what's already been shown
   (`_candidate_tail`).
2. Refuse to print that tail until a second cycle agrees with it
   (`Stitcher.push`, a LocalAgreement-2 commit policy).

Step 2 isn't belt-and-braces. Real testing 2026-08-03 (--debug raw per-cycle
dump) showed the earlier "print the new tail the moment it appears" design was
unworkable: on ambiguous audio (a proper noun, "Scherzer") Whisper re-decoded
the same ~95%-overlapping window into completely different words cycle to
cycle — "Ex-sherzer" / "you're told" / "you know Dalton" for the same audio —
not near-boundary paraphrasing. No diff, exact or fuzzy, dedupes that; the
words genuinely aren't the same until the model has enough context to settle.
Waiting for agreement also means a hallucinated guess on near-silence never
reaches the screen, since the next cycle guesses something else.

Priming the decode with prior context (`initial_prompt`) would cut the
remaining cross-window wording drift — "1,500 years" / "1,5 years" / "two and
a half years" for one phrase across consecutive cycles — but it's hard-blocked
on NPU (see transcriber.py). That drift is an accepted limitation for now.
"""

from difflib import SequenceMatcher

# How far back into the confirmed transcript to look for the overlap boundary.
# A word-level anchor, not a global search: real speech repeats short phrases
# legitimately, and a global longest-match can lock onto an earlier, wrong
# occurrence. Given window >> hop, the true boundary is reliably within a small
# recent slice of what's confirmed.
#
# Widened 15 -> 40 after real testing 2026-08-03 against real content (compared
# against YouTube's own captions as ground truth) showed choppy re-statement at
# cycle boundaries — "was with the best pitcher in baseball. was with the best
# pitcher in baseball for the first month." Per-cycle latency on substantial
# speech runs well past the ~1.2s benchmark figure, so more of the true overlap
# fell outside a 15-word anchor than expected.
_ANCHOR_WORDS = 40

# Minimum overlap before we trust it. Scaled down for short transcripts rather
# than flat: a flat 3 can never be satisfied when the transcript is 1-2 words
# long, which let a repeated single-word hallucination ("you" x26 on quiet
# audio the silence gate doesn't catch) reprint every cycle, since it could
# never be recognised as already-shown.
_MIN_MATCH_WORDS = 3

# Whisper's known hallucination failure mode on ambiguous/musical/silent audio:
# the decoder gets stuck looping a short phrase, sometimes hundreds of times
# ("bam bam bam...", or "youyouyouyou..." with no space at all between repeats
# — confirmed 2026-08-03, a degenerate case that word-splitting can't see).
# Confirmed the same day that openvino_genai's Whisper decode loop doesn't
# implement no_repeat_ngram_size at all (checked pipeline_static.cpp,
# whisper.cpp, logit_processor.cpp/hpp — zero references to "ngram"/"repeat"),
# despite the field existing on WhisperGenerationConfig. So this is a
# client-side cleanup, done at character level rather than by word so it
# catches repeats with no whitespace boundary to split on. Genuine speech
# essentially never repeats the same unit more than a few times running.
_MAX_CONSECUTIVE_REPEATS = 3
_MAX_REPEAT_UNIT_CHARS = 50

# The word-level pass's equivalent of _MAX_REPEAT_UNIT_CHARS. Past roughly this
# length a repeated run stops looking like a decoder loop and starts looking
# like a speaker restating themselves, and the two are not distinguishable from
# the text alone. Erring short is the safe direction: scrollback is
# append-only, so a false positive silently deletes real words and there is no
# way to put them back. The longest loop unit actually seen here was five words
# ("do things that make you", 2026-09-01).
_MAX_REPEAT_UNIT_WORDS = 6

# Only the last _ANCHOR_WORDS are ever read back, so retaining the whole
# session's transcript just leaks memory across a long run.
_MAX_CONFIRMED_WORDS = 200

# Stripped before comparing two decodes of the same word, never before printing
# it. Same failure mode as the capitalization flip documented in
# _candidate_tail, and from the same cause: Whisper re-decodes the identical
# audio with different punctuation depending on how much right-context the
# window happened to include ("baseball." / "baseball," / "baseball" as the
# window boundary moves through the sentence). Each flip breaks the anchor
# mid-overlap exactly the way a case flip did, which is enough on its own to
# drop below _MIN_MATCH_WORDS and reprint an already-shown sentence.
_COMPARE_STRIP = ".,!?;:\"'“”‘’()[]—–-…"


def _norm(word: str) -> str:
    """Word as it should be *compared*, not as it should be shown."""
    # Fall back to the raw word when stripping empties it, so a token that is
    # nothing but punctuation ("—") doesn't normalize to "" and match anything.
    return word.strip(_COMPARE_STRIP).lower() or word.lower()


def collapse_repeats(text: str) -> str:
    """Collapse any unit repeated more than _MAX_CONSECUTIVE_REPEATS times."""
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
    """Word-level counterpart to collapse_repeats, compared through _norm.

    Measured 2026-09-04 against the character-level pass, which turns out to
    catch more than its docstring claims: an *exact* whitespace-separated
    repeat ("you you you you", "do things do things do things do things") is
    already collapsed, because "you " is just as much a repeating character
    unit as "you" is. So whitespace was never the thing defeating it.

    What defeats it is normalization. Whisper re-decodes the same audio with
    drifting punctuation and capitalization — documented twice here, on
    2026-08-07 and again on 2026-09-01 — so a real loop reaches this function
    as "do things. do things, Do things do things! do things", which shares no
    repeating character unit at all and passes straight through. Comparing on
    _norm is what closes that, and it is the same normalization the anchor
    already uses for the same reason.

    Deliberately only collapses *adjacent* repeats, not a word appearing three
    times anywhere in the cycle. Real speech does the latter constantly ("the",
    "and", "you know"), and with an append-only transcript a false positive is
    unrecoverable, so the cheap-looking extra coverage is the expensive kind.

    Returns the original words, never the normalized ones — _norm is for
    comparing, never for printing.
    """
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
    """Trim words off the front of `curr` that restate the tail of `confirmed`,
    one word at a time, as far as they keep agreeing.

    Backstop for when the SequenceMatcher search below finds no block of at
    least _MIN_MATCH_WORDS. That floor exists to avoid locking onto an
    unrelated *internal* occurrence of a common phrase, but it also rejects a
    real overlap that happens to be short — confirmed 2026-09-01 on real
    content: wording drift left only a 2-word boundary match ("do things"
    before "do things that make you"), so the whole cycle fell through to
    "treat as new" and reprinted the already-shown words. A match *at the
    boundary* doesn't have that ambiguity — curr always starts inside a window
    that overlaps what's already confirmed, so there's nowhere else a matching
    first word could have come from — so even a 1-word boundary match is
    trustworthy where an internal one isn't.
    """
    limit = min(len(confirmed), len(curr))
    tail = confirmed[-limit:] if limit else []
    n = 0
    while n < limit and _norm(tail[n]) == _norm(curr[n]):
        n += 1
    return curr[n:]


def _candidate_tail(confirmed: list[str], curr: list[str]) -> list[str]:
    """The part of `curr` that goes past what's already been shown."""
    if not confirmed:
        return curr

    anchor = confirmed[-_ANCHOR_WORDS:]

    # Compared through _norm, not as raw strings. Whisper flips capitalization
    # on the same word across cycles (confirmed 2026-08-03: "world series last
    # year." vs "World Series last year." for the same audio two cycles apart)
    # and flips punctuation the same way. An exact match reads either as a
    # different word and breaks the anchor mid-overlap, which was on its own
    # enough to drop below _MIN_MATCH_WORDS and fall through to "treat as brand
    # new" — and two consecutive fallthrough cycles agree with each other, so
    # the whole transcript gets reconfirmed and reprinted from scratch.
    #
    # autojunk=False because the heuristic it disables ("ignore items
    # appearing in >1% of a long sequence") would classify exactly the common
    # words — "the", "a", "and" — that hold an overlap match together.
    matcher = SequenceMatcher(
        None, [_norm(w) for w in anchor], [_norm(w) for w in curr], autojunk=False
    )

    # Two filters, in this order, and the order matters.
    #
    # Size first: a block has to be long enough to be believable as the real
    # overlap. Reach second: among believable blocks, take the one ending
    # latest in `curr`, not the longest. A single hallucinated word
    # mid-sentence ("acquire Hose Jose Soriano" vs "...Jose Soriano") splits
    # one long match into two shorter blocks, and the biggest of those isn't
    # necessarily the one closest to curr's frontier — confirmed 2026-08-03
    # that a 9-word block won over a 7-word block reaching 8 words further,
    # reprinting an already-shown sentence. What we want is "how far into curr
    # can we still call this old content".
    #
    # Filtering before the max, rather than picking furthest-reaching and then
    # checking its size, is the fix for the mirror-image bug: one spurious
    # 1-word match at the very end of curr would otherwise win the reach
    # comparison, fail the size check, and send the entire transcript through
    # as new.
    min_match = min(_MIN_MATCH_WORDS, len(curr))
    blocks = [b for b in matcher.get_matching_blocks() if b.size >= min_match]
    if not blocks:
        # No confident *internal* overlap. Still try the boundary before
        # giving up and treating everything as fresh — see
        # _strip_confirmed_prefix. LocalAgreement still has to confirm
        # whatever's left before it's printed either way.
        return _strip_confirmed_prefix(confirmed, curr)

    match = max(blocks, key=lambda b: b.b + b.size)
    return curr[match.b + match.size :]


class Stitcher:
    """Accumulates per-cycle transcripts into one committed transcript."""

    def __init__(self) -> None:
        self._confirmed: list[str] = []
        self._pending: list[str] = []

    @property
    def pending(self) -> list[str]:
        """Words seen once, still waiting on a second cycle to agree."""
        return list(self._pending)

    def push(self, transcript: str) -> list[str]:
        """Feed one cycle's transcript; return the words now safe to print."""
        # Both passes run here, on the raw cycle, rather than on the words
        # about to be committed. Cleaning only the commit would leave
        # `_confirmed` holding a collapsed run while the next cycle's `curr`
        # still holds the full one, and an anchor compared against a tail that
        # no longer matches the audio is exactly the reprint bug this is meant
        # to defend against. One representation, cleaned once, at the door.
        curr = collapse_word_repeats(collapse_repeats(transcript).split())
        if not curr:
            return []

        candidate = _candidate_tail(self._confirmed, curr)

        agree_len = 0
        # strict=False: the two are different lengths by construction —
        # `candidate` is what this cycle saw, `_pending` what the last one did.
        for old_word, new_word in zip(self._pending, candidate, strict=False):
            if _norm(old_word) != _norm(new_word):  # same flips as above
                break
            agree_len += 1

        # Committed from `candidate`, not `self._pending`: when two decodes
        # agree modulo punctuation, the later one saw more right-context, so
        # its punctuation is the better guess.
        newly_confirmed = candidate[:agree_len]
        self._pending = candidate[agree_len:]
        self._commit(newly_confirmed)
        return newly_confirmed

    def flush(self) -> list[str]:
        """Commit whatever agreed once but never got a confirming cycle.

        For shutdown: showing the last unconfirmed guess beats dropping it.
        """
        tail, self._pending = self._pending, []
        self._commit(tail)
        return tail

    def _commit(self, words: list[str]) -> None:
        if not words:
            return
        self._confirmed.extend(words)
        del self._confirmed[:-_MAX_CONFIRMED_WORDS]
