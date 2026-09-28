"""ovfetch's `detect` JSON, read into doctor notes.

The fixture is real `ovfetch detect` output from the Wildcat Lake laptop
(2026-09-28). Nothing here runs ovfetch: `subprocess.run` and `shutil.which`
are replaced, the same boundary the capture tests use.
"""

import copy
import json
import subprocess

import pytest

from vinowhisper import devices, doctor, ovfetch

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
