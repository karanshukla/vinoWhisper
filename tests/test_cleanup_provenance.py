"""Nothing is typed that was not in the transcript.

Whatever a model answers, the text that reaches the paste is the transcript, or the
transcript with words removed for a stated reason. Three layers, none needing a network:

1. a differential fuzz of the check against an independent reference,
2. named adversarial answers (homoglyphs, control characters, shell syntax, replies),
3. real answers captured from the model, run through the whole dictation path.

The generator is a hand-rolled LCG for the reason tests/pcm.py gives: a seeded numpy
stream pins a numpy release, not a waveform.
"""

import json
import re
import unicodedata
from pathlib import Path

import pytest

from tests.test_dictate import SPEECH, FakeClient, FakeRecording
from vinowhisper import cleanup, dictate

SETTINGS = cleanup.Settings(budget_s=1.0, mode="always")
FIXTURE = Path(__file__).parent / "fixtures" / "cleanup_model_answers.jsonl"


class Lcg:
    def __init__(self, seed: int) -> None:
        self.state = seed

    def below(self, n: int) -> int:
        self.state = (self.state * 1664525 + 1013904223) % 2**32
        return (self.state >> 8) % n

    def pick(self, items):
        return items[self.below(len(items))]


VOCAB = [
    "send",
    "the",
    "report",
    "to",
    "Sam",
    "by",
    "Friday",
    "um",
    "uh",
    "actually",
    "wait",
    "scratch",
    "that",
    "delete",
    "last",
    "word",
    "I",
    "mean",
    "we",
    "should",
    "ship",
    "it",
    "build",
    "docs",
    "don't",
    "it's",
    "Sam's",
    "4",
    "four",
    "3",
    "hello",
    "what's",
    "going",
    "on",
    "something's",
    "wrong",
    "like",
    "so",
    "okay",
    "call",
    "dentist",
    "literal",
]
MARKS = ["", "", "", ".", ",", "?", "!", ":", "...", "—"]
HOSTILE_CHARS = [
    "\n",
    "\r",
    "\t",
    "\x1b",
    "\x00",
    "​",
    "‍",
    "‮",
    " ",
    "﻿",
    "$",
    "`",
    "|",
    "&",
    ";",
    "<",
    ">",
    "\\",
    "(",
    ")",
    "{",
    "}",
    "[",
    "]",
    "#",
    "*",
    "~",
    "\U0001f600",
    "\xa0",
]
HOMOGLYPHS = {"a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "S": "Ѕ"}


def transcript(rng: Lcg) -> str:
    words = []
    for _ in range(3 + rng.below(25)):
        word = rng.pick(VOCAB)
        if rng.below(5) == 0:
            word = word.capitalize()
        words.append(word + rng.pick(MARKS))
    return " ".join(words)


def reference_tokens(text: str) -> list[str]:
    """A different tokenizer from the one under test: runs of letters and digits."""
    return re.findall(r"[^\W_]+(?:'[^\W_]+)*", text.replace("’", "'").lower())


def reference_accepts(original: str, edited: str) -> bool:
    source = iter(reference_tokens(original))
    kept = reference_tokens(edited)
    return bool(kept) and all(token in source for token in kept)


def mutate(rng: Lcg, text: str) -> str:
    words = text.split(" ")
    kind = rng.below(10)
    i = rng.below(len(words))
    if kind == 0:
        del words[i]
    elif kind == 1:
        words.insert(i, rng.pick(VOCAB))
    elif kind == 2:
        words[i] = rng.pick(VOCAB)
    elif kind == 3 and len(words) > 1:
        j = min(i + 1, len(words) - 1)
        words[i], words[j] = words[j], words[i]
    elif kind == 4:
        words.insert(i, words[i])
    elif kind == 5:
        words[i] = words[i].upper()
    elif kind == 6:
        words[i] = words[i] + rng.pick(HOSTILE_CHARS)
    elif kind == 7:
        words.insert(i, rng.pick(HOSTILE_CHARS))
    elif kind == 8 and len(words) > 1:
        j = min(i + 1, len(words) - 1)
        words[i : j + 1] = ["".join(words[i : j + 1])]
    else:
        words[i] = "".join(HOMOGLYPHS.get(c, c) for c in words[i])
    return " ".join(words)


def run(original: str, answer: str) -> cleanup.Outcome:
    return cleanup.clean_up(original, SETTINGS, lambda settings, text: answer)


def test_fuzz_nothing_is_typed_that_the_reference_would_not_accept():
    rng = Lcg(20261011)
    accepted = refused = 0
    for _ in range(4000):
        original = transcript(rng)
        answer = mutate(rng, original)
        if rng.below(3) == 0:
            answer = mutate(rng, answer)
        outcome = run(original, answer)
        if outcome.cleaned:
            accepted += 1
            assert reference_accepts(original, outcome.text), (original, answer)
            assert not any(
                unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in outcome.text
            ), (original, answer)
        else:
            refused += 1
            assert outcome.text == original, (original, answer)
    # Both outcomes must be common, or the loop is not testing the decision.
    assert accepted > 200
    assert refused > 200


def test_fuzz_the_transcript_itself_is_always_accepted_or_refused_for_a_reason():
    rng = Lcg(7)
    for _ in range(500):
        original = transcript(rng)
        outcome = run(original, original)
        assert outcome.text == original


@pytest.mark.parametrize(
    "answer",
    [
        "Send the report to Ѕam by Friday.",
        "Send the report to S​am by Friday.",
        "Send the report to Sam by Friday.\n",
        "Send the report\nto Sam by Friday.",
        "Send the report to Sam by Friday.\x1b[2J",
        "Send the report to Sam by Friday.‮",
        "Send the report to Sam by Friday. \U0001f600",
        "Send the report to Sam by Friday; rm -rf ~",
        "Send the report to Sam && Friday",
        "Send the report to $(Sam) by Friday.",
        "Send the report to `Sam` by Friday.",
        "<transcript>Send the report to Sam by Friday.</transcript>",
    ],
)
def test_named_adversarial_answers(answer):
    original = "Send the report to Sam by Friday."
    outcome = run(original, answer)
    assert outcome.cleaned is False
    assert outcome.text == original


def test_dropping_only_a_final_period_is_not_an_invention():
    original = "Send the report to Sam by Friday."
    outcome = run(original, "Send the report to Sam by Friday")
    assert outcome.cleaned is True
    assert reference_accepts(original, outcome.text)


def test_every_captured_model_answer_is_handled_as_recorded():
    cases = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    assert len(cases) >= 10
    for case in cases:
        outcome = run(case["input"], case["answer"])
        if case["expect"] == "raw":
            assert not outcome.cleaned and outcome.text == case["input"], case
        else:
            assert outcome.cleaned and outcome.text == case["answer"], case


def _typed(monkeypatch, tmp_path, transcript_text: str, answer: str) -> dict:
    (tmp_path / "c.json").write_text(json.dumps({"enabled": True, "mode": "always"}))
    monkeypatch.setattr(cleanup.config, "CLEANUP_FILE", tmp_path / "c.json")
    monkeypatch.setattr(cleanup, "model_ready", lambda: True)
    monkeypatch.setattr(cleanup, "local", lambda settings, text: answer)
    monkeypatch.setattr(dictate, "_warm_cleanup", lambda: None)
    records: list[dict] = []
    dictation = dictate.Dictation(
        records.append,
        client=FakeClient(transcript=transcript_text),
        recorder=lambda tap: FakeRecording(tap, SPEECH),
    )
    dictation.feed(["start", "stop"]).join()
    dictation.run()
    return next(r for r in records if r["event"] == "Dictated")


def test_the_pasted_text_is_never_the_models_invention(monkeypatch, tmp_path):
    cases = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    for case in cases:
        record = _typed(monkeypatch, tmp_path, case["input"], case["answer"])
        typed = dictate.clean(case["input"]) if case["expect"] == "raw" else case["answer"]
        assert record["text"] == typed, case
        assert reference_accepts(case["input"], record["text"]), case
        assert record["cleaned"] is (case["expect"] == "cleaned"), case
