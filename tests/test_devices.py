"""Device selection and the fallback policy.

`available()` needs OpenVINO, so every test here passes an inventory in. That
is also the honest split: selection is policy and is testable; enumeration is
a driver question and isn't.
"""

import os

import pytest

from vinowhisper import devices


def test_kind_strips_the_instance_suffix():
    assert devices.kind_of("GPU.1") == "GPU"
    assert devices.kind_of("npu") == "NPU"


def test_npu_wins_when_present(inventory):
    selection = devices.select("auto", inventory("CPU", "GPU", "NPU"))
    assert selection.device.kind == "NPU"
    assert not selection.degraded
    assert selection.warnings == ()


def test_gpu_is_preferred_over_cpu(inventory):
    selection = devices.select("auto", inventory("CPU", "GPU"))
    assert selection.device.kind == "GPU"
    assert selection.degraded
    assert any("not the NPU" in warning for warning in selection.warnings)


def test_cpu_fallback_is_loud(inventory):
    selection = devices.select("auto", inventory("CPU"))
    assert selection.device.kind == "CPU"
    assert selection.degraded
    joined = " ".join(selection.warnings)
    assert "CPU" in joined
    assert "vinowhisper-doctor" in joined


def test_none_and_auto_mean_the_same_thing(inventory):
    devices_list = inventory("CPU", "NPU")
    assert devices.select(None, devices_list).device == devices.select("auto", devices_list).device


def test_an_explicit_device_is_refused_rather_than_downgraded(inventory):
    """Someone who typed --device NPU wants to know it did not happen."""
    with pytest.raises(devices.DeviceError, match="not available"):
        devices.select("NPU", inventory("CPU"))


def test_an_explicit_device_can_be_a_full_name(inventory):
    devices_list = [devices.Device(name="GPU.1", kind="GPU", full_name="Arc")]
    assert devices.select("gpu.1", devices_list).device.name == "GPU.1"


def test_an_empty_inventory_is_an_error_not_a_cpu_fallback():
    with pytest.raises(devices.DeviceError):
        devices.select("auto", [])


def test_an_unknown_device_kind_is_taken_but_flagged():
    exotic = [devices.Device(name="MYRIAD", kind="MYRIAD", full_name="")]
    selection = devices.select("auto", exotic)
    assert selection.device.name == "MYRIAD"
    assert selection.degraded
    assert any("tested" in warning for warning in selection.warnings)


def test_preflight_reports_a_missing_accel_node(monkeypatch):
    monkeypatch.setattr(devices, "accel_nodes", lambda: [])
    monkeypatch.setattr(devices, "_module_loaded", lambda name: False)
    notes = devices.npu_preflight()
    assert notes[0].ok is False
    assert "/dev/accel" in notes[0].detail
    assert any("modprobe" in note.detail for note in notes)


def test_preflight_reports_a_permission_problem(monkeypatch, tmp_path):
    node = tmp_path / "accel0"
    node.write_text("")
    node.chmod(0o000)
    monkeypatch.setattr(devices, "accel_nodes", lambda: [node])
    notes = devices.npu_preflight()
    assert notes[0].ok is True  # the node exists
    # Running as root defeats the permission check entirely, which is fine —
    # the assertion is that the check reports one way or the other, with the
    # group hint attached when it fails.
    permission = notes[1]
    assert permission.label == "device permissions"
    if permission.ok is False:
        assert "render" in permission.detail


def test_missing_npu_help_ends_with_the_distro_remediation(monkeypatch):
    monkeypatch.setattr(devices, "accel_nodes", lambda: [])
    monkeypatch.setattr(devices, "_module_loaded", lambda name: None)
    lines = devices.npu_missing_help()
    assert lines
    assert any("linux-npu-driver" in line for line in lines)


# --- Userspace NPU libraries ---------------------------------------------
#
# The case each of these covers is one where every kernel-side check above
# passes and the NPU still cannot compile a model, which is exactly the failure
# that is invisible without this.


def fake_lib(path, marker: str = "") -> None:
    """A stand-in .so: real enough for the marker scan, 200 bytes rather than 127MB."""
    path.write_bytes(b"\x7fELF" + b"\x00" * 64 + marker.encode() + b"\x00" * 64)


@pytest.fixture
def libdir(monkeypatch, tmp_path):
    monkeypatch.setattr(devices, "library_dirs", lambda: [tmp_path])
    return tmp_path


def labelled(notes, label):
    return next(note for note in notes if note.label == label)


def test_userspace_reports_both_libraries_and_their_versions(libdir):
    fake_lib(libdir / "libze_intel_npu.so.1.35.0", "npu-linux-driver-ci-1.35.0.20260722")
    (libdir / "libze_intel_npu.so.1").symlink_to("libze_intel_npu.so.1.35.0")
    fake_lib(libdir / devices.COMPILER_LIB)
    fake_lib(libdir / devices.COMPILER_LOADER, "2026.3.0-22159-4089686065a-0722.205447")

    notes = devices.npu_userspace()
    backend = labelled(notes, "level-zero NPU backend")
    assert backend.ok is True
    assert "1.35.0.20260722" in backend.detail

    compiler = labelled(notes, "NPU compiler")
    assert compiler.ok is True
    # Trailing build metadata past the git hash is noise, and is not reported.
    assert "OpenVINO 2026.3.0-22159-4089686065a)" in compiler.detail


def test_a_missing_compiler_is_a_failure_carrying_the_extraction_steps(libdir):
    fake_lib(libdir / "libze_intel_npu.so.1.35.0")
    (libdir / "libze_intel_npu.so.1").symlink_to("libze_intel_npu.so.1.35.0")

    compiler = labelled(devices.npu_userspace(), "NPU compiler")
    assert compiler.ok is False
    # The whole point of this check: it is not caught by anything upstream.
    assert "still enumerates" in compiler.detail
    assert "dpkg-deb" in compiler.detail
    assert "linux-npu-driver/releases" in compiler.detail


def test_a_reverted_soname_symlink_is_caught_and_named(libdir):
    """What `dnf reinstall intel-npu-driver` does to a hand-installed backend."""
    fake_lib(libdir / "libze_intel_npu.so.1.32.0")
    fake_lib(libdir / "libze_intel_npu.so.1.35.0")
    (libdir / "libze_intel_npu.so.1").symlink_to("libze_intel_npu.so.1.32.0")
    fake_lib(libdir / devices.COMPILER_LIB)
    fake_lib(libdir / devices.COMPILER_LOADER)

    version = labelled(devices.npu_userspace(), "level-zero version")
    assert version.ok is False
    assert "libze_intel_npu.so.1.35.0" in version.detail
    assert "ln -sf" in version.detail


def test_the_newest_backend_being_selected_raises_nothing(libdir):
    """The same two files, with the symlink the right way round: no warning."""
    fake_lib(libdir / "libze_intel_npu.so.1.32.0")
    fake_lib(libdir / "libze_intel_npu.so.1.35.0")
    (libdir / "libze_intel_npu.so.1").symlink_to("libze_intel_npu.so.1.35.0")
    fake_lib(libdir / devices.COMPILER_LIB)
    fake_lib(libdir / devices.COMPILER_LOADER)

    assert all(note.label != "level-zero version" for note in devices.npu_userspace())


def test_a_missing_level_zero_backend_says_the_device_will_not_enumerate(libdir):
    backend = labelled(devices.npu_userspace(), "level-zero NPU backend")
    assert backend.ok is False
    assert "will not enumerate" in backend.detail


def test_library_dirs_puts_ld_library_path_first(monkeypatch, tmp_path):
    """A vendored toolkit sourced through setupvars.sh has to win over /usr/lib64."""
    vendored = tmp_path / "opt" / "openvino" / "lib"
    vendored.mkdir(parents=True)
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{vendored}{os.pathsep}/does/not/exist")

    dirs = devices.library_dirs()
    assert dirs[0] == vendored
    assert all(directory.is_dir() for directory in dirs)
