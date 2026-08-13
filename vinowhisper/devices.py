"""What OpenVINO can actually run on here, and what to do when it's the wrong thing.

Two separate questions, deliberately answered by two separate code paths:

1. **What does OpenVINO enumerate?** `available()` asks the runtime. That is
   the ground truth for what `WhisperPipeline` will accept, and it is the only
   check that matters once everything works.
2. **Why isn't the NPU there?** `npu_preflight()` looks at the kernel side —
   the /dev/accel node, its permissions — without importing OpenVINO at all.
   This exists because "no NPU among ['CPU']" is a useless error message: the
   interesting part is always whether the kernel driver bound, whether you are
   in the right group, or whether only the userspace half is missing. Each of
   those has a different fix and they are indistinguishable from OpenVINO's
   device list.

**Fallback policy.** NPU, then GPU, then CPU, and anything below NPU is loud
about it everywhere it surfaces (server log, /health, status bar, doctor). CPU
is not a supported configuration so much as an escape hatch that keeps the tool
running while you fix the driver — the whole latency design assumes NPU-class
per-cycle times (~1.19s per 30s window, measured 2026-08-03), and the
LocalAgreement-2 commit policy multiplies any regression by two.

**The export is device-specific and this is the trap.** NPU needs a
`--disable-stateful` export; that same export cannot run on CPU at all, it
fails on a `beam_idx` port error, which is incidentally how the NPU was
confirmed to be doing real work rather than silently falling back. So falling
back to CPU means using a *different model directory*, and if that export
doesn't exist the honest answer is to say so rather than to fail deep inside
the pipeline constructor. See config.model_dir().
"""

import os
from dataclasses import dataclass
from pathlib import Path

from . import distro

# Preference order for automatic selection. Not configurable on purpose: there
# is no machine where GPU-over-NPU is the right default for this workload.
PREFERENCE = ("NPU", "GPU", "CPU")

ACCEL_GLOB = "accel*"
ACCEL_DIR = Path("/dev/accel")
DRI_DIR = Path("/dev/dri")


class DeviceError(RuntimeError):
    """OpenVINO is unusable, or the requested device does not exist."""


@dataclass(frozen=True)
class Device:
    name: str  # OpenVINO's own name: "NPU", "GPU.0", "CPU"
    kind: str  # "NPU" | "GPU" | "CPU" | ...
    full_name: str = ""  # FULL_DEVICE_NAME, e.g. "Intel(R) AI Boost"

    def __str__(self) -> str:
        return f"{self.name} ({self.full_name})" if self.full_name else self.name


@dataclass(frozen=True)
class Selection:
    """A chosen device plus everything the UI needs to be honest about it."""

    device: Device
    requested: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        return self.device.kind

    @property
    def degraded(self) -> bool:
        """True when this is not the NPU, i.e. when captions will be slower."""
        return self.device.kind != "NPU"


@dataclass(frozen=True)
class Note:
    """One preflight observation. `ok=None` means it could not be determined."""

    ok: bool | None
    label: str
    detail: str


def kind_of(name: str) -> str:
    """Map "GPU.1" to "GPU" — OpenVINO suffixes multi-instance devices."""
    return name.split(".")[0].upper()


def available() -> list[Device]:
    """Every device OpenVINO enumerates, in its own order.

    Raises DeviceError rather than returning [] when OpenVINO itself is
    missing or broken: an empty list would read as "no hardware" when the real
    answer is "the runtime never loaded".
    """
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
    """Pick a device, with warnings attached when it isn't the NPU.

    `preferred` may be a kind ("NPU"), a full OpenVINO name ("GPU.1"), or
    None/"auto" for PREFERENCE order. An explicit request that isn't available
    is an error, never a silent downgrade: someone who typed --device NPU wants
    to know it didn't happen.
    """
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

    # Something exotic (an ARM plugin, a remote device). Take it and say so.
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


# --- Kernel-side preflight, no OpenVINO involved --------------------------


def accel_nodes() -> list[Path]:
    """/dev/accel/accelN nodes, which the in-tree intel_vpu driver creates."""
    try:
        return sorted(ACCEL_DIR.glob(ACCEL_GLOB))
    except OSError:
        return []


def render_nodes() -> list[Path]:
    try:
        return sorted(DRI_DIR.glob("renderD*"))
    except OSError:
        return []


def _module_loaded(name: str) -> bool | None:
    """Whether a kernel module is loaded. None if /proc/modules is unreadable.

    A False here is weaker evidence than it looks — the driver can be built
    into the kernel rather than modular — so the device node above is the
    check that decides, and this only ever adds colour.
    """
    try:
        modules = Path("/proc/modules").read_text(encoding="utf-8")
    except OSError:
        return None
    return any(line.startswith(f"{name} ") for line in modules.splitlines())


def npu_preflight() -> list[Note]:
    """Kernel-side reasons the NPU might not show up in OpenVINO's device list."""
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

    # The node is typically group `render`, mode 0660: a fresh user account
    # that was never added to that group sees the node and still cannot open
    # it, which surfaces in OpenVINO as a plain missing NPU.
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


def npu_missing_help(distro_info: distro.Distro | None = None) -> list[str]:
    """Everything worth saying when the NPU didn't turn up, in fix-first order."""
    lines: list[str] = []
    for note in npu_preflight():
        if note.ok is not True:
            lines.append(f"{note.label}: {note.detail}")

    if accel_nodes():
        # Kernel side is fine, so the missing half is userspace: the level-zero
        # loader plus Intel's NPU compiler/firmware packages.
        lines.append(
            "The kernel driver is bound, so what is missing is the userspace NPU "
            "driver that OpenVINO's NPU plugin loads through level-zero."
        )
    lines.extend(distro.remediation(distro.NPU_DRIVER, distro_info).lines())
    return lines
