"""What is on the PCI bus, as opposed to what OpenVINO enumerates.

The two disagree in exactly the cases worth diagnosing: an Intel GPU with no
compute runtime (OpenVINO sees CPU and NPU only, which is how this laptop sat
until 2026-09-12), a machine with no NPU at all being told to install an NPU
driver, and an AMD NPU that OpenVINO cannot drive whatever is installed.
"""

import pytest

from vinowhisper import devices, distro, doctor, wizard


@pytest.fixture
def pci(monkeypatch, tmp_path):
    root = tmp_path / "devices"
    root.mkdir()
    monkeypatch.setattr(devices, "PCI_DIR", root)

    def add(slot: str, vendor: str, device: str, pci_class: str, driver: str | None = None):
        path = root / slot
        path.mkdir()
        (path / "vendor").write_text(vendor + "\n")
        (path / "device").write_text(device + "\n")
        (path / "class").write_text(pci_class + "\n")
        if driver:
            target = tmp_path / "drivers" / driver
            target.mkdir(parents=True, exist_ok=True)
            (path / "driver").symlink_to(target)

    return add


@pytest.fixture
def icds(monkeypatch, tmp_path):
    directory = tmp_path / "vendors"
    directory.mkdir()
    monkeypatch.setattr(devices, "OPENCL_VENDORS_DIR", directory)

    def add(name: str) -> None:
        (directory / name).write_text("/usr/lib64/libsomething.so\n")

    return add


@pytest.fixture
def fedora(fake_os_release):
    return distro.detect(fake_os_release(ID="fedora", NAME="Fedora Linux", VERSION_ID="44"))


def wildcat_lake(pci) -> None:
    """This laptop's bus as read on 2026-09-12, including two 0x1180 decoys."""
    pci("0000:00:02.0", "0x8086", "0xfd80", "0x030000", "xe")
    pci("0000:00:04.0", "0x8086", "0xfd1d", "0x118000", "proc_thermal_pci")
    pci("0000:00:0a.0", "0x8086", "0xfd7d", "0x118000", "intel_vsec")
    pci("0000:00:0b.0", "0x8086", "0xfd3e", "0x120000", "intel_vpu")


def test_the_wildcat_lake_bus_is_one_gpu_and_one_npu(pci):
    wildcat_lake(pci)
    found = devices.hardware()
    assert [(hw.kind, hw.vendor, hw.pci_id, hw.driver) for hw in found] == [
        ("GPU", "Intel", "8086:fd80", "xe"),
        ("NPU", "Intel", "8086:fd3e", "intel_vpu"),
    ]


def test_an_amd_npu_is_found_by_id_since_its_class_is_ambiguous(pci):
    pci("0000:c5:00.1", "0x1022", "0x17f0", "0x118000", "amdxdna")
    pci("0000:c5:00.2", "0x1022", "0x15e2", "0x118000")
    (npu,) = devices.hardware()
    assert (npu.kind, npu.vendor, npu.driver) == ("NPU", "AMD", "amdxdna")


def test_a_bound_npu_driver_decides_even_for_an_id_not_in_the_table(pci):
    pci("0000:c6:00.1", "0x1022", "0x1bff", "0x118000", "amdxdna")
    (npu,) = devices.hardware()
    assert npu.kind == "NPU"


def test_an_unbound_device_says_so(pci):
    pci("0000:01:00.0", "0x10de", "0x2684", "0x030000")
    assert "no driver bound" in str(devices.hardware()[0])


def test_an_unreadable_bus_is_empty_rather_than_an_error():
    assert devices.hardware() == []


def test_an_intel_gpu_openvino_cannot_see_names_the_runtime_package(pci, fedora, inventory):
    wildcat_lake(pci)
    (note,) = devices.gpu_notes(inventory("CPU", "NPU"), devices.hardware(), fedora)
    assert note.ok is False
    assert "no OpenCL ICD" in note.detail
    assert "intel-compute-runtime" in note.detail


def test_someone_elses_icd_is_not_mistaken_for_intels(pci, icds, fedora, inventory):
    wildcat_lake(pci)
    icds("rusticl.icd")
    (note,) = devices.gpu_notes(inventory("CPU"), devices.hardware(), fedora)
    assert "rusticl.icd" in note.detail


def test_an_enumerated_intel_gpu_is_fine(pci, inventory):
    wildcat_lake(pci)
    (note,) = devices.gpu_notes(inventory("CPU", "GPU", "NPU"), devices.hardware())
    assert note.ok is True


def test_a_non_intel_gpu_is_reported_as_not_a_fallback(pci, inventory):
    pci("0000:01:00.0", "0x1002", "0x744c", "0x030000", "amdgpu")
    (note,) = devices.gpu_notes(inventory("CPU"), devices.hardware())
    assert note.ok is None
    assert "only drives Intel" in note.detail


def use_device(monkeypatch, *kinds: str) -> None:
    inventory = [devices.Device(name=kind, kind=kind, full_name="fake") for kind in kinds]
    monkeypatch.setattr(devices, "available", lambda: inventory)


def labelled(results, label):
    return next(result for result in results if result.label == label)


def test_the_doctor_does_not_prescribe_an_npu_driver_without_an_npu(monkeypatch, pci):
    pci("0000:00:02.0", "0x8086", "0x46a6", "0x030000", "i915")
    use_device(monkeypatch, "CPU")
    results = doctor._devices()
    assert labelled(results, "npu").status == doctor.UNKNOWN
    assert not any(result.label.startswith("npu: ") for result in results)
    assert labelled(results, "gpu").status == doctor.WARN


def test_the_doctor_names_an_amd_npu_it_cannot_use(monkeypatch, pci):
    pci("0000:c5:00.1", "0x1022", "0x17f0", "0x118000", "amdxdna")
    use_device(monkeypatch, "CPU")
    result = labelled(doctor._devices(), "npu")
    assert result.status == doctor.WARN
    assert "cannot drive" in result.detail


def test_the_doctor_still_walks_the_npu_checks_when_the_bus_is_unreadable(monkeypatch):
    use_device(monkeypatch, "CPU")
    assert labelled(doctor._devices(), "npu").status == doctor.FAIL


def test_fedora_install_lines_name_only_packages_that_exist(fedora):
    """Checked against Fedora 44's repos on 2026-09-12.

    There is no `level-zero` package or Provides on Fedora (the loader is
    `oneapi-level-zero`), so a line naming it fails outright with "No match for
    argument". `intel-compute-runtime` is a metapackage that already pulls in
    intel-opencl and intel-level-zero, and `intel-npu-driver` requires the loader.
    """
    assert distro.install_command(distro.GPU_RUNTIME, fedora) == (
        "sudo dnf install -y intel-compute-runtime"
    )
    assert (
        distro.install_command(distro.NPU_DRIVER, fedora) == "sudo dnf install -y intel-npu-driver"
    )


def test_the_wizard_offers_the_gpu_runtime_not_the_npu_driver(monkeypatch, pci, fedora, capsys):
    pci("0000:00:02.0", "0x8086", "0x46a6", "0x030000", "i915")
    use_device(monkeypatch, "CPU")
    setup = wizard.Wizard(dry_run=True)
    setup.distro = fedora
    outcome = setup.check_device()
    out = capsys.readouterr().out
    assert outcome.ok is None
    assert "$ sudo dnf install -y intel-compute-runtime" in out
    assert "intel-npu-driver" not in out
