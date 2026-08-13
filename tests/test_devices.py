"""Device selection and the fallback policy.

`available()` needs OpenVINO, so every test here passes an inventory in. That
is also the honest split: selection is policy and is testable; enumeration is
a driver question and isn't.
"""

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
