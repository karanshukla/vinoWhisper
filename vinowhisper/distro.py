"""Which Linux this is, and what the things it needs are called here.

Every actionable message this tool prints — the doctor's remediation lines, the
setup wizard's steps, the "pw-record not found" error — needs a package name,
and package names are the one thing that genuinely differs between distros.
Keeping that mapping in one table means the rest of the code can ask for a
*capability* ("something that provides pw-record") and let this module answer
in the local dialect.

Scope, deliberately: this maps capabilities to commands, it does not run them.
Nothing here shells out, so it is safe to call from anywhere and trivially
testable against a fixture `os-release`.

Honesty about coverage. Fedora is the machine this was built on and the only
family where the package names are confirmed by use. The rest are derived from
each distro's package index rather than from a working install, so the wizard
and the doctor always print the command instead of silently running it, and
every NPU entry carries the upstream release URL as the authoritative fallback.
If you fix a wrong package name for your distro, fix it here.
"""

from dataclasses import dataclass, field
from pathlib import Path

OS_RELEASE = Path("/etc/os-release")

# Capabilities the rest of the codebase asks for by name.
AUDIO_PIPEWIRE = "pipewire-tools"  # pw-record, pw-dump
AUDIO_PULSE = "pulse-tools"  # pactl, parec
NPU_DRIVER = "npu-driver"  # level-zero + the NPU compiler/firmware userspace
NPU_COMPILER = "npu-compiler"  # libopenvino_intel_npu_compiler.so, packaged nowhere
GPU_RUNTIME = "gpu-runtime"  # compute runtime for OpenVINO's GPU plugin

# The NPU userspace is the one dependency with no reliable distro package
# across the board, so every family's guidance ends up pointing here.
NPU_RELEASES_URL = "https://github.com/intel/linux-npu-driver/releases"

# The compiler libraries are a special case even among the NPU parts: no family
# in the table below packages them at all, so there is no local dialect to
# answer in and the steps are the same everywhere. They are plain userspace
# .so files with no kernel-module or packaging-system dependency, which is why
# extracting Intel's Ubuntu .debs works unchanged on an rpm distro.
NPU_COMPILER_STEPS = (
    "No distro packages libopenvino_intel_npu_compiler.so. Intel ships it only "
    "inside intel-driver-compiler-npu, in the release archive below.",
    "Download the linux-npu-driver tarball for your architecture, then:",
    "  tar xf linux-npu-driver-*.tar.gz",
    "  dpkg-deb -x intel-driver-compiler-npu_*.deb extracted",
    "  sudo install -m 0755 $(find extracted -name 'libopenvino_intel_npu_compiler*.so') /usr/lib64/",
    "  sudo ldconfig",
    "dpkg-deb is in the `dpkg` package and is present on rpm distros too, so "
    "no alien/rpm2cpio conversion is needed. The result is untracked by your "
    "package manager: note it somewhere, because nothing will upgrade it.",
)


@dataclass(frozen=True)
class Distro:
    """A parsed /etc/os-release, plus the family whose tooling it uses."""

    id: str
    name: str
    version: str
    like: tuple[str, ...] = ()
    family: str = "unknown"

    @property
    def known(self) -> bool:
        return self.family != "unknown"

    def __str__(self) -> str:
        return f"{self.name} ({self.id}, {self.family} family)" if self.known else self.name


@dataclass(frozen=True)
class Remediation:
    """How to obtain a capability here: commands to run, caveats to read."""

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
    """One packaging ecosystem: how to install, and what things are called."""

    name: str
    install: str  # prefix; package names are appended
    packages: dict[str, tuple[str, ...]] = field(default_factory=dict)
    notes: dict[str, tuple[str, ...]] = field(default_factory=dict)


# `--needed`/`-y`-style flags are included so the printed command is the one to
# run, not a template to fix up. Nothing here is executed without the user
# saying yes first (see wizard.py), so a non-interactive flag is a convenience,
# not a policy.
_FAMILIES: dict[str, Family] = {
    "fedora": Family(
        name="fedora",
        install="sudo dnf install -y",
        packages={
            AUDIO_PIPEWIRE: ("pipewire-utils",),
            AUDIO_PULSE: ("pulseaudio-utils",),
            NPU_DRIVER: ("intel-npu-driver", "level-zero"),
            GPU_RUNTIME: ("intel-compute-runtime", "level-zero"),
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
    # NixOS is not "a distro with a different package manager", it is a
    # different model: imperative installs do not persist, so printing an
    # `nix-env` line would be actively bad advice. Point at configuration.nix.
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

# ID_LIKE is not always present and not always helpful, so map the IDs that
# actually turn up first and fall back to ID_LIKE only when the ID is unknown.
_ID_TO_FAMILY = {
    "fedora": "fedora",
    "rhel": "fedora",
    "centos": "fedora",
    "almalinux": "fedora",
    "rocky": "fedora",
    "nobara": "fedora",
    "bazzite": "fedora",
    "silverblue": "fedora",
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
        # Values are shell-quoted, and PRETTY_NAME essentially always is.
        values[key.strip()] = value.strip().strip("\"'")
    return values


def detect(path: Path = OS_RELEASE) -> Distro:
    """Read /etc/os-release. Never raises: an unreadable file is 'unknown'."""
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
    )


def remediation(capability: str, distro: Distro | None = None) -> Remediation:
    """How to get `capability` on this system, as commands plus caveats."""
    distro = distro or detect()
    family = _FAMILIES.get(distro.family)
    url = NPU_RELEASES_URL if capability in (NPU_DRIVER, NPU_COMPILER) else ""

    if family is None:
        return Remediation(
            capability=capability,
            notes=(
                f"Unrecognised distro ({distro.name}); install the equivalent of "
                f"'{capability}' with your package manager.",
            ),
            url=url,
        )

    packages = family.packages.get(capability, ())
    commands = (f"{family.install} {' '.join(packages)}",) if packages and family.install else ()
    # NPU_COMPILER has no packaged form anywhere, so a family that says nothing
    # about it is the normal case rather than a gap in the table.
    default = NPU_COMPILER_STEPS if capability == NPU_COMPILER else ()
    return Remediation(
        capability=capability,
        commands=commands,
        notes=family.notes.get(capability, default),
        url=url,
    )


def install_command(capability: str, distro: Distro | None = None) -> str | None:
    """The single command that installs `capability`, if there is one.

    None means "no package, read the notes" — which is the normal case for the
    NPU driver on most distros, not an error.
    """
    commands = remediation(capability, distro).commands
    return commands[0] if commands else None
