"""The failed-device mark: what the server writes when a device that
enumerated cannot build a pipeline, and what everything else reads.

The rule that matters most is the default. A file that is missing, empty,
corrupt or the wrong shape means nothing has failed: the NPU stays in play
unless something has said, in so many words, that it did not work.
"""

import json

import pytest

from vinowhisper import config, failures


@pytest.fixture
def state_file(monkeypatch, tmp_path):
    path = tmp_path / "config" / "vinowhisper" / "failed-devices.json"
    monkeypatch.setattr(config, "FAILED_DEVICES_FILE", path)
    return path


def test_no_file_means_nothing_failed(state_file):
    assert failures.load() == {}


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"NPU": "yes"}', '{"NPU": {}}'])
def test_anything_unreadable_also_means_nothing_failed(state_file, content):
    state_file.parent.mkdir(parents=True)
    state_file.write_text(content)
    assert failures.load() == {}


def test_a_recorded_failure_survives_a_reload(state_file):
    recorded = failures.record("NPU", "Missing upper bound for one or more nodes")
    loaded = failures.load()
    assert loaded == {"NPU": recorded}
    assert loaded["NPU"].error == "Missing upper bound for one or more nodes"
    assert loaded["NPU"].failed_at.startswith("20")


def test_the_error_is_the_message_not_openvino_s_location_headers(state_file):
    error = (
        "\nException from src/inference/src/cpp/core.cpp:107:\n"
        "Exception from src/inference/src/dev/plugin.cpp:53:\n"
        "Missing upper bound for one or more nodes\n" + "x" * 500
    )
    failure = failures.record("NPU", error)
    assert failure.error == "Missing upper bound for one or more nodes"


def test_the_error_is_bounded(state_file):
    assert failures.record("NPU", "y" * 500).error == "y" * 200


def test_a_message_that_is_only_headers_keeps_the_first(state_file):
    error = "Exception from src/a.cpp:1:\nException from src/b.cpp:2:"
    assert failures.record("NPU", error).error == "Exception from src/a.cpp:1:"


def test_the_directory_is_created_and_the_file_is_plain_json(state_file):
    failures.record("GPU.1", "boom")
    assert json.loads(state_file.read_text())["GPU.1"]["error"] == "boom"


def test_devices_are_marked_independently(state_file):
    failures.record("NPU", "one")
    failures.record("GPU", "two")
    failures.forget("NPU")
    assert list(failures.load()) == ["GPU"]


def test_forgetting_an_unmarked_device_writes_nothing(state_file):
    failures.forget("NPU")
    assert not state_file.exists()


def test_clear_returns_what_it_removed(state_file):
    failures.record("NPU", "one")
    cleared = failures.clear()
    assert [failure.device for failure in cleared] == ["NPU"]
    assert failures.load() == {}
    assert failures.clear() == []


def test_a_failure_reads_as_one_line():
    failure = failures.Failure("NPU", "2026-09-16T17:00:00+00:00", "compile failed")
    assert str(failure) == "NPU failed on 2026-09-16T17:00:00+00:00: compile failed"
