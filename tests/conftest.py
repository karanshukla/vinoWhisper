"""Shared fixtures.

Everything under tests/ runs without an NPU, without an audio server and
without OpenVINO installed at all. That is a hard constraint, not a
preference: CI has none of the three, and a test suite that only runs on one
laptop is the test suite this project spent its first months not having.

So: no test imports vinowhisper.transcriber or vinowhisper.server, and
anything that would shell out to pw-record/pactl is monkeypatched at the
capture-module boundary.
"""

from collections import namedtuple

import pytest

from vinowhisper import devices

_Version = namedtuple("_Version", "major minor micro releaselevel serial")


def fake_version(major: int, minor: int, micro: int = 0) -> _Version:
    """A stand-in for sys.version_info that supports both .major and slicing."""
    return _Version(major, minor, micro, "final", 0)


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
