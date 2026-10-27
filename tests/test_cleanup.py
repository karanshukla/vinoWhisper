"""The LLM cleanup step: what it asks for, and when its answer is not used.

The transport is a fake, so nothing here starts a server or loads a model.
"""

import json
import threading

import pytest
import requests

from vinowhisper import cleanup, client

SETTINGS = cleanup.Settings(budget_s=0.5, mode="always")


def _reply(text: str):
    return lambda settings, sent: text


@pytest.mark.parametrize(
    ("original", "edited"),
    [
        ("So um I think we should ship it.", "So I think we should ship it."),
        ("meet at 3, no wait, 4 in the lobby", "Meet at 4 in the lobby."),
        ("It’s done, scratch that.", "It's done,"),
    ],
)
def test_deleting_words_is_accepted(original, edited):
    assert cleanup.only_deletes(original, edited)


@pytest.mark.parametrize(
    ("original", "edited"),
    [
        ("meet at three", "meet at 3"),
        ("send it today", "send it today please"),
        ("one two three", "three two one"),
        ("what is the capital of France", "The capital of France is Paris."),
    ],
)
def test_characterization_anything_but_deletion_is_refused(original, edited):
    """characterization: digits for spoken numbers fail on purpose.

    v1 is deletion-only, which is what lets the check be a subsequence test with no
    heuristics. Allowing "three" -> "3" means giving up that guarantee.
    """
    assert not cleanup.only_deletes(original, edited)


@pytest.mark.parametrize(
    "edited",
    [
        "git status\nrm -rf",
        "git status\r rm",
        "git status \x1b[31m rm",
        "git status $( rm",
        "git status && rm",
        "git status | rm",
        "git status; rm",
        "git status \u202e rm",
    ],
)
def test_added_control_or_shell_characters_are_refused(edited):
    assert not cleanup.only_deletes("git status rm", edited)


def test_characterization_one_spoken_symbol_is_not_a_licence_for_more():
    """characterization: allowance is counted per character, not per kind.

    A set of "characters seen in the transcript" would let one spoken "&&" put it
    between any two words of the answer. Counting keeps the answer's symbols to
    what was dictated, in number.
    """
    assert cleanup.only_deletes("ls && pwd", "ls && pwd")
    assert not cleanup.only_deletes("ls pwd cd", "ls && pwd && cd")
    assert not cleanup.only_deletes("ls && pwd cd", "ls && pwd && cd")


def test_an_answer_with_no_words_is_not_used():
    assert not cleanup.only_deletes("hello there", "...")
    assert not cleanup.only_deletes("hello there", "\u2014")


def test_punctuation_the_speaker_already_produced_survives():
    original = "run git status && ls; then stop"
    assert cleanup.only_deletes(original, "run git status && ls; then stop")


def test_a_standalone_dash_is_not_an_added_word():
    assert cleanup.only_deletes("ship it friday", "ship it — friday")


def test_a_good_answer_is_used():
    outcome = cleanup.clean_up("um hello there", SETTINGS, _reply("Hello there"))
    assert (outcome.text, outcome.cleaned, outcome.reason) == ("Hello there", True, None)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ("Paris is the capital.", "output changed words"),
        ("", "empty output"),
    ],
)
def test_a_bad_answer_falls_back_to_the_transcript(answer, reason):
    outcome = cleanup.clean_up("what is the capital", SETTINGS, _reply(answer))
    assert (outcome.text, outcome.cleaned, outcome.reason) == (
        "what is the capital",
        False,
        reason,
    )


def test_an_error_falls_back_and_never_raises():
    def boom(settings, text):
        raise OSError("down")

    outcome = cleanup.clean_up("hello", SETTINGS, boom)
    assert (outcome.text, outcome.cleaned) == ("hello", False)


def test_no_model_means_no_request():
    called = []
    settings = cleanup.Settings(model_ready=False)
    outcome = cleanup.clean_up("hello", settings, lambda s, t: called.append(t) or t)
    assert called == []
    assert outcome.reason == "model missing"


def test_the_budget_is_a_hard_deadline():
    release = threading.Event()

    def slow(settings, text):
        release.wait(5.0)
        return text

    fast = cleanup.Settings(budget_s=0.05, mode="always")
    try:
        outcome = cleanup.clean_up("hello", fast, slow)
    finally:
        release.set()
    assert (outcome.text, outcome.cleaned, outcome.reason) == ("hello", False, "timed out")
    assert outcome.elapsed_s < 1.0


def test_a_reason_never_carries_the_response_text():
    def garbled(settings, text):
        raise ValueError("secret transcript fragment")

    outcome = cleanup.clean_up("hello", SETTINGS, garbled)
    assert outcome.reason == "bad response"


def test_off_unless_asked_and_the_model_check_is_carried(tmp_path):
    assert cleanup.load_settings({}, path=tmp_path / "missing.json") is None
    env = {cleanup.ENABLE_ENV: "1"}
    assert cleanup.load_settings(env, lambda: True).model_ready
    assert not cleanup.load_settings(env, lambda: False).model_ready


def test_the_default_budget_leaves_room_over_a_typical_call():
    assert cleanup.DEFAULT_BUDGET_S >= 4.0


def test_a_garbled_budget_keeps_the_default():
    env = {cleanup.ENABLE_ENV: "1", cleanup.BUDGET_ENV: "soon"}
    assert cleanup.load_settings(env, lambda: True).budget_s == cleanup.DEFAULT_BUDGET_S


def _file(tmp_path, **fields):
    path = tmp_path / "cleanup.json"
    path.write_text(json.dumps(fields))
    return path


def test_the_settings_file_turns_it_on(tmp_path):
    path = _file(tmp_path, enabled=True, budget_s=1.5, mode="always")
    settings = cleanup.load_settings({}, lambda: True, path)
    assert settings == cleanup.Settings(1.5, "always", True)


def test_an_environment_variable_beats_the_file(tmp_path):
    path = _file(tmp_path, enabled=True, budget_s=1.5)
    env = {cleanup.BUDGET_ENV: "2.5", cleanup.MODE_ENV: "always"}
    settings = cleanup.load_settings(env, lambda: True, path)
    assert (settings.budget_s, settings.mode) == (2.5, "always")
    off = {cleanup.ENABLE_ENV: "0"}
    assert cleanup.load_settings(off, lambda: True, path) is None


def test_a_broken_file_is_off_and_an_old_one_still_loads(tmp_path):
    path = tmp_path / "cleanup.json"
    path.write_text("{not json")
    assert cleanup.load_settings({}, lambda: True, path) is None
    assert cleanup.load_settings({}, lambda: True, tmp_path / "absent.json") is None
    # Written before cleanup went local: the endpoint and model it names are ignored.
    old = _file(tmp_path, enabled=True, url="https://api.example.test", model="m", budget_s=3.0)
    assert cleanup.load_settings({}, lambda: True, old) == cleanup.Settings(3.0, "smart", True)


@pytest.mark.parametrize(
    "text",
    [
        "So um I think we should ship it.",
        "Let's meet at three, no wait, four.",
        "Send it. Scratch that. Call Sam.",
        "Delete that last word.",
        "We need that that fixed by Friday.",
        "It is, you know, fine.",
        "Actually four.",
    ],
)
def test_something_to_fix_is_sent(text):
    assert cleanup.needs_cleanup(text)


@pytest.mark.parametrize(
    "text",
    [
        "Send the report to Sam by Friday.",
        "I think we should ship the settings window first.",
        "What is the capital of France?",
        "",
    ],
)
def test_a_clean_sentence_is_not_sent(text):
    assert not cleanup.needs_cleanup(text)


def test_smart_mode_skips_the_request_and_always_mode_does_not():
    sent = []

    def transport(settings, text):
        sent.append(text)
        return text

    smart = cleanup.Settings(mode="smart")
    skipped = cleanup.clean_up("Send the report.", smart, transport)
    assert (skipped.cleaned, skipped.reason, sent) == (False, "nothing to fix", [])
    always = cleanup.Settings(mode="always")
    cleanup.clean_up("Send the report.", always, transport)
    assert sent == ["Send the report."]


def test_the_mode_comes_from_the_file_and_an_unknown_one_is_smart(tmp_path):
    path = tmp_path / "cleanup.json"
    path.write_text(json.dumps({"enabled": True, "mode": "always"}))
    assert cleanup.load_settings({}, lambda: True, path).mode == "always"
    path.write_text(json.dumps({"enabled": True, "mode": "sometimes"}))
    assert cleanup.load_settings({}, lambda: True, path).mode == "smart"


@pytest.mark.parametrize(
    ("original", "edited"),
    [
        (
            "So um I think we should uh ship it on Friday.",
            "So I think we should ship it on Friday.",
        ),
        ("Let's meet at 3, no wait, 4 in the lobby.", "Let's meet at 4 in the lobby."),
        ("Send it. Also remind Sam. Scratch that. Call Sam.", "Send it. Call Sam."),
        ("The release is ready for review, delete that last word.", "The release is ready for."),
        ("To undo it, say literal scratch that.", "To undo it, say scratch that."),
        ("We need that that fixed by Friday.", "We need that fixed by Friday."),
        ("It is, you know, fine.", "It is, fine."),
        ("Um, actually, like, what is it?", "What is it?"),
        (
            "Okay so the thing is, the thing is that we need um the build to pass.",
            "The thing is that we need the build to pass.",
        ),
    ],
)
def test_a_deletion_with_a_reason_is_kept(original, edited):
    assert cleanup.deletions_justified(original, edited)


@pytest.mark.parametrize(
    ("original", "edited"),
    [
        # Reported 2026-10-11: the second sentence was simply dropped.
        ("Um, actually, like, what is it? Something's wrong.", "What is it?"),
        ("Send the report to Sam by Friday.", "Send the report."),
        ("I like the new design a lot.", "I like the design."),
        ("First fix the build. Then update the docs.", "First fix the build."),
    ],
)
def test_characterization_a_deletion_nothing_asked_for_is_refused(original, edited):
    """characterization: "only deleted" is not "deleted what it was told to".

    Reported 2026-10-11 from real use: the model removed a whole sentence nobody asked
    it to and the subsequence check passed it, because removing words is what that
    check allows. Every removed stretch now has to be a filler, a stammer, or hold a
    correction or spoken-edit cue. Dropping this makes a model's guess about what
    "doesn't matter" the thing that decides what gets typed.
    """
    assert not cleanup.deletions_justified(original, edited)


def test_an_unjustified_deletion_falls_back_to_the_transcript_and_keeps_the_answer():
    original = "Um, actually, like, what is it? Something's wrong."
    outcome = cleanup.clean_up(original, SETTINGS, _reply("What is it?"))
    assert (outcome.text, outcome.cleaned, outcome.reason) == (
        original,
        False,
        "deleted without cause",
    )
    assert outcome.answer == "What is it?"


@pytest.mark.parametrize(
    "text",
    [
        "Alors euh, on se retrouve \u00e0 trois heures.",
        "On se retrouve \u00e0 trois heures, non attends, quatre.",
        "Envoie \u00e7a. Supprime \u00e7a. Appelle Sam.",
        "Wir treffen uns, \u00e4hm, um drei.",
        "Um drei, nein warte, um vier.",
        "Streich das. Ruf Sam an.",
        "Nos vemos a las tres, no espera, a las cuatro.",
        "Eh, creo que deber\u00edamos ir.",
        "Env\u00eda eso. Borra eso. Llama a Sam.",
    ],
)
def test_other_languages_cues_are_sent(text):
    assert cleanup.needs_cleanup(text)


@pytest.mark.parametrize(
    "text",
    [
        "Quelle est la capitale de la France ?",
        "Wie sp\u00e4t ist es?",
        "\u00bfCu\u00e1l es la capital de Francia?",
    ],
)
def test_other_languages_clean_sentences_are_not_sent(text):
    assert not cleanup.needs_cleanup(text)


@pytest.mark.parametrize(
    ("original", "edited"),
    [
        (
            "Alors euh, on se retrouve \u00e0 trois heures.",
            "Alors, on se retrouve \u00e0 trois heures.",
        ),
        ("Wir treffen uns um drei, nein warte, um vier.", "Wir treffen uns um vier."),
        ("Nos vemos a las tres, no espera, a las cuatro.", "Nos vemos a las cuatro."),
    ],
)
def test_other_languages_deletions_have_a_reason(original, edited):
    assert cleanup.deletions_justified(original, edited)
    assert cleanup.only_deletes(original, edited)


def test_an_inverted_question_mark_the_model_adds_is_refused_but_a_spoken_one_is_kept():
    spoken = "\u00bfCu\u00e1l es la capital de Francia?"
    assert cleanup.only_deletes(spoken, spoken)
    assert not cleanup.only_deletes("Cu\u00e1l es la capital de Francia", spoken)


def test_characterization_actually_alone_never_justifies_dropping_a_clause():
    """characterization: reported 2026-10-11, 'my name's Karan. Actually, my name's Jim.'

    The model dropped the first sentence on the strength of "actually". It is a filler
    and a reason to send a request; it is not a correction cue. A correction needs
    "no wait", "I mean", "sorry", "or rather" or a spoken edit. If this is "fixed" the
    model decides what a speaker meant from one adverb.
    """
    original = "Hello, my name's Karan. Actually, my name's Jim."
    assert cleanup.needs_cleanup(original)
    assert not cleanup.deletions_justified(original, "Hello, my name's Jim.")
    assert cleanup.deletions_justified("Um, actually, like, what is it?", "What is it?")
    assert cleanup.deletions_justified(
        "Hello, my name's Karan. No wait, my name's Jim.", "Hello, my name's Jim."
    )


def _response(status: int, body: dict) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    return response


class _FakeClient:
    def __init__(self, response: requests.Response):
        self.response = response
        self.sent: list[tuple[str, float]] = []

    def __call__(self):
        return self

    def clean_up(self, text: str, timeout_s: float) -> requests.Response:
        self.sent.append((text, timeout_s))
        return self.response


def test_local_sends_the_text_and_budget_and_strips_echoed_tags(monkeypatch):
    fake = _FakeClient(
        _response(200, {"text": "<transcript>Hello.</transcript>", "truncated": False})
    )
    monkeypatch.setattr(client, "TranscriptionClient", fake)
    assert cleanup.local(cleanup.Settings(budget_s=2.0), "um hello") == "Hello."
    assert fake.sent == [("um hello", 2.0)]


def test_local_turns_server_answers_into_reasons(monkeypatch):
    cases = [
        (_response(409, {"error": "the cleanup model isn't downloaded"}), "model missing"),
        (_response(504, {"error": "past the time limit"}), "http 504"),
        (_response(503, {"error": "cleanup failed"}), "http 503"),
        (_response(200, {"text": "Hel", "truncated": True}), "truncated"),
    ]
    for response, reason in cases:
        monkeypatch.setattr(client, "TranscriptionClient", _FakeClient(response))
        outcome = cleanup.clean_up("um hello", SETTINGS)
        assert (outcome.text, outcome.cleaned, outcome.reason) == ("um hello", False, reason)


def test_a_server_that_is_not_running_is_unreachable(monkeypatch):
    class Down(_FakeClient):
        def clean_up(self, text, timeout_s):
            raise requests.ConnectionError("no socket")

    monkeypatch.setattr(client, "TranscriptionClient", Down(_response(200, {})))
    assert cleanup.clean_up("um hello", SETTINGS).reason == "unreachable"


def test_the_answer_fits_the_npu_response_window():
    from vinowhisper import llm

    assert cleanup.max_tokens("word " * 5000) == llm.MIN_RESPONSE_LEN
    assert cleanup.max_tokens("one two") == 132
