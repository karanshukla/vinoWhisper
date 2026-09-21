"""The merge policy, which is where every caption bug so far has lived.

Each case here is a failure that actually happened on hardware, written down
as a fixture. The 2026-08-06 and 2026-08-07 reviews found all of them with
throwaway scripts against a stubbed pipeline; this is those scripts, kept.
"""

from vinowhisper.stitch import (
    Stitcher,
    _cut,
    _norm,
    _realign,
    _redecode_len,
    collapse_repeats,
    collapse_word_repeats,
)


def push_all(stitcher: Stitcher, *transcripts: str) -> list[str]:
    printed: list[str] = []
    for transcript in transcripts:
        printed.extend(stitcher.push(transcript))
    return printed


def test_nothing_prints_until_two_cycles_agree():
    stitcher = Stitcher()
    assert stitcher.push("the dugout emptied out") == []
    assert stitcher.pending == ["the", "dugout", "emptied", "out"]
    assert stitcher.push("the dugout emptied out") == ["the", "dugout", "emptied", "out"]


def test_a_word_only_one_cycle_ever_saw_never_prints():
    """The anti-hallucination property: a one-off guess is dropped, not shown."""
    stitcher = Stitcher()
    stitcher.push("Ex-sherzer was pitching")
    stitcher.push("you know Dalton was pitching")
    assert "Ex-sherzer" not in stitcher._confirmed
    assert "Dalton" not in stitcher._confirmed


def test_overlapping_windows_do_not_reprint_the_overlap():
    stitcher = Stitcher()
    push_all(
        stitcher,
        "he was with the best pitcher in baseball",
        "he was with the best pitcher in baseball",
    )
    printed = push_all(
        stitcher,
        "with the best pitcher in baseball for the first month",
        "with the best pitcher in baseball for the first month",
    )
    assert printed == ["for", "the", "first", "month"]


def test_punctuation_drift_across_cycles_does_not_break_the_anchor():
    """The 2026-08-07 bug: 'baseball.' / 'baseball,' / 'baseball' for one word.

    Whisper re-decodes identical audio with drifting punctuation as the window
    boundary moves through a sentence. Comparing raw strings breaks the anchor
    mid-overlap, drops below the match floor, and reprints a shown sentence.
    """
    stitcher = Stitcher()
    push_all(
        stitcher,
        "the best pitcher in baseball.",
        "the best pitcher in baseball.",
    )
    printed = push_all(
        stitcher,
        "the best pitcher in baseball, for the first month",
        "the best pitcher in baseball, for the first month",
    )
    assert printed == ["for", "the", "first", "month"]


def test_capitalization_drift_does_not_break_the_anchor():
    stitcher = Stitcher()
    push_all(stitcher, "won the world series last year", "won the world series last year")
    printed = push_all(
        stitcher,
        "won the World Series last year and again",
        "won the World Series last year and again",
    )
    assert printed == ["and", "again"]


def test_a_single_spurious_word_match_does_not_reprint_everything():
    """The mirror-image bug from the 2026-08-06 review.

    A size-1 match block near the end of the new transcript used to win the
    furthest-reach comparison, fail the size check, and fall through to
    'treat the whole thing as new'.
    """
    stitcher = Stitcher()
    push_all(stitcher, "alpha bravo charlie delta echo", "alpha bravo charlie delta echo")
    printed = push_all(
        stitcher,
        "bravo charlie delta echo foxtrot golf alpha",
        "bravo charlie delta echo foxtrot golf alpha",
    )
    assert printed[:2] == ["foxtrot", "golf"]
    assert printed.count("bravo") == 0


def test_short_boundary_overlap_below_the_anchor_floor_does_not_reprint():
    """The 2026-09-01 bug, seen on a real 4-minute session: wording drift left
    only a 2-word boundary match ("do things"), below _MIN_MATCH_WORDS, so the
    whole next cycle fell through to "treat as new" and reprinted words
    already on screen, growing worse each cycle as the sentence continued.
    """
    stitcher = Stitcher()
    assert push_all(stitcher, "do things", "do things") == ["do", "things"]
    printed = push_all(
        stitcher,
        "do things that make you",
        "do things that make you",
    )
    assert printed == ["that", "make", "you"]


def test_flush_releases_the_last_unconfirmed_guess():
    stitcher = Stitcher()
    stitcher.push("and the crowd went")
    assert stitcher.flush() == ["and", "the", "crowd", "went"]
    assert stitcher.pending == []
    assert stitcher.flush() == []


def test_empty_transcript_is_ignored():
    stitcher = Stitcher()
    assert stitcher.push("   ") == []
    assert stitcher.pending == []


def test_confirmed_history_stays_bounded():
    stitcher = Stitcher()
    for index in range(200):
        # Distinct text each cycle so nothing anchors; each pair confirms.
        stitcher.push(f"word{index} filler{index}")
        stitcher.push(f"word{index} filler{index}")
    assert len(stitcher._confirmed) <= 200


def test_collapse_repeats_handles_the_no_whitespace_case():
    """'youyouyouyou...' — the degenerate case word-splitting cannot see."""
    assert collapse_repeats("you" * 26) == "youyouyou"
    assert collapse_repeats("bam bam bam bam bam bam ") == "bam bam bam "


def test_collapse_repeats_leaves_ordinary_speech_alone():
    text = "he said that that was the plan"
    assert collapse_repeats(text) == text


def test_repeat_collapse_runs_before_stitching():
    stitcher = Stitcher()
    stitcher.push("okay " + "you" * 20)
    assert stitcher.pending == ["okay", "youyouyou"]


def test_characterization_push_commits_the_later_decodes_punctuation():
    """characterization: `push` commits `candidate`'s words, not `_pending`'s.

    Reads like a copy-paste slip — the agreement was computed against
    `_pending`, so committing `_pending` looks like the obvious intent. It is
    not. When two decodes agree modulo punctuation the *later* one saw more
    right-context, so its punctuation is the better guess, and taking it is
    the whole reason the commit reads from `candidate`.

    Flipping this is a deliberate act. It will not fail loudly if you get it
    wrong; it will just quietly print slightly worse punctuation forever.
    """
    stitcher = Stitcher()
    stitcher.push("the best pitcher in baseball.")  # first decode: full stop
    printed = stitcher.push("the best pitcher in baseball,")  # second: comma

    assert printed[-1] == "baseball,"  # the later decode's, not the earlier's
    assert stitcher._confirmed[-1] == "baseball,"


def test_characterization_norm_strips_punctuation_for_comparison_only():
    """characterization: `_norm` never touches what gets printed.

    The temptation is to normalize once, on the way in, and be done. That
    would strip Whisper's punctuation and capitalization out of the
    transcript, and since whisper-small.en is where all of that comes from
    (nothing here infers it), the captions would arrive as unpunctuated
    lowercase and the paragraph logic — which looks for sentence ends — would
    stop finding any.
    """
    assert _norm("Baseball.") == _norm("baseball,") == "baseball"

    stitcher = Stitcher()
    printed = push_all(stitcher, "He won. It's over —", "He won. It's over —")
    assert printed == ["He", "won.", "It's", "over", "—"]


def test_characterization_a_pure_punctuation_token_does_not_normalize_to_empty():
    """characterization: `_norm` falls back to the raw word when stripping
    empties it. Without the fallback an em dash normalizes to "" and compares
    equal to every other punctuation-only token, which is a match the anchor
    would believe.
    """
    assert _norm("—") == "—"
    assert _norm("...") == "..."
    assert _norm("—") != _norm("…")


def words(text: str) -> list[str]:
    return collapse_word_repeats(text.split())


def test_the_character_pass_already_catches_an_exact_spaced_repeat():
    """Measured 2026-09-04, and it is the reason #10 needed re-stating: the
    character-level pass is not blind to whitespace-separated repeats at all.
    "you " repeats as a character unit exactly as "you" does.
    """
    assert collapse_repeats("you you you you you you you") == "you you you you"
    assert collapse_repeats("do things " * 5).split() == ["do", "things"] * 3


def test_punctuation_drift_between_repeats_defeats_the_character_pass():
    """Which is what actually leaves a loop uncaught, and it is not a rare
    input: drifting punctuation on re-decoded audio is documented here twice
    (2026-08-07, 2026-09-01).
    """
    looped = "do things. do things, do things do things! do things"
    assert collapse_repeats(looped) == looped  # unchanged — nothing to see
    assert words(looped) == ["do", "things.", "do", "things,", "do", "things"]


def test_capitalization_drift_between_repeats_is_caught_too():
    looped = "Do things do things Do things do things Do things"
    assert collapse_repeats(looped) == looped
    assert words(looped) == ["Do", "things", "do", "things", "Do", "things"]


def test_a_looped_single_word_survives_punctuation_drift():
    assert words("you you. you, You you you") == ["you", "you.", "you,"]


def test_the_collapsed_words_keep_the_punctuation_they_arrived_with():
    """_norm is for comparing, never for printing — same rule as the anchor."""
    assert words("bam! bam? bam. bam bam") == ["bam!", "bam?", "bam."]


def test_a_long_unit_is_left_alone_rather_than_guessed_at():
    """Past _MAX_REPEAT_UNIT_WORDS a repeated run is as likely to be a speaker
    restating themselves as a decoder loop, and an append-only transcript
    cannot take back a wrong guess.
    """
    long_unit = "the quick brown fox jumps over the lazy dog " * 5
    assert words(long_unit) == long_unit.split()


def test_ordinary_speech_repetition_is_left_alone():
    for text in (
        "he said that that was the plan",
        "very very good",
        "no no I meant it",
        "and thanks for watching and thanks so much for watching",
    ):
        assert words(text) == text.split(), text


def test_a_word_repeated_non_adjacently_is_not_touched():
    """Deliberate. Real speech does this constantly and the cost of a false
    positive is a word deleted from scrollback with no way to restore it.
    """
    text = "the ball hit the wall past the line"
    assert words(text) == text.split()


def test_word_level_collapse_runs_before_stitching():
    stitcher = Stitcher()
    stitcher.push("okay do things. do things, do things do things! do things")
    assert stitcher.pending == ["okay", "do", "things.", "do", "things,", "do", "things"]


def test_a_drifting_loop_never_reaches_the_confirmed_transcript():
    """The end-to-end shape: two cycles agree on the cleaned text, so what
    prints is the collapsed run rather than the loop.
    """
    stitcher = Stitcher()
    printed = push_all(
        stitcher,
        "and then you know. you know, You know you know you know it stopped",
        "and then you know. you know, You know you know you know it stopped",
    )
    # Three reps kept, each with the punctuation it arrived with, and the real
    # words on the far side of the loop still reach the screen.
    kept = "and then you know. you know, You know it stopped"
    assert printed == kept.split()


def test_collapse_word_repeats_of_nothing_is_nothing():
    assert collapse_word_repeats([]) == []


def test_short_boundary_overlap_still_works_once_the_transcript_is_long():
    """The 2026-09-12 bug: the 2026-09-01 boundary check compared the wrong
    words once the confirmed transcript outgrew one window, so it only ever
    worked in the first minute of a session. Same fixture as above, after
    fifty words of earlier speech.
    """
    stitcher = Stitcher()
    for index in range(25):
        push_all(stitcher, f"w{index} x{index}", f"w{index} x{index}")
    assert push_all(stitcher, "do things", "do things") == ["do", "things"]
    printed = push_all(
        stitcher,
        "do things that make you",
        "do things that make you",
    )
    assert printed == ["that", "make", "you"]


def test_a_re_decoded_confirmed_word_does_not_print_twice():
    """Measured 2026-09-12 with a 0.7s hop: the trailing words of a window flip
    for several cycles ("Whiskers" / "Whispers" / "Whisper's"), two near-
    identical windows agree on one form, it prints, and the decode then
    settles on another. The anchor cannot match the settled form against the
    printed one, so the cut used to land a word early and the settled form
    printed again: "Whispers Whisper's encoder cost is fixed".
    """
    stitcher = Stitcher()
    push_all(
        stitcher,
        "cycle time is the only real lever. Whispers",
        "cycle time is the only real lever. Whispers",
    )
    printed = push_all(
        stitcher,
        "cycle time is the only real lever. Whisper's encoder cost is fixed",
        "cycle time is the only real lever. Whisper's encoder cost is fixed",
    )
    assert printed == ["encoder", "cost", "is", "fixed"]


def test_a_re_decode_that_splits_a_word_is_skipped_as_a_unit():
    """ "auto-aggressive. One" printed, then decoded as "auto regressive one":
    two words became three. The skip is judged on the joined text, so the
    word count changing does not leave one of them behind to print.
    """
    stitcher = Stitcher()
    push_all(
        stitcher,
        "no matter what, but decoding is auto-aggressive. One",
        "no matter what, but decoding is auto-aggressive. One",
    )
    printed = push_all(
        stitcher,
        "no matter what, but decoding is auto regressive one forward pass per token",
        "no matter what, but decoding is auto regressive one forward pass per token",
    )
    assert printed == ["forward", "pass", "per", "token"]


def test_characterization_the_redecode_skip_is_bounded_and_needs_a_resemblance():
    """characterization: `_redecode_len` may skip at most two words more than
    the residual holds, and nothing at all when the new words do not resemble
    it. A residual word that the new decode simply dropped must not cost a
    real new word, and a long residual must never swallow a sentence.
    """
    assert _redecode_len(["really"], ["important", "thing", "is"]) == 0
    assert _redecode_len(["Whispers"], ["Whisper's", "encoder"]) == 1
    assert _redecode_len(["auto-aggressive.", "One"], ["auto", "regressive", "one", "forward"]) == 3
    assert _redecode_len(["a", "b"], ["a", "b", "c", "d", "e", "f"]) <= 4
    assert _redecode_len([], ["anything"]) == 0


def test_cut_falls_back_to_the_boundary_check_below_the_anchor_floor():
    confirmed = [f"w{i}" for i in range(60)] + ["do", "things"]
    assert _cut(confirmed, ["do", "things", "that", "make", "you"]) == 2
    assert _cut(confirmed, ["that", "make", "you"]) == 0


def test_a_lost_anchor_realigns_on_the_pending_words_instead_of_stalling():
    """Measured 2026-09-12: a stall long enough for the confirmed tail to roll
    out of the window used to be permanent, because the prefix comparison
    needs pending and candidate to start at the same word and nothing put
    them back in step. Requiring four cycles of agreement on real speech
    dropped 75 of 141 words this way.
    """
    stitcher = Stitcher()
    push_all(stitcher, "the quick brown fox", "the quick brown fox")
    # A decode that no longer contains the confirmed words, whose pending
    # tail then reappears one word further in.
    stitcher.push("jumps over the lazy dog")
    assert stitcher.pending == ["jumps", "over", "the", "lazy", "dog"]
    printed = stitcher.push("it jumps over the lazy dog tonight")
    assert printed == ["jumps", "over", "the", "lazy", "dog"]
    assert stitcher.pending == ["tonight"]


def test_a_pending_word_the_new_decode_dropped_is_let_go():
    stitcher = Stitcher()
    push_all(stitcher, "the quick brown fox", "the quick brown fox")
    stitcher.push("um jumps over the lazy dog")
    printed = stitcher.push("jumps over the lazy dog tonight")
    assert printed == ["jumps", "over", "the", "lazy", "dog"]
    assert _realign(["um", "jumps", "over"], ["jumps", "over", "x"]) == (0, 1)
    assert _realign(["jumps", "over"], ["it", "jumps", "over"]) == (1, 0)
    assert _realign(["jumps", "over"], ["nothing", "alike"]) == (0, 0)


def _fixture_session() -> tuple[list[str], list[str]]:
    import json
    import re
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures"
    transcripts = [
        json.loads(line)["transcript"]
        for line in (fixtures / "espeak_12s_session.jsonl").read_text().splitlines()
        if line.strip()
    ]
    reference = (fixtures / "espeak_12s_session.txt").read_text()
    words = (re.sub(r"[^a-z0-9']", "", token.lower()) for token in reference.split())
    return transcripts, [w for w in words if w]


def test_a_real_session_prints_no_phrase_twice():
    """77 real whisper-small.en decodes off the NPU, 2026-09-12: a 12s window
    sliding over 55s of espeak-ng reading a known text at the live loop's own
    pacing (0.66s mean decode, 0.70s hop). Before the fix the stitcher printed
    148 words for a 141-word text, among them "Whispers Whisper's encoder" and
    "out 12s rather 12's rather than". After it, 143: the two extra are Whisper
    hearing "short-form" and "threshold-lowering" as two words each.
    """
    transcripts, reference = _fixture_session()
    stitcher = Stitcher()
    printed = push_all(stitcher, *transcripts) + stitcher.flush()
    text = " ".join(printed)

    assert "Whispers Whisper's" not in text
    assert "rather 12's rather" not in text
    assert len(printed) <= len(reference) + 2


def test_a_phrase_that_recurs_in_the_confirmed_text_is_not_an_anchor():
    """Measured 2026-09-21 on the LibriVox reading time-stretched to 1.75x:
    after a stall rolled the confirmed tail out of the window, the only
    3-word match left was "man of large", from "a young man of large fortune"
    already on screen and "a single man of large fortune" in the new decode.
    Cutting there threw away the 19 words before it, and with them 47 words
    Whisper had decoded correctly five times over. A real overlap carries
    most of the text in front of it; a recurring phrase carries none.
    """
    on_screen = (
        "Mrs. Long says that Netherfield is taken by a young man of large fortune "
        "from the north of England, that he came down on Monday in a chaise and "
        "four to see the place, and was so much delighted with it, that"
    )
    decoded = (
        "What is his name? Bingley. Is he married or single? Oh, single, my dear, "
        "to be sure. A single man of large fortune."
    )
    confirmed, curr = on_screen.split(), decoded.split()

    assert _cut(confirmed, curr) == 0
