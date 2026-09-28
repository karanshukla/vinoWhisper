"""ovfetch's `detect` JSON, read into doctor notes.

The fixture is real `ovfetch detect` output from the Wildcat Lake laptop
(2026-09-28). Nothing here runs ovfetch: `subprocess.run` and `shutil.which`
are replaced, the same boundary the capture tests use.
"""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from vinowhisper import devices, doctor, ovfetch, wizard

WILDCAT_LAKE = {
    "devices": [
        {"kind": "npu", "pci_id": "0xfd3e", "slot": "0000:00:0b.0"},
        {"kind": "gpu", "pci_id": "0xfd80", "slot": "0000:00:02.0"},
    ],
    "npu_driver": "1.35.0",
    "npu_compiler": True,
    "npu_compiler_needs": [
        ["libopenvino_intel_npu_compiler_loader.so", "libopenvino_intel_npu_compiler.so"],
        ["libnpu_driver_compiler.so"],
    ],
    "platforms": [
        {
            "pci_id": "0xfd3e",
            "kernel_name": "WCL",
            "codename": "Wildcat Lake",
            "min_openvino": "2025.4",
            "min_npu_driver": "1.32.0",
        }
    ],
    "openvino_ceiling": "2026.4",
    "npu_driver_required": "1.32.0",
    "warnings": [],
}


def status(**changes):
    result = copy.deepcopy(WILDCAT_LAKE)
    result.update(changes)
    return result


def labelled(notes, label):
    return next(note for note in notes if note.label == label)


def test_the_laptops_own_setup_is_inside_the_range():
    notes = ovfetch.notes(status(), "2026.4.0-22959-99c81491cc3-releases/2026/4")
    assert all(note.ok is True for note in notes)
    assert "Wildcat Lake" in labelled(notes, "npu: platform").detail
    assert "2025.4 to 2026.4" in labelled(notes, "npu: openvino range").detail


def test_characterization_newer_than_the_ceiling_is_a_warning_not_a_failure():
    """characterization: past the ceiling is untested, not known to fail.

    Measured 2026-09-28: drivers 1.32.0 and 1.35.0 each ran OpenVINO up to
    2026.4, well past Intel's pairing for either. The ceiling is ovfetch's
    pessimistic "newest known to work", so the doctor says so and moves on;
    turning it into a failure would fail setups that run.
    """
    note = labelled(ovfetch.notes(status(), "2026.5.0"), "npu: openvino range")
    assert note.ok is False
    assert "Untested rather than known to fail" in note.detail


def test_older_than_the_floor_is_a_warning():
    note = labelled(ovfetch.notes(status(), "2025.3.0"), "npu: openvino range")
    assert note.ok is False
    assert "older than 2025.4" in note.detail


def test_a_driver_with_no_recorded_range_is_unknown():
    note = labelled(ovfetch.notes(status(openvino_ceiling=None), "2026.4.0"), "npu: openvino range")
    assert note.ok is None


def test_ovfetch_warnings_are_passed_through():
    notes = ovfetch.notes(status(warnings=["NPU driver 1.30.0 is older than 1.32.0"]), "2026.4.0")
    assert labelled(notes, "npu: ovfetch").ok is False


def test_an_ovfetch_without_platform_data_asks_for_an_upgrade():
    old = {key: WILDCAT_LAKE[key] for key in ("devices", "npu_driver", "npu_compiler")}
    [note] = ovfetch.notes(old, "2026.4.0")
    assert note.ok is None
    assert ovfetch.INSTALL_HINT in note.detail


def test_no_npu_means_no_bounds():
    [note] = ovfetch.notes(status(devices=[], platforms=[]), "2026.4.0")
    assert note.ok is True


def test_no_openvino_skips_only_the_range():
    notes = ovfetch.notes(status(), None)
    assert all(note.label != "npu: openvino range" for note in notes)
    assert labelled(notes, "npu: platform").ok is True


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(ovfetch.shutil, "which", lambda name: "/usr/bin/ovfetch")

    def respond(returncode=0, stdout="", stderr=""):
        def run(argv, **kwargs):
            assert argv == ["/usr/bin/ovfetch", "detect"]
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

        monkeypatch.setattr(ovfetch.subprocess, "run", run)

    return respond


def test_detect_is_none_when_not_installed(monkeypatch):
    monkeypatch.setattr(ovfetch.shutil, "which", lambda name: None)
    assert ovfetch.detect() is None


def test_detect_reads_the_json(installed):
    installed(stdout=json.dumps(WILDCAT_LAKE))
    assert ovfetch.detect() == WILDCAT_LAKE


@pytest.mark.parametrize(
    "response",
    [
        {"returncode": 1, "stderr": "Error: parsing data/platforms.toml"},
        {"stdout": "not json"},
        {"stdout": "[]"},
    ],
)
def test_detect_failures_raise_with_the_reason(installed, response):
    installed(**response)
    with pytest.raises(ovfetch.OvfetchError):
        ovfetch.detect()


def test_detect_timeout_raises(monkeypatch):
    monkeypatch.setattr(ovfetch.shutil, "which", lambda name: "/usr/bin/ovfetch")

    def hang(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(ovfetch.subprocess, "run", hang)
    with pytest.raises(ovfetch.OvfetchError, match="timed out"):
        ovfetch.detect()


@pytest.fixture
def intel_npu(monkeypatch):
    npu = devices.Hardware(slot="0000:00:0b.0", kind="NPU", vendor="Intel", pci_id="0xfd3e")
    monkeypatch.setattr(devices, "hardware", lambda: [npu])
    monkeypatch.setattr(doctor, "_openvino_version", lambda: "2027.1.0")


def test_the_doctor_never_fails_on_ovfetch(monkeypatch, intel_npu):
    warned = status(warnings=["NPU driver 1.30.0 is older than 1.32.0"], openvino_ceiling="2026.4")
    monkeypatch.setattr(ovfetch, "detect", lambda: warned)
    results = doctor._ovfetch()
    assert results
    assert doctor.FAIL not in {result.status for result in results}
    assert doctor.WARN in {result.status for result in results}


def test_the_doctor_reports_ovfetch_missing_as_unknown(monkeypatch, intel_npu):
    monkeypatch.setattr(ovfetch, "detect", lambda: None)
    [result] = doctor._ovfetch()
    assert result.status == doctor.UNKNOWN
    assert ovfetch.INSTALL_HINT in result.detail


def test_the_doctor_skips_ovfetch_without_an_intel_npu(monkeypatch):
    monkeypatch.setattr(devices, "hardware", lambda: [])
    monkeypatch.setattr(ovfetch, "detect", lambda: pytest.fail("ran ovfetch"))
    assert doctor._ovfetch() == []


# --- installing it -------------------------------------------------------


def _pin(tmp_path, body=b"\x7fELF ovfetch", arch="x86_64"):
    import hashlib

    path = tmp_path / "ovfetch_release.json"
    record = {"version": "0.2.0", "assets": {arch: {"sha256": hashlib.sha256(body).hexdigest()}}}
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_the_pin_names_the_release_asset(tmp_path):
    release = ovfetch.pinned(_pin(tmp_path), machine="x86_64")
    assert isinstance(release, ovfetch.Release)
    assert release.url == (
        "https://github.com/karanshukla/ovfetch/releases/download/v0.2.0/ovfetch-x86_64-linux"
    )


def test_no_pin_means_no_download(tmp_path):
    assert isinstance(ovfetch.pinned(tmp_path / "missing.json"), str)


def test_other_architectures_have_no_binary(tmp_path):
    reason = ovfetch.pinned(_pin(tmp_path), machine="aarch64")
    assert isinstance(reason, str)
    assert "cargo install ovfetch" in reason


def test_the_shipped_pin_matches_ovfetchs_release_asset():
    """If the file ships, it names the asset ovfetch's release.yml uploads."""
    if not ovfetch.PIN_FILE.exists():
        pytest.skip("no ovfetch pin in this checkout")
    release = ovfetch.pinned(machine="x86_64")
    assert isinstance(release, ovfetch.Release)
    assert release.name == "ovfetch-x86_64-linux"
    assert len(release.sha256) == 64


@pytest.mark.parametrize(
    ("stdout", "expected"), [("ovfetch 0.2.0\n", (0, 2, 0)), ("ovfetch 0.1.1\n", (0, 1, 1))]
)
def test_the_installed_version_is_read_from_version(monkeypatch, stdout, expected):
    monkeypatch.setattr(ovfetch.shutil, "which", lambda name: "/usr/bin/ovfetch")
    monkeypatch.setattr(
        ovfetch.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout, ""),
    )
    assert ovfetch.installed() == (ovfetch.Path("/usr/bin/ovfetch"), expected)


def test_a_mismatched_download_installs_nothing(tmp_path):
    from tests.test_overlay import _serving

    release = ovfetch.pinned(_pin(tmp_path, body=b"pinned"), machine="x86_64")
    dest = tmp_path / "bin" / "ovfetch"
    with pytest.raises(ovfetch.overlay.OverlayError, match="nothing was installed"):
        ovfetch.fetch(release, dest, get=_serving(b"tampered"))
    assert list(dest.parent.iterdir()) == []


def test_a_verified_download_is_installed(tmp_path):
    from tests.test_overlay import _serving

    body = b"\x7fELF ovfetch"
    release = ovfetch.pinned(_pin(tmp_path, body=body), machine="x86_64")
    dest = ovfetch.fetch(release, tmp_path / "bin" / "ovfetch", get=_serving(body))
    assert dest.read_bytes() == body


@pytest.fixture
def release(monkeypatch):
    pinned = ovfetch.Release(version="0.2.0", arch="x86_64", sha256="0" * 64)
    monkeypatch.setattr(ovfetch, "pinned", lambda: pinned)
    return pinned


def test_a_current_ovfetch_is_left_alone(monkeypatch, release):
    monkeypatch.setattr(ovfetch, "installed", lambda: (ovfetch.Path("/usr/bin/ovfetch"), (0, 2, 0)))
    monkeypatch.setattr(ovfetch, "fetch", lambda *a, **k: pytest.fail("downloaded again"))
    assert wizard.Wizard(assume_yes=True).install_ovfetch().ok is True


def test_an_older_ovfetch_is_offered_the_pinned_one(monkeypatch, release, capsys):
    monkeypatch.setattr(ovfetch, "installed", lambda: (ovfetch.Path("/usr/bin/ovfetch"), (0, 1, 1)))
    outcome = wizard.Wizard(dry_run=True).install_ovfetch()
    assert outcome.ok is None
    assert "predates 0.2.0" in capsys.readouterr().out


def test_setup_never_downloads_ovfetch_without_a_pin(monkeypatch):
    monkeypatch.setattr(ovfetch, "pinned", lambda: "no pin")
    monkeypatch.setattr(ovfetch, "fetch", lambda *a, **k: pytest.fail("downloaded without a pin"))
    assert wizard.Wizard(assume_yes=True).install_ovfetch().ok is None


def test_setup_ovfetch_runs_that_step_and_nothing_else(monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(wizard.Wizard, "run_all", lambda self: pytest.fail("ran the full setup"))
    monkeypatch.setattr(
        wizard.Wizard,
        "install_ovfetch",
        lambda self: ran.append("ovfetch") or wizard.Outcome(True, "fine"),
    )
    assert wizard.main(["--ovfetch", "--dry-run"]) == 0
    assert ran == ["ovfetch"]


def test_an_older_copy_earlier_on_path_is_named_with_its_fix(monkeypatch, release, tmp_path):
    """What this laptop had on 2026-09-28: `cargo install`'s 0.1.1 ahead of ~/.local/bin."""
    cargo_copy = str(Path.home() / ".cargo/bin/ovfetch")
    monkeypatch.setattr(ovfetch, "installed", lambda: (Path(cargo_copy), (0, 1, 1)))
    monkeypatch.setattr(ovfetch, "fetch", lambda release, dest, get=None: dest)
    monkeypatch.setattr(wizard.shutil, "which", lambda name: cargo_copy)

    outcome = wizard.Wizard(assume_yes=True).install_ovfetch()
    assert outcome.ok is None
    assert "cargo uninstall ovfetch" in outcome.summary
