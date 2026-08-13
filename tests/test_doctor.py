"""The parts of the doctor that are logic rather than measurement.

The level probe and the OpenVINO checks need hardware and are exercised by
running the thing; what is checkable here is the model-export matrix (which
export is required for which device, and what a mismatch is called) and the
verdict, which is the one place the doctor draws a conclusion rather than
reporting a fact.
"""

import pytest

from vinowhisper import config, devices, doctor


@pytest.fixture
def model_dirs(monkeypatch, tmp_path):
    """Point both export paths at a tmp dir and let a test create them."""
    npu = tmp_path / "npu"
    stateful = tmp_path / "stateful"
    monkeypatch.setattr(config, "MODEL_DIR", npu)
    monkeypatch.setattr(config, "STATEFUL_MODEL_DIR", stateful)

    def make(kind: str, with_past: bool) -> None:
        directory = npu if kind == "npu" else stateful
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "openvino_encoder_model.xml").write_text("")
        if with_past:
            (directory / "openvino_decoder_with_past_model.xml").write_text("")

    return make


def use_device(monkeypatch, kind: str) -> None:
    device = devices.Device(name=kind, kind=kind, full_name="fake")
    monkeypatch.setattr(devices, "available", lambda: [device])


def find(results, label):
    return next(result for result in results if result.label == label)


def test_npu_export_present_and_correct(monkeypatch, model_dirs):
    use_device(monkeypatch, "NPU")
    model_dirs("npu", with_past=True)
    results = doctor._models()
    assert find(results, "model (npu)").status == doctor.OK
    # The CPU export is simply absent, which is not a problem on an NPU box.
    assert find(results, "model (stateful)").status == doctor.UNKNOWN


def test_npu_export_without_disable_stateful_is_a_failure(monkeypatch, model_dirs):
    use_device(monkeypatch, "NPU")
    model_dirs("npu", with_past=False)
    result = find(doctor._models(), "model (npu)")
    assert result.status == doctor.FAIL
    assert "--variant npu" in result.detail


def test_a_cpu_box_needs_the_stateful_export(monkeypatch, model_dirs):
    use_device(monkeypatch, "CPU")
    results = doctor._models()
    assert find(results, "model (stateful)").status == doctor.FAIL
    assert find(results, "model (npu)").status == doctor.UNKNOWN


def test_the_npu_export_sitting_in_the_cpu_slot_is_named_as_such(monkeypatch, model_dirs):
    """It would otherwise die inside the pipeline on a beam_idx port error."""
    use_device(monkeypatch, "CPU")
    model_dirs("stateful", with_past=True)
    result = find(doctor._models(), "model (stateful)")
    assert result.status == doctor.FAIL
    assert "wrong export" in result.detail


def test_devices_check_reports_the_fallback(monkeypatch):
    use_device(monkeypatch, "CPU")
    results = doctor._devices()
    assert find(results, "npu").status == doctor.FAIL
    assert find(results, "selected device").status == doctor.WARN
    assert "NOT the NPU" in find(results, "selected device").detail


def test_devices_check_survives_openvino_being_absent(monkeypatch):
    def boom():
        raise devices.DeviceError("openvino is not importable")

    monkeypatch.setattr(devices, "available", boom)
    results = doctor._devices()
    assert results[0].status == doctor.FAIL


def test_verdict_confirms_the_mute_problem_by_measurement():
    results = [
        doctor.Result(doctor.WARN, "level: default sink", "rms 0.00001, below the silence gate"),
        doctor.Result(doctor.OK, "level: Chromium (1234)", "rms 0.02"),
    ]
    assert "mute/volume problem, confirmed" in doctor._verdict(results)


def test_verdict_points_upstream_when_nothing_is_audible():
    results = [
        doctor.Result(doctor.WARN, "level: default sink", "below the silence gate"),
        doctor.Result(doctor.WARN, "level: Chromium (1234)", "below the silence gate"),
    ]
    assert "upstream of vinoWhisper" in doctor._verdict(results)


def test_verdict_clears_mute_when_the_monitor_still_carries_signal():
    """This machine measured 0.98x of the app's level while muted (2026-08-07)."""
    results = [
        doctor.Result(doctor.WARN, "muted", "YES (the level probe below decides)"),
        doctor.Result(doctor.OK, "level: default sink", "rms 0.08"),
        doctor.Result(doctor.OK, "level: Chromium (1234)", "rms 0.08"),
    ]
    verdict = doctor._verdict(results)
    assert "pre-mute" in verdict
    assert "muted the app rather" in verdict


def test_verdict_is_none_without_an_application_to_compare_against():
    """Without an audible control stream, a muted run measures nothing at all."""
    results = [doctor.Result(doctor.OK, "level: default sink", "rms 0.02")]
    assert doctor._verdict(results) is None


def test_python_314_is_a_failure(monkeypatch):
    import sys

    from conftest import fake_version

    monkeypatch.setattr(sys, "version_info", fake_version(3, 14, 0))
    assert doctor._python().status == doctor.FAIL


def test_unknown_distro_is_a_warning_not_a_failure(monkeypatch, tmp_path):
    from vinowhisper import distro

    monkeypatch.setattr(distro, "OS_RELEASE", tmp_path / "absent")
    monkeypatch.setattr(doctor.distro, "detect", lambda path=None: distro.Distro("x", "X", ""))
    assert doctor._distro().status == doctor.WARN
