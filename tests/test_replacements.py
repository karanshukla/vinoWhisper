import json

import pytest

from tests.test_dictate import SPEECH, FakeClient, _run
from vinowhisper import config, replacements


def test_match_is_case_insensitive_and_inserts_the_value_as_written():
    mapping = {"open vino": "OpenVINO"}
    assert replacements.apply("I use Open Vino daily", mapping) == "I use OpenVINO daily"
    assert replacements.apply("OPEN VINO", mapping) == "OpenVINO"


def test_a_longer_word_is_never_touched():
    mapping = {"vino": "OpenVINO"}
    assert (
        replacements.apply("a vinous wine, vino, devino", mapping)
        == "a vinous wine, OpenVINO, devino"
    )


def test_multi_word_keys_match_across_whitespace():
    mapping = {"vino whisper": "vinoWhisper"}
    assert replacements.apply("try vino  whisper now", mapping) == "try vinoWhisper now"
    assert replacements.apply("vino", mapping) == "vino"


def test_longest_key_wins():
    mapping = {"open": "X", "open vino": "OpenVINO", "open vino toolkit": "OV Toolkit"}
    assert replacements.apply("open vino toolkit and open vino and open", mapping) == (
        "OV Toolkit and OpenVINO and X"
    )


def test_punctuation_stays_attached():
    mapping = {"open vino": "OpenVINO"}
    assert replacements.apply('Open vino. (open vino), "open vino"!', mapping) == (
        'OpenVINO. (OpenVINO), "OpenVINO"!'
    )


def test_the_replacement_is_literal():
    assert replacements.apply("price", {"price": r"\1 $& \g<0>"}) == r"\1 $& \g<0>"


def test_non_ascii_keys_and_values():
    mapping = {"café noir": "Café Noir ☕", "naïve": "naive"}
    assert replacements.apply("Un CAFÉ NOIR, naïve.", mapping) == "Un Café Noir ☕, naive."
    assert replacements.apply("naïvety", mapping) == "naïvety"


def test_keys_with_symbols_match_whole():
    assert replacements.apply("use k8s, not k8sx", {"k8s": "Kubernetes"}) == (
        "use Kubernetes, not k8sx"
    )


def test_an_empty_table_or_text_is_a_no_op():
    assert replacements.apply("hello", {}) == "hello"
    assert replacements.apply("", {"a": "b"}) == ""


def test_a_missing_file_is_no_replacements(tmp_path, capsys):
    assert replacements.load(tmp_path / "nope.json") == {}
    assert capsys.readouterr().err == ""


def test_a_valid_file_loads(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"k8s": "Kubernetes", "naïve": "naive"}), encoding="utf-8")
    assert replacements.load(path) == {"k8s": "Kubernetes", "naïve": "naive"}


@pytest.mark.parametrize(
    "content, problem",
    [
        ("{not json", "Expecting"),
        ("[1, 2]", "object"),
        ('{"a": 1}', "'a'"),
        ('{"a": null}', "'a'"),
    ],
)
def test_a_broken_file_warns_and_loads_nothing(tmp_path, capsys, content, problem):
    path = tmp_path / "r.json"
    path.write_text(content)
    assert replacements.load(path) == {}
    err = capsys.readouterr().err
    assert str(path) in err
    assert problem in err


def test_dictation_applies_the_file_and_rereads_it_on_each_start(tmp_path):
    config.REPLACEMENTS_FILE.write_text(json.dumps({"hello": "Howdy"}))
    records, _, _ = _run(["start", "stop"], client=FakeClient())
    assert records[-1]["text"] == "Howdy there."

    config.REPLACEMENTS_FILE.write_text(json.dumps({"there": "y'all"}))
    records, _, _ = _run(["start", "stop"], client=FakeClient())
    assert records[-1]["text"] == "Hello y'all."


def test_dictation_survives_a_broken_file(capsys):
    config.REPLACEMENTS_FILE.write_text("{oops")
    records, _, _ = _run(["start", "stop"], signal=SPEECH)
    assert records[-1] == {**records[-1], "event": "Dictated", "text": "Hello there."}
    assert not any(r["event"] == "Error" for r in records)
    assert str(config.REPLACEMENTS_FILE) in capsys.readouterr().err
