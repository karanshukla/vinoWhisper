"""A release reports its number and a source build reports `dev` (docs/development.md)."""

import json
from importlib.metadata import PathDistribution

import pytest

from vinowhisper import version


def _dist(tmp_path, direct_url=None):
    info = tmp_path / str(len(list(tmp_path.iterdir()))) / "vinowhisper-1.8.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: vinowhisper\nVersion: 1.8.0\n")
    if direct_url is not None:
        (info / "direct_url.json").write_text(
            direct_url if isinstance(direct_url, str) else json.dumps(direct_url)
        )
    return PathDistribution(info)


WHEEL = {"url": "file:///tmp/vinowhisper-1.8.0-py3-none-any.whl", "archive_info": {"hashes": {}}}
RELEASE_URL = {
    "url": "https://github.com/karanshukla/vinoWhisper/releases/download/v1.8.0/x.whl",
    "archive_info": {},
}


def test_a_release_build_reports_its_version(tmp_path):
    assert version.resolve(_dist(tmp_path)) == "1.8.0"


@pytest.mark.parametrize("record", [WHEEL, RELEASE_URL], ids=["local wheel", "release url"])
def test_a_wheel_installed_by_path_or_url_is_a_release(tmp_path, record):
    """The release installer runs `uv tool install` on a downloaded wheel."""
    assert version.resolve(_dist(tmp_path, record)) == "1.8.0"


@pytest.mark.parametrize(
    "record",
    [
        {"url": "file:///home/dev/vinoWhisper", "dir_info": {}},
        {"url": "file:///home/dev/vinoWhisper", "dir_info": {"editable": True}},
        {"url": "https://github.com/karanshukla/vinoWhisper", "vcs_info": {"vcs": "git"}},
    ],
    ids=["directory", "editable", "git"],
)
def test_an_unstamped_build_reports_dev(tmp_path, record):
    assert version.resolve(_dist(tmp_path, record)) == "dev"


def test_a_checkout_run_without_installing_reports_dev():
    assert version.resolve(None) == "dev"


def test_a_checkout_reports_dev_whatever_metadata_is_on_the_path(tmp_path):
    """An `.egg-info` in the tree has no direct_url.json."""
    assert version.resolve(_dist(tmp_path), checkout=True) == "dev"
    assert version.resolve(_dist(tmp_path), checkout=False) == "1.8.0"


def test_an_unreadable_direct_url_is_not_taken_for_a_source_install(tmp_path):
    """A release must never be classified dev."""
    assert version.resolve(_dist(tmp_path, "not json")) == "1.8.0"
    assert version.resolve(_dist(tmp_path, "[]")) == "1.8.0"


def test_a_flatpak_release_build_reports_its_stamp(tmp_path):
    info, stamp = tmp_path / "flatpak-info", tmp_path / "release-version"
    info.write_text("[Application]\n")
    stamp.write_text("1.8.0\n")
    assert version.flatpak_release(stamp, info) == "1.8.0"
    source = _dist(tmp_path, {"url": "file:///build", "dir_info": {}})
    assert version.resolve(source, "1.8.0") == "1.8.0"


def test_a_local_flatpak_build_reports_dev(tmp_path):
    info = tmp_path / "flatpak-info"
    info.write_text("[Application]\n")
    assert version.flatpak_release(tmp_path / "release-version", info) is None
    source = _dist(tmp_path, {"url": "file:///build", "dir_info": {}})
    assert version.resolve(source, None) == "dev"


def test_a_stamp_outside_a_sandbox_is_ignored(tmp_path):
    stamp = tmp_path / "release-version"
    stamp.write_text("1.8.0\n")
    assert version.flatpak_release(stamp, tmp_path / "absent") is None


def test_this_checkout_reports_dev():
    """CI runs the suite from the tree without installing it."""
    assert version.VERSION == "dev"
