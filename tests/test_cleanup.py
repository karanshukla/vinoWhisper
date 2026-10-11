"""The LLM cleanup step: what it asks for, and when its answer is not used.

The transport is a fake, so nothing here touches the network or a wallet.
"""

import json
import threading

import pytest

from vinowhisper import cleanup

SETTINGS = cleanup.Settings(api_key="sk-test", budget_s=0.5, mode="always")


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


def test_no_key_means_no_request():
    called = []
    settings = cleanup.Settings(api_key="")
    outcome = cleanup.clean_up("hello", settings, lambda s, t: called.append(t) or t)
    assert called == []
    assert outcome.reason == "no api key"


def test_the_budget_is_a_hard_deadline():
    release = threading.Event()

    def slow(settings, text):
        release.wait(5.0)
        return text

    fast = cleanup.Settings(api_key="k", budget_s=0.05, mode="always")
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


def test_characterization_the_request_turns_thinking_off():
    """characterization: thinking tokens count against max_tokens.

    Measured 2026-10-11 on claude-haiku-5-5: a ten-token answer spent about a hundred
    on a thinking block and stopped at max_tokens, so a correct answer was thrown away
    as "truncated". Leaving it on also adds output tokens to every dictation.
    """
    body = cleanup.request_body(SETTINGS, "one two three")
    assert body["thinking"] == {"type": "disabled"}


def test_the_request_has_no_sampling_parameters_and_a_bounded_output():
    body = cleanup.request_body(SETTINGS, "one two three")
    assert "temperature" not in body
    assert body["messages"] == [
        {"role": "user", "content": "<transcript>one two three</transcript>"}
    ]
    assert body["system"] == cleanup.PROMPT
    assert body["max_tokens"] <= 4096


def test_the_response_text_blocks_are_joined_and_truncation_is_refused():
    ok = json.dumps({"content": [{"type": "text", "text": " Hello "}], "stop_reason": "end_turn"})
    assert cleanup.parse_response(ok.encode()) == "Hello"
    echoed = json.dumps({"content": [{"type": "text", "text": "<transcript>Hi</transcript>"}]})
    assert cleanup.parse_response(echoed.encode()) == "Hi"
    cut = json.dumps({"content": [{"type": "text", "text": "Hel"}], "stop_reason": "max_tokens"})
    with pytest.raises(ValueError, match="truncated"):
        cleanup.parse_response(cut.encode())


def test_off_unless_asked_and_the_env_key_beats_the_wallet(tmp_path):
    assert cleanup.load_settings({}, path=tmp_path / "missing.json") is None
    env = {cleanup.ENABLE_ENV: "1", cleanup.KEY_ENV: "from-env"}
    assert cleanup.load_settings(env, lookup=lambda: "from-wallet").api_key == "from-env"
    only_wallet = {cleanup.ENABLE_ENV: "1"}
    assert cleanup.load_settings(only_wallet, lambda: "from-wallet").api_key == "from-wallet"


def test_the_default_budget_leaves_room_over_a_typical_call():
    assert cleanup.DEFAULT_BUDGET_S >= 3.0


def test_a_garbled_budget_keeps_the_default():
    env = {cleanup.ENABLE_ENV: "1", cleanup.BUDGET_ENV: "soon"}
    assert cleanup.load_settings(env, lambda: "").budget_s == cleanup.DEFAULT_BUDGET_S


def test_the_endpoint_is_the_messages_path_under_the_base_url():
    custom = cleanup.Settings(base_url="http://localhost:8080/", api_key="k")
    assert custom.endpoint == "http://localhost:8080/v1/messages"


def _file(tmp_path, **fields):
    path = tmp_path / "cleanup.json"
    path.write_text(json.dumps(fields))
    return path


def test_the_settings_file_turns_it_on_and_supplies_the_endpoint(tmp_path):
    path = _file(tmp_path, enabled=True, url="http://localhost:9", model="m", budget_s=1.5)
    settings = cleanup.load_settings({}, lambda: "wallet-key", path)
    assert settings == cleanup.Settings("http://localhost:9", "m", 1.5, "wallet-key")


def test_an_environment_variable_beats_the_file(tmp_path):
    path = _file(tmp_path, enabled=True, url="http://file", model="file-model")
    env = {cleanup.URL_ENV: "http://env", cleanup.MODEL_ENV: "env-model"}
    settings = cleanup.load_settings(env, lambda: "k", path)
    assert (settings.base_url, settings.model) == ("http://env", "env-model")
    off = {cleanup.ENABLE_ENV: "0"}
    assert cleanup.load_settings(off, lambda: "k", path) is None


def test_a_blank_or_broken_file_is_off_not_an_error(tmp_path):
    path = tmp_path / "cleanup.json"
    path.write_text("{not json")
    assert cleanup.load_settings({}, lambda: "k", path) is None
    assert cleanup.load_settings({}, lambda: "k", tmp_path / "absent.json") is None
    blank = _file(tmp_path, enabled=True, url="", model="")
    settings = cleanup.load_settings({}, lambda: "k", blank)
    assert (settings.base_url, settings.model) == (cleanup.DEFAULT_URL, cleanup.DEFAULT_MODEL)


def test_the_wallet_entry_matches_what_the_settings_window_writes():
    from pathlib import Path

    source = (Path(__file__).parent.parent / "settings/src/lib.rs").read_text()
    for part in cleanup.SECRET_ATTRIBUTES:
        assert f'"{part}"' in source


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

    smart = cleanup.Settings(api_key="k", mode="smart")
    skipped = cleanup.clean_up("Send the report.", smart, transport)
    assert (skipped.cleaned, skipped.reason, sent) == (False, "nothing to fix", [])
    always = cleanup.Settings(api_key="k", mode="always")
    cleanup.clean_up("Send the report.", always, transport)
    assert sent == ["Send the report."]


def test_the_mode_comes_from_the_file_and_an_unknown_one_is_smart(tmp_path):
    path = tmp_path / "cleanup.json"
    path.write_text(json.dumps({"enabled": True, "mode": "always"}))
    assert cleanup.load_settings({}, lambda: "k", path).mode == "always"
    path.write_text(json.dumps({"enabled": True, "mode": "sometimes"}))
    assert cleanup.load_settings({}, lambda: "k", path).mode == "smart"


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
