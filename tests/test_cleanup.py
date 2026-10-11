"""The LLM cleanup step: what it asks for, and when its answer is not used.

The transport is a fake, so nothing here touches the network or a wallet.
"""

import json
import threading

import pytest

from vinowhisper import cleanup

SETTINGS = cleanup.Settings(api_key="sk-test", budget_s=0.5)


def _reply(text: str):
    return lambda settings, sent: text


@pytest.mark.parametrize(
    ("original", "edited"),
    [
        ("So um I think we should ship it.", "So I think we should ship it."),
        ("meet at 3, no wait, 4 in the lobby", "Meet at 4 in the lobby."),
        ("It’s done, scratch that.", "It's done,"),
        ("a b c", ""),
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

    fast = cleanup.Settings(api_key="k", budget_s=0.05)
    try:
        outcome = cleanup.clean_up("hello", fast, slow)
    finally:
        release.set()
    assert (outcome.text, outcome.cleaned, outcome.reason) == ("hello", False, "timed out")
    assert outcome.elapsed_s < 1.0


def test_the_request_has_no_sampling_parameters_and_a_bounded_output():
    body = cleanup.request_body(SETTINGS, "one two three")
    assert "temperature" not in body
    assert body["messages"] == [{"role": "user", "content": "one two three"}]
    assert body["system"] == cleanup.PROMPT
    assert body["max_tokens"] <= 4096


def test_the_response_text_blocks_are_joined_and_truncation_is_refused():
    ok = json.dumps({"content": [{"type": "text", "text": " Hello "}], "stop_reason": "end_turn"})
    assert cleanup.parse_response(ok.encode()) == "Hello"
    cut = json.dumps({"content": [{"type": "text", "text": "Hel"}], "stop_reason": "max_tokens"})
    with pytest.raises(ValueError, match="truncated"):
        cleanup.parse_response(cut.encode())


def test_off_unless_asked_and_the_env_key_beats_the_wallet():
    assert cleanup.load_settings({}) is None
    env = {cleanup.ENABLE_ENV: "1", cleanup.KEY_ENV: "from-env"}
    assert cleanup.load_settings(env, lookup=lambda: "from-wallet").api_key == "from-env"
    only_wallet = {cleanup.ENABLE_ENV: "1"}
    assert cleanup.load_settings(only_wallet, lambda: "from-wallet").api_key == "from-wallet"


def test_a_garbled_budget_keeps_the_default():
    env = {cleanup.ENABLE_ENV: "1", cleanup.BUDGET_ENV: "soon"}
    assert cleanup.load_settings(env, lambda: "").budget_s == cleanup.DEFAULT_BUDGET_S


def test_the_endpoint_is_the_messages_path_under_the_base_url():
    custom = cleanup.Settings(base_url="http://localhost:8080/", api_key="k")
    assert custom.endpoint == "http://localhost:8080/v1/messages"
