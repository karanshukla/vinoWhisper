import mmap
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import distro

PREFERENCE = ("NPU", "GPU", "CPU")

ACCEL_GLOB = "accel*"
ACCEL_DIR = Path("/dev/accel")
DRI_DIR = Path("/dev/dri")


class DeviceError(RuntimeError):
    """OpenVINO is unusable, or the requested device does not exist."""


@dataclass(frozen=True)
class Device:
    name: str
    kind: str
    full_name: str = ""

    def __str__(self) -> str:
        return f"{self.name} ({self.full_name})" if self.full_name else self.name


@dataclass(frozen=True)
class Selection:
    device: Device
    requested: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        return self.device.kind

    @property
    def degraded(self) -> bool:
        return self.device.kind != "NPU"


@dataclass(frozen=True)
class Note:
    ok: bool | None
    label: str
    detail: str


def kind_of(name: str) -> str:
    return name.split(".")[0].upper()


def available() -> list[Device]:
    try:
        import openvino
    except ImportError as exc:  # pragma: no cover - exercised by the doctor, not tests
        raise DeviceError(f"openvino is not importable ({exc}); run `uv sync`") from exc

    try:
        core = openvino.Core()
        names = list(core.available_devices)
    except Exception as exc:  # noqa: BLE001 - driver faults arrive as arbitrary exceptions
        raise DeviceError(f"OpenVINO could not enumerate devices: {exc}") from exc

    devices = []
    for name in names:
        try:
            full_name = str(core.get_property(name, "FULL_DEVICE_NAME"))
        except Exception:  # noqa: BLE001 - a device that won't describe itself is still usable
            full_name = ""
        devices.append(Device(name=name, kind=kind_of(name), full_name=full_name))
    return devices


def select(preferred: str | None = None, inventory: list[Device] | None = None) -> Selection:
    devices = inventory if inventory is not None else available()
    if not devices:
        raise DeviceError("OpenVINO enumerated no devices at all")

    if preferred and preferred.lower() != "auto":
        wanted = preferred.upper()
        for device in devices:
            if device.name.upper() == wanted or device.kind == wanted:
                return Selection(device=device, requested=preferred, warnings=_warnings(device))
        names = ", ".join(device.name for device in devices)
        raise DeviceError(f"requested device {preferred!r} is not available (have: {names})")

    for kind in PREFERENCE:
        for device in devices:
            if device.kind == kind:
                return Selection(device=device, requested=None, warnings=_warnings(device))

    device = devices[0]
    return Selection(
        device=device,
        requested=None,
        warnings=(f"{device.name} is not a device this has been tested on.",) + _warnings(device),
    )


def _warnings(device: Device) -> tuple[str, ...]:
    if device.kind == "NPU":
        return ()
    if device.kind == "GPU":
        return (
            f"Running on {device.name}, not the NPU. The GPU path is untested here; "
            "expect different per-cycle timings and watch the lag figure on the status bar.",
        )
    if device.kind == "CPU":
        return (
            "Running on the CPU. Every cycle now competes with everything else on "
            "the machine, and the two-cycle commit policy doubles whatever that costs, "
            "so captions will lag noticeably further behind the audio.",
            "Run vinowhisper-doctor to see why the NPU was not picked up.",
        )
    return (f"Running on {device.name}, which this has never been tested against.",)


def accel_nodes() -> list[Path]:
    try:
        return sorted(ACCEL_DIR.glob(ACCEL_GLOB))
    except OSError:
        return []


def render_nodes() -> list[Path]:
    try:
        return sorted(DRI_DIR.glob("renderD*"))
    except OSError:
        return []


PCI_DIR = Path("/sys/bus/pci/devices")
OPENCL_VENDORS_DIR = Path("/etc/OpenCL/vendors")

_VENDORS = {"0x8086": "Intel", "0x1002": "AMD", "0x1022": "AMD", "0x10de": "NVIDIA"}
# amdxdna's modalias table. AMD NPUs report class 0x1180, which Intel reuses for thermal and telemetry.
AMD_NPU_IDS = frozenset(
    {"0x1502", "0x17f0", "0x17f1", "0x17f2", "0x17f3", "0x1b0a", "0x1b0b", "0x1b0c"}
)
NPU_DRIVERS = frozenset({"intel_vpu", "amdxdna"})


@dataclass(frozen=True)
class Hardware:
    slot: str
    kind: str
    vendor: str
    pci_id: str
    driver: str = ""

    def __str__(self) -> str:
        bound = f"driver {self.driver}" if self.driver else "no driver bound"
        return f"{self.vendor} {self.kind} [{self.pci_id}] at {self.slot}, {bound}"


def _read_attr(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""


def _hardware_kind(pci_class: str, vendor: str, device: str, driver: str) -> str | None:
    if pci_class.startswith("0x03"):
        return "GPU"
    if pci_class.startswith("0x12") or driver in NPU_DRIVERS:
        return "NPU"
    if vendor == "0x1022" and device in AMD_NPU_IDS:
        return "NPU"
    return None


def hardware(root: Path | None = None) -> list[Hardware]:
    try:
        slots = sorted((root or PCI_DIR).iterdir())
    except OSError:
        return []

    found = []
    for slot in slots:
        vendor = _read_attr(slot / "vendor")
        device = _read_attr(slot / "device")
        try:
            driver = (slot / "driver").resolve(strict=True).name
        except OSError:
            driver = ""
        kind = _hardware_kind(_read_attr(slot / "class"), vendor, device, driver)
        if kind is None:
            continue
        found.append(
            Hardware(
                slot=slot.name,
                kind=kind,
                vendor=_VENDORS.get(vendor, vendor or "unknown"),
                pci_id=f"{vendor.removeprefix('0x')}:{device.removeprefix('0x')}",
                driver=driver,
            )
        )
    return found


def opencl_icds() -> list[Path]:
    try:
        return sorted(OPENCL_VENDORS_DIR.glob("*.icd"))
    except OSError:
        return []


def gpu_notes(
    inventory: list[Device],
    found: list[Hardware],
    distro_info: "distro.Distro | None" = None,
) -> list[Note]:
    gpus = [hw for hw in found if hw.kind == "GPU"]
    notes = [
        Note(None, "gpu", f"{gpu}: OpenVINO only drives Intel GPUs, so this is not a fallback")
        for gpu in gpus
        if gpu.vendor != "Intel"
    ]
    intel = [gpu for gpu in gpus if gpu.vendor == "Intel"]
    if not intel:
        return notes

    if any(device.kind == "GPU" for device in inventory):
        return notes + [Note(True, "gpu", f"{intel[0]}, visible to OpenVINO")]

    icds = opencl_icds()
    if not icds:
        cause = f"there is no OpenCL ICD in {OPENCL_VENDORS_DIR}, so the compute runtime is missing"
    elif not any("intel" in icd.name for icd in icds):
        names = ", ".join(icd.name for icd in icds)
        cause = f"the OpenCL ICDs present ({names}) are not Intel's compute runtime"
    else:
        cause = "Intel's OpenCL ICD is present, so the runtime may predate this GPU"
    remedy = distro.remediation(distro.GPU_RUNTIME, distro_info)
    detail = f"{intel[0]}, but OpenVINO does not see it: {cause}.\n" + "\n".join(remedy.lines())
    return notes + [Note(False, "gpu", detail)]


def _module_loaded(name: str) -> bool | None:
    try:
        modules = Path("/proc/modules").read_text(encoding="utf-8")
    except OSError:
        return None
    return any(line.startswith(f"{name} ") for line in modules.splitlines())


def npu_preflight() -> list[Note]:
    notes: list[Note] = []
    nodes = accel_nodes()
    if not nodes:
        notes.append(
            Note(
                False,
                "kernel driver",
                "no /dev/accel/accel* node — the intel_vpu driver did not bind. "
                "Either the kernel predates support for this silicon or probing "
                "failed; `dmesg | grep -i vpu` says which.",
            )
        )
        loaded = _module_loaded("intel_vpu")
        if loaded is False:
            notes.append(
                Note(False, "intel_vpu module", "not loaded (try `sudo modprobe intel_vpu`)")
            )
        return notes

    node = nodes[0]
    notes.append(Note(True, "kernel driver", f"{node} present"))

    if os.access(node, os.R_OK | os.W_OK):
        notes.append(Note(True, "device permissions", f"{node} is readable and writable"))
    else:
        notes.append(
            Note(
                False,
                "device permissions",
                f"cannot open {node} — add yourself to the owning group "
                "(`sudo usermod -aG render $USER`, then log out and back in)",
            )
        )
    return notes


LEVEL_ZERO_STEM = "libze_intel_npu.so"
COMPILER_LIB = "libopenvino_intel_npu_compiler.so"
COMPILER_LOADER = "libopenvino_intel_npu_compiler_loader.so"

LIBRARY_DIRS = (
    "/usr/lib64",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib",
    "/usr/local/lib64",
    "/usr/local/lib",
)

_DRIVER_MARK = re.compile(rb"npu-linux-driver-ci-([\d][\d.]*\d)")
_OPENVINO_MARK = re.compile(rb"20\d\d\.\d+\.\d+(?:-\w+){0,2}")


def library_dirs() -> list[Path]:
    seen: dict[Path, None] = {}
    raw = os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
    for text in [entry for entry in raw if entry] + list(LIBRARY_DIRS):
        try:
            path = Path(text)
            if path.is_dir():
                seen.setdefault(path, None)
        except OSError:
            continue
    return list(seen)


def find_library(name: str) -> Path | None:
    for directory in library_dirs():
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in text.split("."):
        if not part.isdigit():
            break
        parts.append(int(part))
    return tuple(parts)


def _marker(path: Path, pattern: re.Pattern[bytes]) -> str | None:
    try:
        with (
            path.open("rb") as handle,
            mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data,
        ):
            match = pattern.search(data)
            if match is None:
                return None
            group = match.group(1) if match.groups() else match.group()
            return group.decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def _level_zero_notes() -> list[Note]:
    soname = find_library(f"{LEVEL_ZERO_STEM}.1")
    if soname is None:
        return [
            Note(
                False,
                "level-zero NPU backend",
                f"no {LEVEL_ZERO_STEM}.1 on the library path, so OpenVINO's NPU "
                "plugin has nothing to load and the device will not enumerate",
            )
        ]

    target = soname.resolve()
    version = _marker(target, _DRIVER_MARK) or "unknown version"
    notes = [Note(True, "level-zero NPU backend", f"{target} ({version})")]

    installed = sorted(
        (path for path in target.parent.glob(f"{LEVEL_ZERO_STEM}.*") if not path.is_symlink()),
        key=lambda path: _version_tuple(path.name.split(".so.", 1)[-1]),
    )
    if installed and installed[-1] != target:
        newer = installed[-1]
        notes.append(
            Note(
                False,
                "level-zero version",
                f"{soname.name} points at {target.name} while {newer.name} is also "
                "installed. A package reinstall rewrites this symlink to the "
                "distro's version, which enumerates the NPU and then fails to "
                f"compile for it. `sudo ln -sf {newer.name} {soname}` selects the "
                "newer one again.",
            )
        )
    return notes


def _compiler_notes(distro_info: "distro.Distro | None" = None) -> list[Note]:
    compiler = find_library(COMPILER_LIB)
    loader = find_library(COMPILER_LOADER)
    if compiler is None:
        remedy = distro.remediation(distro.NPU_COMPILER, distro_info)
        return [
            Note(
                False,
                "NPU compiler",
                f"no {COMPILER_LIB} on the library path. Nothing above catches "
                "this: the device still enumerates and every compile fails.\n"
                + "\n".join(remedy.lines()),
            )
        ]

    version = _marker(loader, _OPENVINO_MARK) if loader else None
    version = version or _marker(compiler, _OPENVINO_MARK) or "unknown version"
    notes = [Note(True, "NPU compiler", f"{compiler} (OpenVINO {version})")]
    if loader is None:
        notes.append(
            Note(
                False,
                "NPU compiler loader",
                f"{COMPILER_LOADER} is missing; it ships beside {COMPILER_LIB} "
                "and is what the plugin actually dlopen()s",
            )
        )
    return notes


def npu_userspace(distro_info: "distro.Distro | None" = None) -> list[Note]:
    return _level_zero_notes() + _compiler_notes(distro_info)


def npu_missing_help(distro_info: distro.Distro | None = None) -> list[str]:
    lines: list[str] = []
    for note in npu_preflight():
        if note.ok is not True:
            lines.append(f"{note.label}: {note.detail}")

    if accel_nodes():
        lines.append(
            "The kernel driver is bound, so what is missing is the userspace NPU "
            "driver that OpenVINO's NPU plugin loads through level-zero."
        )
        for note in npu_userspace(distro_info):
            if note.ok is not True:
                lines.append(f"{note.label}: {note.detail}")
    lines.extend(distro.remediation(distro.NPU_DRIVER, distro_info).lines())
    return lines
