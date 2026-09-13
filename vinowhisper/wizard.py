import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import __version__, capture, config, devices, distro, integrity, overlay

BIN_DIR = Path.home() / ".local/bin"
UNIT_DIR = Path.home() / ".config/systemd/user"
COMPLETION_DIR = Path.home() / ".local/share/bash-completion/completions"
COMMANDS = ("caption", "server", "replay", "doctor", "setup")

# Keep in step with scripts/convert_model.sh.
EXPORT_TASK = "automatic-speech-recognition-with-past"


@dataclass
class Outcome:
    ok: bool | None
    summary: str


class Wizard:
    def __init__(self, assume_yes: bool = False, dry_run: bool = False, device: str = "auto"):
        self.assume_yes = assume_yes
        self.dry_run = dry_run
        self.device = device
        self.distro = distro.detect()
        self.failed: list[str] = []
        self.skipped: list[str] = []

    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def confirm(self, prompt: str) -> bool:
        if self.dry_run:
            return False
        if self.assume_yes:
            return True
        if not sys.stdin.isatty():
            self.say("  (not a terminal, and no --yes — skipping)")
            return False
        try:
            answer = input(f"  {prompt} [y/N] ").strip().lower()
        except EOFError:
            return False
        return answer in ("y", "yes")

    def run(self, argv: list[str], why: str) -> bool:
        self.say(f"  $ {' '.join(argv)}")
        if not self.confirm(why):
            return False
        try:
            subprocess.run(argv, check=True)
        except FileNotFoundError:
            self.say(f"  ✗ {argv[0]} not found")
            return False
        except subprocess.CalledProcessError as exc:
            self.say(f"  ✗ exited {exc.returncode}")
            return False
        return True

    def step(self, title: str, action: Callable[[], Outcome], optional: bool = False) -> None:
        self.say(f"\n── {title}")
        outcome = action()
        marker = {True: "✓", False: "✗", None: "…"}[outcome.ok]
        self.say(f"  {marker} {outcome.summary}")
        if outcome.ok is False:
            self.failed.append(title)
        elif outcome.ok is None and not optional:
            self.skipped.append(title)

    def check_python(self) -> Outcome:
        version = sys.version_info
        text = f"{version.major}.{version.minor}.{version.micro}"
        if version[:2] == (3, 14):
            return Outcome(False, f"Python {text} cannot export the model; use 3.13 or older")
        return Outcome(True, f"Python {text} at {sys.executable}")

    def check_audio(self) -> Outcome:
        backends = capture.available_backends()
        if backends:
            active = backends[0]
            extra = "" if active.supports_app_capture else " (no per-application capture)"
            return Outcome(True, f"{active.record} found — {active.name} backend{extra}")

        self.say("  No capture tool installed. PipeWire is preferred; PulseAudio also works.")
        command = distro.install_command(distro.AUDIO_PIPEWIRE, self.distro)
        if command is None:
            for line in distro.remediation(distro.AUDIO_PIPEWIRE, self.distro).lines():
                self.say(line)
            return Outcome(None, "install a capture tool by hand, then re-run")
        if self.run(command.split(), "install it?"):
            return Outcome(True, "capture tool installed")
        return Outcome(None, "no capture tool yet — captions cannot run without one")

    def check_device(self) -> Outcome:
        try:
            inventory = devices.available()
        except devices.DeviceError as exc:
            return Outcome(False, str(exc))

        self.say(f"  OpenVINO sees: {', '.join(str(device) for device in inventory)}")
        try:
            selection = devices.select(self.device, inventory)
        except devices.DeviceError as exc:
            return Outcome(False, str(exc))

        if not selection.degraded:
            return Outcome(True, f"will run on {selection.device}")

        for warning in selection.warnings:
            self.say(f"  ⚠ {warning}")
        found = devices.hardware()
        intel_npu = any(hw.kind == "NPU" and hw.vendor == "Intel" for hw in found)
        if not any(device.kind == "NPU" for device in inventory):
            self.say("")
            if found and not intel_npu:
                for hw in found:
                    if hw.kind == "NPU":
                        self.say(f"  {hw}: OpenVINO cannot drive a non-Intel NPU.")
                if not any(hw.kind == "NPU" for hw in found):
                    self.say("  No NPU on the PCI bus, so there is no NPU driver to install.")
            else:
                for line in devices.npu_missing_help(self.distro):
                    self.say(f"  {line}")
                command = distro.install_command(distro.NPU_DRIVER, self.distro)
                if command and self.run(command.split(), "install the NPU driver?"):
                    return Outcome(None, "NPU driver installed — reboot, then re-run this")

        for note in devices.gpu_notes(inventory, found, self.distro):
            if note.ok is True:
                continue
            self.say("")
            for line in note.detail.splitlines():
                self.say(f"  {line}")
            command = distro.install_command(distro.GPU_RUNTIME, self.distro)
            if (
                note.ok is False
                and command
                and self.run(command.split(), "install the GPU compute runtime?")
            ):
                return Outcome(None, "GPU runtime installed; re-run this to pick it up")
        return Outcome(None, f"falling back to {selection.device.name}")

    def check_model(self) -> Outcome:
        try:
            kind = devices.select(self.device).kind
        except devices.DeviceError:
            kind = "NPU"
        variant = "npu" if kind == "NPU" else "stateful"
        directory = config.model_dir(kind)

        if directory.is_dir() and any(directory.glob("*.xml")):
            return self.check_digests(
                variant, directory, f"{variant} export present at {directory}"
            )

        self.say(f"  No {variant} export at {directory}.")
        cli = optimum_cli()
        if cli is None:
            self.say("  Exporting needs optimum, the optional `export` extra (it brings torch).")
            if not self.run(export_extra_argv(), "install it?"):
                return Outcome(None, "install the export extra, then re-run this")
            cli = optimum_cli()
            if cli is None:
                return Outcome(False, "the export extra installed, but optimum-cli is not on PATH")

        self.say("  This downloads ~1GB from Hugging Face and takes a few minutes.")
        if self.run([cli, *export_argv(variant, directory)[1:]], "export it now?"):
            return self.check_digests(variant, directory, f"exported to {directory}")
        return Outcome(None, f"run {config.export_command(variant)} when ready")

    def check_digests(self, variant: str, directory: Path, summary: str) -> Outcome:
        result = integrity.verify(directory, variant)
        if result.status == integrity.VERIFIED:
            return Outcome(True, f"{summary}, digests verified")
        for line in result.lines()[1:]:
            self.say(f"  {line.strip()}")
        if result.severe:
            return Outcome(False, f"{summary}, but {result.summary()}")
        return Outcome(True, f"{summary} ({result.status})")

    def install_units(self) -> Outcome:
        if not _has_systemd():
            return Outcome(
                None,
                "no systemd user session — start the server yourself with "
                "`vinowhisper-server` before captioning",
            )

        service, socket = unit_files(device=self.device)
        self.say(f"  Writing {UNIT_DIR}/vinowhisper-server.{{service,socket}}")
        self.say("  ExecStart: " + _exec_start(self.device))
        if not self.confirm("install the systemd units?"):
            return Outcome(None, "units not installed")

        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        (UNIT_DIR / "vinowhisper-server.service").write_text(service, encoding="utf-8")
        (UNIT_DIR / "vinowhisper-server.socket").write_text(socket, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        # The socket, never the service: the service is only ever socket-activated.
        enabled = subprocess.run(
            ["systemctl", "--user", "enable", "--now", "vinowhisper-server.socket"],
            check=False,
        )
        if enabled.returncode:
            return Outcome(False, "units written but `systemctl --user enable` failed")
        return Outcome(True, "socket unit enabled; the server starts on first use")

    def link_binaries(self) -> Outcome:
        bin_dir = Path(sys.executable).parent
        entry_points = [(bin_dir / f"vinowhisper-{name}") for name in COMMANDS]
        missing = [path.name for path in entry_points if not path.exists()]
        if missing:
            return Outcome(
                False,
                f"{', '.join(missing)} not next to {sys.executable} — run `uv sync` first",
            )
        if bin_dir == BIN_DIR:
            return Outcome(True, f"already installed in {BIN_DIR}")

        self.say(f"  Symlinking {len(entry_points)} commands into {BIN_DIR}")
        if not self.confirm(f"link them into {BIN_DIR}?"):
            return Outcome(None, f"skipped; run with `uv run` or add {bin_dir} to PATH")

        BIN_DIR.mkdir(parents=True, exist_ok=True)
        for path in entry_points:
            link = BIN_DIR / path.name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(path)

        on_path = str(BIN_DIR) in os.environ.get("PATH", "").split(os.pathsep)
        if not on_path:
            return Outcome(True, f"linked, but {BIN_DIR} is not on your PATH — add it")
        return Outcome(True, f"linked into {BIN_DIR}")

    def install_completion(self) -> Outcome:
        source = _repo_root() / "scripts/vinowhisper-completion.bash"
        if not source.is_file():
            return Outcome(None, "completion script not found (installed from a wheel?)")
        if not self.confirm(f"install bash completion into {COMPLETION_DIR}?"):
            return Outcome(None, "skipped")

        COMPLETION_DIR.mkdir(parents=True, exist_ok=True)
        for name in COMMANDS:
            link = COMPLETION_DIR / f"vinowhisper-{name}"
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(source)
        return Outcome(True, f"completion installed for {len(COMMANDS)} commands")

    def install_overlay(self) -> Outcome:
        path = overlay.installed(BIN_DIR)
        if path is not None:
            self.say(f"  Found {path}")
        else:
            got = self._get_overlay()
            if isinstance(got, Outcome):
                return got
            path = got

        if path.is_relative_to(Path.home()):
            self.run([str(path), "--install"], "add a launcher entry for it?")
        self.run([str(path), "--autostart"], "start it in the tray at login?")
        return Outcome(True, f"{path} (open it from the app menu, or run vinowhisper-gui)")

    def _get_overlay(self) -> Path | Outcome:
        available = overlay.availability()
        if isinstance(available, overlay.Pin):
            self.say(f"  Download {available.url}")
            self.say(f"  sha256   {available.sha256} (pinned in vinowhisper {available.version})")
            if not self.confirm(f"install it into {BIN_DIR}?"):
                return Outcome(None, "skipped; `vinowhisper-setup --gui` installs it any time")
            try:
                return overlay.fetch(available, BIN_DIR / overlay.BINARY)
            except overlay.OverlayError as exc:
                return Outcome(False, str(exc))

        if overlay.can_build():
            if not self.run(overlay.build_argv(), "build it with cargo? (minutes, the first time)"):
                return Outcome(None, "not built; `vinowhisper-setup --gui` builds it any time")
            return overlay.install_binary(overlay.built_binary(), BIN_DIR / overlay.BINARY)

        return Outcome(None, available)

    def run_all(self) -> int:
        self.say(f"vinowhisper-setup {__version__}")
        self.say(f"  distro:  {self.distro}")
        self.say(f"  python:  {sys.executable}")
        if self.dry_run:
            self.say("\n  --dry-run: nothing will be changed; every command is printed.")

        self.step("Python version", self.check_python)
        self.step("Audio capture", self.check_audio)
        self.step("Inference device", self.check_device)
        self.step("Model export", self.check_model)
        self.step("Systemd units", self.install_units)
        self.step("Commands on PATH", self.link_binaries)
        self.step("Bash completion", self.install_completion)
        if os.environ.get("WAYLAND_DISPLAY"):
            self.step("Caption overlay (optional)", self.install_overlay, optional=True)

        self.say("")
        if self.failed:
            self.say(f"✗ {len(self.failed)} step(s) failed: {', '.join(self.failed)}")
            self.say("  vinowhisper-doctor has the detail.")
            return 1
        if self.skipped:
            self.say(f"… {len(self.skipped)} step(s) left undone: {', '.join(self.skipped)}")
            self.say("  Re-run vinowhisper-setup once you have dealt with them.")
            return 0
        self.say("✓ Ready. Run `vinowhisper-caption` with something playing.")
        return 0

    def run_overlay(self) -> int:
        self.say(f"vinowhisper-setup {__version__}: the caption overlay")
        if self.dry_run:
            self.say("\n  --dry-run: nothing will be changed; every command is printed.")
        self.step("Caption overlay", self.install_overlay)
        return 1 if self.failed else 0


def export_argv(variant: str, directory: Path | None = None) -> list[str]:
    if variant not in ("npu", "stateful"):
        raise ValueError(f"variant must be 'npu' or 'stateful', got {variant!r}")
    out = directory or (config.MODEL_DIR if variant == "npu" else config.STATEFUL_MODEL_DIR)
    argv = [
        "optimum-cli",
        "export",
        "openvino",
        "--model",
        config.MODEL_ID,
        "--task",
        EXPORT_TASK,
    ]
    if variant == "npu":
        argv.append("--disable-stateful")
    argv.append(str(out))
    return argv


def optimum_cli() -> str | None:
    beside = Path(sys.executable).with_name("optimum-cli")
    if beside.exists():
        return str(beside)
    return shutil.which("optimum-cli")


def _has_pip() -> bool:
    return importlib.util.find_spec("pip") is not None


def export_extra_argv() -> list[str]:
    uv = shutil.which("uv")
    # A checkout is an editable install; pip installing from PyPI would replace it.
    if config.CONVERT_SCRIPT.is_file() and uv:
        return [uv, "sync", "--extra", "export", "--project", str(_repo_root())]
    requirement = f"vinowhisper[export]=={__version__}"
    if not _has_pip() and uv:
        return [uv, "pip", "install", "--python", sys.executable, requirement]
    return [sys.executable, "-m", "pip", "install", requirement]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _has_systemd() -> bool:
    return Path("/run/systemd/system").exists()


def _exec_start(device: str = "auto") -> str:
    script = Path(sys.executable).with_name("vinowhisper-server")
    if script.exists():
        return f"{script} --device {device}"
    return f"{sys.executable} -m vinowhisper.server --device {device}"


def unit_files(device: str = "auto") -> tuple[str, str]:
    service = f"""\
[Unit]
Description=vinoWhisper transcription server
Documentation=https://github.com/karanshukla/vinoWhisper
Requires=vinowhisper-server.socket

[Service]
ExecStart={_exec_start(device)}
Restart=on-failure
RestartSec=2

# Generated by vinowhisper-setup {__version__}. Re-run it after moving the
# checkout or switching Python environments.
#
# No [Install]/WantedBy: this unit is socket-activated (see
# vinowhisper-server.socket), not enabled or started directly. Systemd starts
# it on the first connection and it self-exits after config.IDLE_TIMEOUT_S
# idle; that clean exit(0) is not a failure, and Restart=on-failure only
# covers real crashes.
"""
    socket = f"""\
[Unit]
Description=vinoWhisper transcription server socket
Documentation=https://github.com/karanshukla/vinoWhisper

[Socket]
ListenStream={config.SERVER_HOST}:{config.SERVER_PORT}
Accept=no

[Install]
WantedBy=sockets.target
"""
    return service, socket


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Guided setup: capture tools, NPU driver, model export, systemd units.",
        epilog="Every step is idempotent; re-run it as often as you like.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Answer yes to every prompt, including the ones that run sudo.",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Print the plan and every command it would run, change nothing.",
    )
    parser.add_argument(
        "--device",
        default=config.DEFAULT_DEVICE,
        metavar="NPU|GPU|CPU|auto",
        help="Device to set up for. Decides which model export is needed and "
        "what the generated systemd unit passes to the server.",
    )
    parser.add_argument(
        "--print-units",
        action="store_true",
        help="Print the systemd units that would be generated, then exit.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Install only the optional caption overlay (vinowhisper-gui): the "
        "release binary, checked against the sha256 pinned in this package, or a "
        "cargo build from a checkout.",
    )
    parser.add_argument("--version", action="version", version=f"vinowhisper {__version__}")
    args = parser.parse_args(argv)

    if args.print_units:
        service, socket = unit_files(args.device)
        print(f"# {UNIT_DIR}/vinowhisper-server.service\n{service}")
        print(f"# {UNIT_DIR}/vinowhisper-server.socket\n{socket}")
        return 0

    if args.yes and args.dry_run:
        print("--yes and --dry-run contradict each other", file=sys.stderr)
        return 2

    wizard = Wizard(assume_yes=args.yes, dry_run=args.dry_run, device=args.device)
    try:
        return wizard.run_overlay() if args.gui else wizard.run_all()
    except KeyboardInterrupt:
        print("\ninterrupted; nothing further was changed", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
