"""The parts of the doctor that are logic rather than measurement.

The level probe and the OpenVINO checks need hardware and are exercised by
running the thing; what is checkable here is the model-export matrix (which
export is required for which device, and what a mismatch is called) and the
verdict, which is the one place the doctor draws a conclusion rather than
reporting a fact.
"""

import json

import pytest

from vinowhisper import config, devices, distro, doctor, failures, integrity


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


def test_an_npu_box_without_the_cpu_export_is_not_a_problem(monkeypatch, model_dirs):
    use_device(monkeypatch, "NPU")
    model_dirs("npu", with_past=True)
    assert find(doctor._models(), "model (stateful)").status == doctor.UNKNOWN


@pytest.fixture
def multilingual_dirs(monkeypatch, tmp_path):
    npu = tmp_path / "multilingual-npu"
    monkeypatch.setattr(config, "MULTILINGUAL_MODEL_DIR", npu)
    monkeypatch.setattr(config, "MULTILINGUAL_STATEFUL_MODEL_DIR", tmp_path / "multilingual-cpu")
    return npu


def test_the_multilingual_export_is_checked_only_once_it_exists(
    monkeypatch, model_dirs, multilingual_dirs
):
    use_device(monkeypatch, "NPU")
    labels = [result.label for result in doctor._models()]
    assert not any("multilingual" in label for label in labels)


def test_an_existing_multilingual_export_is_checked(monkeypatch, model_dirs, multilingual_dirs):
    use_device(monkeypatch, "NPU")
    multilingual_dirs.mkdir()
    labels = [result.label for result in doctor._models()]
    assert "model (npu, multilingual)" in labels
    assert "model (stateful, multilingual)" in labels


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


def test_a_failed_device_mark_is_reported_and_cleared(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "FAILED_DEVICES_FILE", tmp_path / "failed-devices.json")
    failures.record("NPU", "Missing upper bound for one or more nodes")

    results = doctor._failed_devices()
    assert [result.status for result in results] == [doctor.WARN]
    assert "Missing upper bound" in results[0].detail
    assert "cleared" in results[0].detail
    assert failures.load() == {}
    assert doctor._failed_devices()[0].status == doctor.OK


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


@pytest.mark.parametrize(
    "status, required, expected",
    [
        (integrity.VERIFIED, True, doctor.OK),
        (integrity.UNPINNED, True, doctor.UNKNOWN),
        (integrity.DRIFT, True, doctor.WARN),
        (integrity.MISMATCH, True, doctor.FAIL),
        (integrity.KNOWN_BAD, True, doctor.FAIL),
    ],
)
def test_the_doctor_grades_a_digest_result_by_whether_the_export_is_needed(
    monkeypatch, tmp_path, status, required, expected
):
    _verify_as(monkeypatch, status)
    assert doctor._digests("npu", tmp_path, required).status == expected


@pytest.mark.parametrize("status", [integrity.MISMATCH, integrity.KNOWN_BAD])
def test_a_bad_digest_on_an_export_this_machine_never_loads_is_only_a_warning(
    monkeypatch, tmp_path, status
):
    _verify_as(monkeypatch, status)
    assert doctor._digests("npu", tmp_path, False).status == doctor.WARN


def _verify_as(monkeypatch, status):
    monkeypatch.setattr(
        integrity,
        "verify",
        lambda directory, variant, *args, **kwargs: integrity.Verification(
            status=status, model_id=config.MODEL_ID, variant=variant, directory=directory
        ),
    )


@pytest.mark.parametrize(
    ("ok", "expected"), [(True, doctor.OK), (False, doctor.WARN), (None, doctor.UNKNOWN)]
)
def test_an_ovfetch_note_is_never_a_failure(monkeypatch, ok, expected):
    intel_npu = devices.Hardware(slot="0000:00:0b.0", kind="NPU", vendor="Intel", pci_id="0x643e")
    monkeypatch.setattr(devices, "hardware", lambda: [intel_npu])
    monkeypatch.setattr(doctor.ovfetch, "detect", lambda: {})
    monkeypatch.setattr(
        doctor.ovfetch, "notes", lambda status, version: [devices.Note(ok, "l", "")]
    )
    monkeypatch.setattr(doctor, "_openvino_version", lambda: None)
    assert [result.status for result in doctor._ovfetch()] == [expected]


def test_digests_are_only_checked_on_an_export_of_the_right_shape(model_dirs, monkeypatch):
    """A wrong-variant export fails first, so hashing it says nothing useful."""
    model_dirs("npu", with_past=False)
    monkeypatch.setattr(devices, "available", lambda: [])
    labels = [result.label for result in doctor._models()]
    assert "model (npu)" in labels
    assert "model (npu) digests" not in labels


def test_replacements_check_reports_absent():
    result = doctor._replacements()[0]
    assert (result.status, result.detail) == (doctor.OK, "absent")


def test_replacements_check_counts_entries():
    config.REPLACEMENTS_FILE.write_text(json.dumps({"a": "b", "c": "d"}))
    result = doctor._replacements()[0]
    assert (result.status, result.detail) == (doctor.OK, "2 entries")


def test_replacements_check_names_the_error():
    config.REPLACEMENTS_FILE.write_text('{"a": 1}')
    result = doctor._replacements()[0]
    assert result.status == doctor.WARN
    assert "'a'" in result.detail
    assert str(config.REPLACEMENTS_FILE) in result.detail


def test_vocabulary_check_reports_absent():
    result = doctor._vocabulary()[0]
    assert (result.status, result.detail) == (doctor.OK, "absent")


def test_vocabulary_check_says_where_it_is_used(monkeypatch):
    config.VOCABULARY_FILE.write_text("Kubernetes\nvinoWhisper\n")
    monkeypatch.setattr(devices, "available", lambda: [devices.Device("CPU", "CPU")])
    result = doctor._vocabulary()[0]
    assert (result.status, result.detail) == (
        doctor.OK,
        "2 terms, given to dictation on CPU and used to correct",
    )


def test_vocabulary_on_the_npu_says_it_only_corrects_spelling(monkeypatch):
    """The NPU pipeline refuses a decoder prompt, so the list is not given to Whisper
    there. Saying "given to dictation" would claim a decode that never happens."""
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    monkeypatch.setattr(devices, "available", lambda: [devices.Device("NPU", "NPU")])
    result = doctor._vocabulary()[0]
    assert result.status == doctor.OK
    assert "spelling correction only" in result.detail
    assert "NPU" in result.detail


def test_vocabulary_on_the_npu_with_our_export_is_given_to_dictation(monkeypatch):
    """#178: our own decode loop takes the prompt where WhisperPipeline refuses it."""
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    config.STEP_MODEL_DIR.mkdir(parents=True)
    for name in ("encoder.xml", "decoder.xml", "step_export.json", "tokenizer.json"):
        (config.STEP_MODEL_DIR / name).write_text("{}")
    monkeypatch.setattr(devices, "available", lambda: [devices.Device("NPU", "NPU")])
    result = doctor._vocabulary()[0]
    assert result.detail == "1 terms, given to dictation on NPU and used to correct"


def test_vocabulary_on_the_npu_with_auto_language_is_correction_only(monkeypatch):
    """The transcriber refuses the prompt there (the loop has no language token for auto),
    so the doctor must not say it is given to dictation. Both read vocabulary.unprompted."""
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    config.save_language(config.AUTO_LANGUAGE, "transcribe", config.LANGUAGE_FILE)
    config.MULTILINGUAL_STEP_MODEL_DIR.mkdir(parents=True)
    for name in ("encoder.xml", "decoder.xml", "step_export.json", "tokenizer.json"):
        (config.MULTILINGUAL_STEP_MODEL_DIR / name).write_text("{}")
    monkeypatch.setattr(devices, "available", lambda: [devices.Device("NPU", "NPU")])
    result = doctor._vocabulary()[0]
    assert "spelling correction only" in result.detail and "auto" in result.detail
    config.save_language("fr", "transcribe", config.LANGUAGE_FILE)
    result = doctor._vocabulary()[0]
    assert result.detail == "1 terms, given to dictation on NPU and used to correct"


def test_a_missing_file_decoder_is_a_warning_with_the_package(monkeypatch):
    """Captions and dictation never use it, so it must not fail the doctor."""
    fedora = distro.Distro(id="fedora", name="Fedora", version="44", family="fedora")
    monkeypatch.setattr(distro, "detect", lambda *args, **kwargs: fedora)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    result = doctor._media_decoder()
    assert result.status == doctor.WARN
    assert "sudo dnf install -y ffmpeg-free" in result.detail

    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: name if name == "gst-launch-1.0" else None
    )
    result = doctor._media_decoder()
    assert result.status == doctor.OK and "gst-launch-1.0" in result.detail


def _hw(kind: str, vendor: str) -> devices.Hardware:
    return devices.Hardware(slot="0000:00:0b.0", kind=kind, vendor=vendor, pci_id="0000:0000")


def test_an_npu_openvino_enumerates_is_ok_whatever_the_bus_says():
    assert doctor.npu_presence(["NPU (Intel(R) AI Boost)"], []).status == doctor.OK


def test_an_intel_npu_on_the_bus_that_openvino_does_not_see_is_a_failure():
    assert doctor.npu_presence([], [_hw("NPU", "Intel")]).status == doctor.FAIL


def test_an_unreadable_bus_with_no_npu_enumerated_is_a_failure_not_a_guess():
    assert doctor.npu_presence([], []).status == doctor.FAIL


def test_a_non_intel_npu_is_a_warning_that_openvino_cannot_drive_it():
    result = doctor.npu_presence([], [_hw("GPU", "Intel"), _hw("NPU", "AMD")])
    assert result.status == doctor.WARN
    assert "cannot drive" in result.detail


def test_a_bus_with_no_npu_at_all_has_no_driver_to_install():
    result = doctor.npu_presence([], [_hw("GPU", "Intel")])
    assert result.status == doctor.UNKNOWN
    assert "no driver to install" in result.detail


def test_a_note_that_fails_takes_the_status_its_check_gives_it():
    notes = [
        devices.Note(True, "a", "fine"),
        devices.Note(None, "b", "unknown"),
        devices.Note(False, "c", "bad"),
    ]
    results = doctor._note_results(notes, doctor.WARN, "npu: ")
    assert [r.status for r in results] == [doctor.OK, doctor.UNKNOWN, doctor.WARN]
    assert [r.label for r in results] == ["npu: a", "npu: b", "npu: c"]


def test_health_names_the_device_and_passes_on_each_warning():
    results = doctor.health_results(
        {"device": "CPU", "version": doctor.VERSION, "warnings": ["not the NPU"]}, "sock"
    )
    assert [(r.status, r.label) for r in results] == [
        (doctor.OK, "server"),
        (doctor.WARN, "server device"),
    ]
    assert results[0].detail == "sock on CPU"


def test_a_server_of_another_version_is_told_to_stop():
    results = doctor.health_results({"device": "NPU", "version": "0.0.1"}, "sock")
    assert find(results, "server version").status == doctor.WARN
    assert "--stop" in find(results, "server version").detail
