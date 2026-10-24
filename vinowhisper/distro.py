import os
from dataclasses import dataclass, field
from pathlib import Path

OS_RELEASE = Path("/etc/os-release")
ROOT = Path("/")

# Image-based systems, where /usr is read-only and packages go into the next boot.
IMAGE_OSTREE = "ostree"
IMAGE_TRANSACTIONAL = "transactional"

AUDIO_PIPEWIRE = "pipewire-tools"
AUDIO_PULSE = "pulse-tools"
NPU_DRIVER = "npu-driver"
NPU_COMPILER = "npu-compiler"
GPU_RUNTIME = "gpu-runtime"

NPU_RELEASES_URL = "https://github.com/intel/linux-npu-driver/releases"

# `ovfetch npu install` (ovfetch 0.6.0) is the fix on every distro: it takes
# the compiler from Intel's release into ~/.local/lib/ovfetch-npu, checked
# against ovfetch's ledger, and vinowhisper loads it from there (no root, no
# dpkg, nothing for the package manager to revert).
NPU_COMPILER_COMMANDS = ("vinowhisper-setup --ovfetch", "ovfetch npu install")

NPU_COMPILER_STEPS = (
    "No distro packages the NPU compiler. Intel ships it only inside "
    "intel-driver-compiler-npu, in the release archive below. The two commands "
    "above fetch it into ~/.local/lib/ovfetch-npu, hash-checked, and "
    "vinowhisper loads it from there; re-run the doctor afterwards.",
    "By hand instead: download the linux-npu-driver tarball for the same "
    "version as your driver (the compiler's file name changed between 1.32 "
    "and 1.35), then:",
    "  tar xf linux-npu-driver-*.tar.gz",
    "  dpkg-deb -x intel-driver-compiler-npu_*.deb extracted",
    "  sudo install -m 0755 $(find extracted -name '*compiler*.so') /usr/lib64/",
    "  sudo ldconfig",
    "dpkg-deb is in the `dpkg` package and is present on rpm distros too, so "
    "no alien/rpm2cpio conversion is needed. The result is untracked by your "
    "package manager: note it somewhere, because nothing will upgrade it.",
)

# /usr/lib64 is read-only on an image-based system; /usr/local and /etc are not.
NPU_COMPILER_STEPS_IMAGE = (
    "No distro packages the NPU compiler. Intel ships it only inside "
    "intel-driver-compiler-npu, in the release archive below. The two commands "
    "above fetch it into ~/.local/lib/ovfetch-npu, hash-checked, and "
    "vinowhisper loads it from there: nothing to layer, no reboot; re-run the "
    "doctor afterwards.",
    "By hand instead: /usr/lib64 is read-only on this system, so it goes in "
    "/usr/local/lib64. Download the linux-npu-driver tarball for the same "
    "version as your driver, then:",
    "  tar xf linux-npu-driver-*.tar.gz",
    "  dpkg-deb -x intel-driver-compiler-npu_*.deb extracted",
    "  sudo install -Dm 0755 -t /usr/local/lib64 $(find extracted -name '*compiler*.so')",
    "  echo /usr/local/lib64 | sudo tee /etc/ld.so.conf.d/intel-npu-compiler.conf",
    "  sudo ldconfig",
    "No dpkg on the host? Run the dpkg-deb line in a toolbox or distrobox, "
    "which shares your home directory. Nothing will upgrade the result: note "
    "it somewhere.",
)


@dataclass(frozen=True)
class Distro:
    id: str
    name: str
    version: str
    like: tuple[str, ...] = ()
    family: str = "unknown"
    image: str = ""

    @property
    def known(self) -> bool:
        return self.family != "unknown"

    @property
    def image_based(self) -> bool:
        return bool(self.image)

    def __str__(self) -> str:
        image = f", image-based ({self.image})" if self.image else ""
        return f"{self.name} ({self.id}, {self.family} family{image})" if self.known else self.name


@dataclass(frozen=True)
class Remediation:
    capability: str
    commands: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    url: str = ""

    @property
    def actionable(self) -> bool:
        return bool(self.commands or self.url)

    def lines(self) -> list[str]:
        out = [f"  $ {command}" for command in self.commands]
        out += [f"  {note}" for note in self.notes]
        if self.url:
            out.append(f"  {self.url}")
        return out


@dataclass(frozen=True)
class Family:
    name: str
    install: str
    packages: dict[str, tuple[str, ...]] = field(default_factory=dict)
    notes: dict[str, tuple[str, ...]] = field(default_factory=dict)


_FAMILIES: dict[str, Family] = {
    "fedora": Family(
        name="fedora",
        install="sudo dnf install -y",
        packages={
            AUDIO_PIPEWIRE: ("pipewire-utils",),
            AUDIO_PULSE: ("pulseaudio-utils",),
            # Fedora has no "level-zero"; both of these pull in oneapi-level-zero themselves.
            NPU_DRIVER: ("intel-npu-driver",),
            GPU_RUNTIME: ("intel-compute-runtime",),
        },
        notes={
            NPU_DRIVER: (
                "intel-npu-driver landed in Fedora fairly recently; on an older "
                "release dnf will not find it and the upstream build below is "
                "the way in.",
            ),
        },
    ),
    "debian": Family(
        name="debian",
        install="sudo apt install -y",
        packages={
            AUDIO_PIPEWIRE: ("pipewire-bin",),
            AUDIO_PULSE: ("pulseaudio-utils",),
            GPU_RUNTIME: ("intel-opencl-icd", "libze1", "libze-intel-gpu1"),
        },
        notes={
            NPU_DRIVER: (
                "Debian/Ubuntu have no NPU driver package. Intel ships .debs on "
                "the release page below: intel-driver-compiler-npu, "
                "intel-fw-npu and intel-level-zero-npu, installed together with "
                "`sudo dpkg -i ./*.deb`. Match the build to your Ubuntu version.",
            ),
        },
    ),
    "arch": Family(
        name="arch",
        install="sudo pacman -S --needed",
        packages={
            AUDIO_PIPEWIRE: ("pipewire",),
            AUDIO_PULSE: ("libpulse",),
            GPU_RUNTIME: ("intel-compute-runtime", "level-zero-loader"),
        },
        notes={
            NPU_DRIVER: (
                "In the AUR rather than the official repos: "
                "`paru -S intel-npu-driver` (or yay). level-zero-loader from "
                "extra is a hard prerequisite.",
            ),
        },
    ),
    "suse": Family(
        name="suse",
        install="sudo zypper install -y",
        packages={
            AUDIO_PIPEWIRE: ("pipewire-tools",),
            AUDIO_PULSE: ("pulseaudio-utils",),
            GPU_RUNTIME: ("intel-compute-runtime", "level-zero"),
        },
        notes={
            NPU_DRIVER: (
                "No NPU package in the openSUSE repos as of writing; build or "
                "unpack the upstream release below.",
            ),
        },
    ),
    "alpine": Family(
        name="alpine",
        install="sudo apk add",
        packages={
            AUDIO_PIPEWIRE: ("pipewire-tools",),
            AUDIO_PULSE: ("pulseaudio-utils",),
        },
        notes={
            NPU_DRIVER: ("musl, so the upstream glibc builds do not apply. Untested here.",),
            GPU_RUNTIME: ("musl, so Intel's compute runtime is unlikely to work as shipped.",),
        },
    ),
    "gentoo": Family(
        name="gentoo",
        install="sudo emerge --ask=n",
        packages={
            AUDIO_PIPEWIRE: ("media-video/pipewire",),
            AUDIO_PULSE: ("media-sound/pulseaudio-daemon",),
            GPU_RUNTIME: ("dev-libs/intel-compute-runtime", "dev-libs/level-zero"),
        },
        notes={
            NPU_DRIVER: ("dev-libs/intel-npu-driver exists in some overlays; otherwise upstream.",),
        },
    ),
    "void": Family(
        name="void",
        install="sudo xbps-install -Sy",
        packages={
            AUDIO_PIPEWIRE: ("pipewire",),
            AUDIO_PULSE: ("pulseaudio-utils",),
            GPU_RUNTIME: ("intel-compute-runtime", "level-zero"),
        },
    ),
    "nixos": Family(
        name="nixos",
        install="",
        packages={},
        notes={
            AUDIO_PIPEWIRE: ("services.pipewire.enable = true;",),
            AUDIO_PULSE: ("hardware.pulseaudio or services.pipewire.pulse.enable",),
            NPU_DRIVER: ("hardware.intel-npu (unstable) or an overlay for intel-npu-driver.",),
            GPU_RUNTIME: ("hardware.graphics.extraPackages = [ pkgs.intel-compute-runtime ];",),
        },
    ),
}

_ID_TO_FAMILY = {
    "fedora": "fedora",
    "rhel": "fedora",
    "centos": "fedora",
    "almalinux": "fedora",
    "rocky": "fedora",
    "nobara": "fedora",
    "bazzite": "fedora",
    "aurora": "fedora",
    "bluefin": "fedora",
    "debian": "debian",
    "ubuntu": "debian",
    "pop": "debian",
    "linuxmint": "debian",
    "elementary": "debian",
    "raspbian": "debian",
    "arch": "arch",
    "cachyos": "arch",
    "endeavouros": "arch",
    "manjaro": "arch",
    "garuda": "arch",
    "opensuse": "suse",
    "opensuse-tumbleweed": "suse",
    "opensuse-leap": "suse",
    "opensuse-microos": "suse",
    "opensuse-slowroll": "suse",
    "suse": "suse",
    "sles": "suse",
    "alpine": "alpine",
    "gentoo": "gentoo",
    "void": "void",
    "nixos": "nixos",
}


def _parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _read_only(path: Path) -> bool:
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def _image(root: Path) -> str:
    if (root / "run/ostree-booted").exists():
        return IMAGE_OSTREE
    # Aeon and MicroOS: a read-only root snapshot that transactional-update writes the next one of.
    tool = any((root / d / "transactional-update").exists() for d in ("usr/sbin", "usr/bin"))
    if tool and _read_only(root / "usr"):
        return IMAGE_TRANSACTIONAL
    return ""


def detect(path: Path = OS_RELEASE, root: Path = ROOT) -> Distro:
    try:
        values = _parse_os_release(path.read_text(encoding="utf-8"))
    except OSError:
        values = {}

    distro_id = values.get("ID", "").lower()
    like = tuple(values.get("ID_LIKE", "").lower().split())
    family = _ID_TO_FAMILY.get(distro_id, "")
    if not family:
        for candidate in like:
            family = _ID_TO_FAMILY.get(candidate, "")
            if family:
                break
    return Distro(
        id=distro_id or "unknown",
        name=values.get("PRETTY_NAME") or values.get("NAME") or "unknown Linux",
        version=values.get("VERSION_ID", ""),
        like=like,
        family=family or "unknown",
        image=_image(root),
    )


# The tool that writes the next deployment, per (image kind, family); anything else gets notes.
_IMAGE_INSTALL = {
    (IMAGE_OSTREE, "fedora"): "sudo rpm-ostree install --idempotent",
    (IMAGE_TRANSACTIONAL, "suse"): "sudo transactional-update --non-interactive pkg install",
}

_REBOOT_NOTE = (
    "/usr is read-only here, so this installs into the next boot: reboot "
    "(`systemctl reboot`) before it is usable, then re-run vinowhisper-setup."
)


def remediation(capability: str, distro: Distro | None = None) -> Remediation:
    distro = distro or detect()
    family = _FAMILIES.get(distro.family)
    url = NPU_RELEASES_URL if capability in (NPU_DRIVER, NPU_COMPILER) else ""

    if family is None:
        if capability == NPU_COMPILER:
            # Not a package anywhere, so the distro does not matter.
            return Remediation(
                capability=capability,
                commands=NPU_COMPILER_COMMANDS,
                notes=NPU_COMPILER_STEPS_IMAGE if distro.image_based else NPU_COMPILER_STEPS,
                url=url,
            )
        return Remediation(
            capability=capability,
            notes=(
                f"Unrecognised distro ({distro.name}); install the equivalent of "
                f"'{capability}' with your package manager.",
            ),
            url=url,
        )

    packages = family.packages.get(capability, ())
    install = family.install
    if distro.image_based:
        install = _IMAGE_INSTALL.get((distro.image, distro.family), "")
    commands: tuple[str, ...] = (f"{install} {' '.join(packages)}",) if packages and install else ()
    default: tuple[str, ...] = ()
    if capability == NPU_COMPILER:
        commands = NPU_COMPILER_COMMANDS
        default = NPU_COMPILER_STEPS_IMAGE if distro.image_based else NPU_COMPILER_STEPS
    notes = family.notes.get(capability, default)
    if distro.image_based and packages:
        if commands:
            notes = (*notes, _REBOOT_NOTE)
        else:
            notes = (
                *notes,
                f"/usr is read-only on {distro.name}, so `{family.install}` cannot work. "
                f"Add {' '.join(packages)} to your image, or layer them with its own tool.",
            )
    return Remediation(
        capability=capability,
        commands=commands,
        notes=notes,
        url=url,
    )


def install_needs_reboot(distro: Distro) -> bool:
    return (distro.image, distro.family) in _IMAGE_INSTALL


def install_command(capability: str, distro: Distro | None = None) -> str | None:
    commands = remediation(capability, distro).commands
    return commands[0] if commands else None
