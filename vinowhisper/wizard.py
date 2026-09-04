"""`vinowhisper-setup` — the guided install.

Everything this tool needs beyond `uv sync` used to be a numbered list in the
README that only worked on one machine: install these packages (Fedora names),
export the model with these flags, copy these systemd units (with a hardcoded
`%h/Development/vinoWhisper` path in them), symlink four binaries. This walks
the same list, but it checks each step first, phrases the fix in the local
distro's package names, and generates the unit files against the paths that
actually exist here.

Three rules it sticks to:

1. **Nothing runs without a yes.** Every command is printed before it is run,
   including — especially — the ones with `sudo` in them. `--yes` answers yes
   to all of it, `--dry-run` answers no to all of it and just shows the plan.
2. **A step that is already done says so and is skipped.** Re-running this
   after fixing one thing must not re-do the other five.
3. **It never pretends.** Where a distro has no package for something (the NPU
   userspace driver, on most of them), it says so and points at Intel's
   releases rather than inventing a command that will fail.

The wizard is not required: every step is a shell command you can run yourself,
and `--dry-run` prints all of them in order for exactly that reason.
"""

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import __version__, capture, config, devices, distro, integrity

BIN_DIR = Path.home() / ".local/bin"
UNIT_DIR = Path.home() / ".config/systemd/user"
COMPLETION_DIR = Path.home() / ".local/share/bash-completion/completions"
COMMANDS = ("caption", "server", "replay", "doctor", "setup")

# Kept in step with scripts/convert_model.sh, which is the same export as a
# standalone shell command for people who never run the wizard. If you change
# the flags in one, change them in the other.
EXPORT_TASK = "automatic-speech-recognition-with-past"


@dataclass
class Outcome:
    """What a step decided. `ok=None` means 'left for the user to do'."""

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

    # --- plumbing --------------------------------------------------------

    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def confirm(self, prompt: str) -> bool:
        if self.dry_run:
            return False
        if self.assume_yes:
            return True
        if not sys.stdin.isatty():
            # Piped input with no --yes: proceeding would be running unattended
            # sudo commands nobody agreed to.
            self.say("  (not a terminal, and no --yes — skipping)")
            return False
        try:
            answer = input(f"  {prompt} [y/N] ").strip().lower()
        except EOFError:
            return False
        return answer in ("y", "yes")

    def run(self, argv: list[str], why: str) -> bool:
        """Print a command, ask, run it. Returns whether it ran successfully."""
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

    def step(self, title: str, action: Callable[[], Outcome]) -> None:
        self.say(f"\n── {title}")
        outcome = action()
        marker = {True: "✓", False: "✗", None: "…"}[outcome.ok]
        self.say(f"  {marker} {outcome.summary}")
        if outcome.ok is False:
            self.failed.append(title)
        elif outcome.ok is None:
            self.skipped.append(title)

    # --- steps -----------------------------------------------------------

    def check_python(self) -> Outcome:
        version = sys.version_info
        text = f"{version.major}.{version.minor}.{version.micro}"
        if version[:2] == (3, 14):
            # Version-independent root cause, not a package-pairing issue:
            # 3.14 made functools.partial a descriptor, which breaks optimum's
            # `NORMALIZED_CONFIG_CLASS = SomeConfig.with_args(...)` idiom.
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

        # The NPU is the whole point of the tool, so a fallback gets the full
        # kernel-node/permissions/driver walk rather than a one-line warning.
        for warning in selection.warnings:
            self.say(f"  ⚠ {warning}")
        if not any(device.kind == "NPU" for device in inventory):
            self.say("")
            for line in devices.npu_missing_help(self.distro):
                self.say(f"  {line}")
            command = distro.install_command(distro.NPU_DRIVER, self.distro)
            if command and self.run(command.split(), "install the NPU driver?"):
                return Outcome(None, "NPU driver installed — reboot, then re-run this")
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
        self.say("  This downloads ~1GB from Hugging Face and takes a few minutes.")
        if self.run(export_argv(variant, directory), "export it now?"):
            return self.check_digests(variant, directory, f"exported to {directory}")
        return Outcome(None, f"run {config.export_command(variant)} when ready")

    def check_digests(self, variant: str, directory: Path, summary: str) -> Outcome:
        """Compare what is on disk against the pinned digests.

        Runs on an export that was already there as well as one this wizard
        just produced, because the interesting question is what is about to be
        loaded onto your hardware, not who downloaded it. ~1.2s for 1.5GB.
        """
        result = integrity.verify(directory, variant)
        if result.status == integrity.VERIFIED:
            return Outcome(True, f"{summary}, digests verified")
        for line in result.lines()[1:]:
            self.say(f"  {line.strip()}")
        if result.severe:
            return Outcome(False, f"{summary}, but {result.summary()}")
        # UNPINNED and DRIFT both warn and continue: an export nobody has
        # pinned is the normal state for --model anything, and a toolchain bump
        # legitimately changes the bytes. Neither is a reason to block setup.
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
        # The socket, never the service: the service has no [Install] section
        # because it is only ever meant to be started by the socket, and this
        # is the whole scale-to-zero design (see docs/architecture.md).
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
        # Symlinks rather than copies, deliberately: `uv sync` installs the
        # project editable with an absolute shebang, so these track edits to
        # the checkout instead of freezing a snapshot.
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
        # bash-completion loads a file from that directory lazily, on first Tab
        # against a command of the same name, hence one link per command.
        for name in COMMANDS:
            link = COMPLETION_DIR / f"vinowhisper-{name}"
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(source)
        return Outcome(True, f"completion installed for {len(COMMANDS)} commands")

    # --- driver ----------------------------------------------------------

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


def export_argv(variant: str, directory: Path | None = None) -> list[str]:
    """The optimum-cli export for one device class.

    --disable-stateful is required for NPU: its static pipeline needs the
    separate KV-cache `decoder_with_past` submodel that the default stateful
    export doesn't produce (self_attn_nodes assertion otherwise — see
    openvinotoolkit/openvino.genai#1728). That same export then cannot run on
    CPU at all, which is why there are two of them.
    """
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _has_systemd() -> bool:
    return Path("/run/systemd/system").exists()


def _exec_start(device: str = "auto") -> str:
    """Prefer the installed console script; fall back to `python -m`.

    Either way it is an absolute path into the environment this wizard is
    running from, which is the bug the old checked-in unit file had: it
    hardcoded ~/Development/vinoWhisper and worked on exactly one machine.
    """
    script = Path(sys.executable).with_name("vinowhisper-server")
    if script.exists():
        return f"{script} --device {device}"
    return f"{sys.executable} -m vinowhisper.server --device {device}"


def unit_files(device: str = "auto") -> tuple[str, str]:
    """(service, socket) unit text, generated against this machine's paths."""
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
        return wizard.run_all()
    except KeyboardInterrupt:
        print("\ninterrupted; nothing further was changed", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
