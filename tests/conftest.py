"""Shared fixtures.

Everything under tests/ runs without an NPU, without an audio server and
without OpenVINO installed at all. That is a hard constraint, not a
preference: CI has none of the three, and a test suite that only runs on one
laptop is the test suite this project spent its first months not having.

So: no test imports openvino (transcriber and server import it lazily), and
anything that would shell out to pw-record/pactl is monkeypatched at the
capture-module boundary.
"""

import os
from collections import namedtuple

import pytest

from vinowhisper import config, devices

_Version = namedtuple("_Version", "major minor micro releaselevel serial")


def fake_version(major: int, minor: int, micro: int = 0) -> _Version:
    """A stand-in for sys.version_info that supports both .major and slicing."""
    return _Version(major, minor, micro, "final", 0)


@pytest.fixture(autouse=True)
def no_real_cleanup_settings(monkeypatch, tmp_path):
    """Dictation reads the user's cleanup.json and model; a test must never see either."""
    monkeypatch.setattr(config, "CLEANUP_FILE", tmp_path / "no-cleanup.json")
    monkeypatch.setattr(config, "CLEANUP_MODEL_DIR", tmp_path / "no-cleanup-model")
    monkeypatch.setattr(config, "CLEANUP_CACHE_DIR", tmp_path / "no-cleanup-cache")
    for name in list(os.environ):
        if name.startswith("VINOWHISPER_CLEANUP"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def no_real_language_settings(monkeypatch, tmp_path):
    """The saved language picks the model and export; a test must never see the user's."""
    monkeypatch.setattr(config, "LANGUAGE_FILE", tmp_path / "no-language.json")


@pytest.fixture(autouse=True)
def no_real_sessions(monkeypatch, tmp_path_factory):
    """Saved sessions, summary settings and the summariser's state are the user's; never touch them.

    Automatic summaries start off here, so a caption test never posts to a real server.
    """
    # Its own directory: plenty of tests assert that tmp_path is left empty.
    home = tmp_path_factory.mktemp("sessions")
    settings = home / "no-summaries.json"
    settings.write_text('{"auto": false}', encoding="utf-8")
    monkeypatch.setattr(config, "SUMMARIES_FILE", settings)
    monkeypatch.setattr(config, "SESSIONS_STATE_FILE", home / "no-sessions-state.json")
    monkeypatch.setattr(config, "TRANSCRIPT_DIR", home / "no-transcripts")


@pytest.fixture(autouse=True)
def no_dictation_preview(monkeypatch):
    monkeypatch.setattr(config, "PREVIEW_EVERY_S", 1e9)


@pytest.fixture(autouse=True)
def no_real_hardware(monkeypatch, tmp_path_factory):
    """The PCI bus and OpenCL ICDs of whatever machine runs the suite are not fixtures."""
    empty = tmp_path_factory.mktemp("sys")
    monkeypatch.setattr(devices, "PCI_DIR", empty / "pci")
    monkeypatch.setattr(devices, "OPENCL_VENDORS_DIR", empty / "icd")


@pytest.fixture(autouse=True)
def no_user_replacements(monkeypatch, tmp_path_factory):
    monkeypatch.setattr(
        config, "REPLACEMENTS_FILE", tmp_path_factory.mktemp("cfg") / "replacements.json"
    )


@pytest.fixture
def fake_os_release(tmp_path):
    """Write an /etc/os-release and hand back its path."""

    def write(**values: str):
        path = tmp_path / "os-release"
        path.write_text(
            "\n".join(f'{key}="{value}"' for key, value in values.items()), encoding="utf-8"
        )
        return path

    return write


@pytest.fixture
def inventory():
    """Build a device inventory the way OpenVINO would report one."""

    def make(*kinds: str) -> list[devices.Device]:
        names = {"NPU": "Intel(R) AI Boost", "GPU": "Intel(R) Arc(TM) Graphics", "CPU": "12th Gen"}
        return [
            devices.Device(name=kind, kind=kind, full_name=names.get(kind, "")) for kind in kinds
        ]

    return make
