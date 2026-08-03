"""Live captioning entry point. Run manually in a terminal, Ctrl+C to stop.

Loop: grab the current rolling-buffer window, transcribe it (streamed),
diff against what's already confirmed, print only newly-confirmed words.
Cycle time is the pacing — no fixed hop timer, no overlapping in-flight
requests, since it's a single synchronous loop. Uses a LocalAgreement-2
policy (see _advance): a word only prints once it's shown up in the same
position across two consecutive cycles, since real testing showed Whisper
re-decoding the same heavily-overlapping window into different wording
cycle to cycle on ambiguous audio, not just paraphrasing near a boundary.
"""

import argparse
import difflib
import sys
import time

import numpy as np
import requests

from . import config
from .recorder import Recorder


# Word-level anchor, not a global character search. Real speech repeats
# short phrases legitimately ("this is the one, for sure" said three times in
# one ramble) — a global longest-match search can lock onto an earlier,
# wrong occurrence of a repeated phrase instead of the true overlap boundary.
# Restricting the search to the confirmed transcript's last _ANCHOR_WORDS
# sidesteps that: given window >> hop, almost all of it reappears near
# curr's start anyway, so the true boundary is reliably within a small
# recent slice of it.
#
# Widened from 15 to 40 after real testing 2026-08-03 against real content
# (compared directly against YouTube's own generated captions as ground
# truth) showed choppy re-statement at cycle boundaries — e.g. "was with the
# best pitcher in baseball. was with the best pitcher in baseball for the
# first month." The match was landing shorter than the true overlap: real
# per-cycle latency on substantial speech runs longer than the ~1.2s
# benchmark figure (more decode tokens takes more time), so less of the true
# overlap fell within a 15-word anchor, and the match-finder couldn't see far
# enough back to find the real boundary.
_ANCHOR_WORDS = 40

# Was a flat 3, but that can never be satisfied when curr itself is very
# short (max possible match on a 1-word transcript is 1) — confirmed
# 2026-08-03 this let a legitimately-repeated single-word hallucination
# ("you" x26, on quiet/hard-to-transcribe real audio the silence gate
# doesn't catch since it's not actually silent) reprint every cycle, since it
# could never be confirmed as "already shown." Scaled to the transcript's
# own length instead: a short curr only needs a proportionally short match.
_MIN_MATCH_WORDS = 3

# Tried priming decoding with prior-transcript context (initial_prompt) to
# reduce cross-window wording drift on the same underlying audio (e.g. "1,500
# years" vs "1,5 years" vs "two and a half years" for the same phrase across
# consecutive cycles, seen in real testing 2026-08-03) — confirmed the same
# day that initial_prompt is hard-blocked on NPU (see transcriber.py). Some
# residual wording drift across cycles is an accepted limitation for now,
# not fixable without a different approach (or a non-NPU device).


# Whisper's known hallucination failure mode on ambiguous/musical/silent
# audio: the decoder gets stuck looping a short phrase, sometimes hundreds of
# times ("bam bam bam...", "I love I love I love...", or even
# "youyouyouyou..." with no space at all between repeats — confirmed in real
# testing 2026-08-03, a degenerate case a word-split approach can't see since
# there's no whitespace boundary to split on). Confirmed the same day that
# openvino_genai's Whisper decode loop (checked pipeline_static.cpp,
# whisper.cpp, logit_processor.cpp/hpp directly — zero references to
# "ngram"/"repeat" anywhere) doesn't implement no_repeat_ngram_size at all
# for Whisper, despite the field existing on WhisperGenerationConfig
# (presumably wired up for the general LLM pipeline only, not Whisper's).
# Not fixable via generation config — this is a client-side cleanup instead,
# done at the character level (not word-split) so it catches repeats
# regardless of whether a space separates them. Genuine speech essentially
# never repeats the exact same word/phrase more than a few times in a row,
# so collapsing runs beyond that is safe.
_MAX_CONSECUTIVE_REPEATS = 3
_MAX_REPEAT_UNIT_CHARS = 50

# Whisper hallucinates short filler words ("you", "thank you for watching")
# on near-silent audio — a well-known community-documented behavior, not
# specific to this pipeline. True digital silence on the "output" source
# measures ~0.0-0.004 RMS (dither/processing noise from the EQ filter-chain
# sink, not exactly zero). This gate is just a "don't waste NPU cycles
# re-transcribing literal silence" optimization, not the primary defense
# against hallucination spam — that's now _advance's LocalAgreement-2 commit
# policy plus _collapse_repeats, both of which suppress a repeated
# hallucinated guess regardless of whether it slips past this gate. First
# dropped 0.01 -> 0.006 (2026-08-03) after "captions only work when volume
# is high"; confirmed the same day that was STILL too aggressive — real
# quiet speech can sit at or below the measured noise floor itself, so no
# fixed threshold above ~0.002 can separate "quiet speech" from "silence"
# reliably. Since a hallucinated guess on true silence no longer reaches the
# display (LocalAgreement won't confirm it, _collapse_repeats catches
# same-cycle runaways), there's no correctness reason left to keep this
# high — the only cost of setting it too low is wasted NPU cycles
# transcribing dead air, which is cheap compared to missing real speech.
_SILENCE_RMS_THRESHOLD = 0.002
_SILENCE_POLL_S = 0.3


def _collapse_repeats(text: str) -> str:
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


def _new_candidate_tail(confirmed_words: list[str], curr_words: list[str]) -> list[str]:
    """Words in curr_words past whatever's already confirmed — a candidate,
    not yet safe to print (see _advance for why).
    """
    if not confirmed_words:
        return curr_words

    anchor = confirmed_words[-_ANCHOR_WORDS:]

    # Case-insensitive comparison, not a raw string match. Whisper flips
    # capitalization on the same word across cycles (confirmed 2026-08-03:
    # "world series last year." in one cycle's confirmed tail vs "World
    # Series last year." two cycles later for the same audio) — an exact
    # match treats that as a totally different word, breaking the anchor
    # match in the middle of otherwise-solid overlap. That alone was enough
    # to drop the match below _MIN_MATCH_WORDS and trigger the "treat as
    # brand new" fallback below, and two consecutive fallback cycles that
    # happen to agree with each other (since neither is checked against
    # confirmed_words at all) reconfirm and reprint the entire transcript
    # from scratch. Printed words still use curr_words' own casing (below),
    # this only affects whether two words are considered "the same" for
    # alignment purposes.
    matcher = difflib.SequenceMatcher(
        None, [w.lower() for w in anchor], [w.lower() for w in curr_words]
    )

    # Furthest-reaching match, not the longest one. A single hallucinated/
    # inserted word mid-sentence ("acquire Hose Jose Soriano..." vs "...Jose
    # Soriano...") splits what should be one long match into two shorter
    # blocks — find_longest_match picks whichever is biggest, which isn't
    # necessarily the one closest to curr's frontier. Confirmed 2026-08-03
    # this caused a 9-word block to win over a 7-word block that actually
    # reached 8 words further into curr, letting an already-shown sentence
    # get reprinted as if new. What we actually want is "how far into curr
    # can we confirm this is old content", i.e. the block ending latest.
    blocks = [b for b in matcher.get_matching_blocks() if b.size > 0]
    if not blocks:
        return curr_words
    match = max(blocks, key=lambda b: b.b + b.size)

    min_match = min(_MIN_MATCH_WORDS, len(curr_words))
    if match.size < min_match:
        # No confident overlap — treating everything as a fresh candidate is
        # the safer failure mode than silently dropping content past a
        # spurious match.
        return curr_words

    return curr_words[match.b + match.size :]


def _advance(
    confirmed_words: list[str], pending_words: list[str], curr_words: list[str]
) -> tuple[list[str], list[str]]:
    """LocalAgreement-2 policy: a word only gets confirmed (safe to print)
    once it's shown up in the same position across two consecutive cycles.

    Real testing 2026-08-03 (--debug raw per-cycle dump) showed the previous
    "print the new tail the moment it appears" design was never going to
    work: on ambiguous audio (a proper noun, "Scherzer"), Whisper re-decoded
    the same ~95%-overlapping window into completely different words cycle
    to cycle — "Ex-sherzer" / "you're told" / "you know Dalton" for the same
    underlying audio across consecutive cycles, not near-boundary
    paraphrasing. No diff, exact or fuzzy, can dedupe that: the words
    genuinely aren't the same until the model has enough context to settle
    down. Returns (newly_confirmed_words, new_pending_words) — print only
    newly_confirmed_words, hold new_pending_words for next cycle.
    """
    candidate = _new_candidate_tail(confirmed_words, curr_words)

    agree_len = 0
    for old_word, new_word in zip(pending_words, candidate):
        if old_word.lower() != new_word.lower():  # same casing flip issue as above
            break
        agree_len += 1

    return candidate[:agree_len], candidate[agree_len:]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live captioning via NPU Whisper.")
    parser.add_argument(
        "--source",
        choices=["output", "mic"],
        default=config.DEFAULT_SOURCE,
        help="'output' captions system audio (default), 'mic' captions your voice.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print each cycle's raw transcript (pre-merge) to stderr, to tell "
        "apart Whisper re-decoding the same audio with different wording "
        "from a bug in the cross-cycle merge itself.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    session = requests.Session()
    confirmed_words: list[str] = []
    pending_words: list[str] = []
    cycle = 0

    try:
        with Recorder(source=args.source) as rec:
            while True:
                samples = rec.snapshot()
                if samples.size == 0:
                    continue

                rms = float(np.sqrt(np.mean(np.square(samples))))
                if rms < _SILENCE_RMS_THRESHOLD:
                    time.sleep(_SILENCE_POLL_S)
                    continue

                response = session.post(
                    f"{config.SERVER_URL}/transcribe",
                    data=samples.tobytes(),
                    stream=True,
                )
                response.raise_for_status()
                # chunk_size=None makes urllib3 read the whole body in one
                # blocking call instead of incrementally — harmless for the
                # current full-transcript-then-diff design, but would defeat
                # any future live/incremental rendering, so read in small
                # pieces regardless.
                curr_transcript = "".join(
                    chunk.decode("utf-8")
                    for chunk in response.iter_content(chunk_size=1)
                )
                curr_transcript = _collapse_repeats(curr_transcript)

                if args.debug:
                    cycle += 1
                    print(f"\n--- cycle {cycle} raw ---\n{curr_transcript}\n", file=sys.stderr)

                curr_words = curr_transcript.split()
                if not curr_words:
                    continue

                newly_confirmed, pending_words = _advance(
                    confirmed_words, pending_words, curr_words
                )
                if newly_confirmed:
                    sep = " " if confirmed_words else ""
                    print(sep + " ".join(newly_confirmed), end="", flush=True)
                    confirmed_words.extend(newly_confirmed)
    except KeyboardInterrupt:
        # Wraps the whole `with` block, not just the loop — a second/mistimed
        # Ctrl+C during Recorder.__exit__'s cleanup should still exit
        # cleanly, not spew a traceback (confirmed happening in real testing
        # 2026-08-03). Also flush whatever was still pending (agreed once,
        # never got a second cycle to confirm it) — better to show the last
        # unconfirmed guess than silently drop it on exit.
        if pending_words:
            sep = " " if confirmed_words else ""
            print(sep + " ".join(pending_words), end="")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
