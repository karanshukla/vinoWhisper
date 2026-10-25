"""The guided install: the export commands and the generated units.

The unit generation is the part worth pinning down. The old checked-in service
file hardcoded ~/Development/vinoWhisper, which worked on exactly one machine;
these assertions are what stop that regressing.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import fake_version

from vinowhisper import capture, config, distro, integrity, wizard


def test_the_npu_export_disables_stateful():
    """Its static pipeline needs the separate decoder_with_past submodel."""
    argv = wizard.export_argv("npu")
    assert argv[:3] == ["optimum-cli", "export", "openvino"]
    assert "--disable-stateful" in argv
    assert argv[-1] == str(config.MODEL_DIR)


def test_the_stateful_export_does_not():
    """That same export fails on CPU with a beam_idx port error."""
    argv = wizard.export_argv("stateful")
    assert "--disable-stateful" not in argv
    assert argv[-1] == str(config.STATEFUL_MODEL_DIR)


def test_both_exports_agree_on_the_task_and_the_model():
    npu, stateful = wizard.export_argv("npu"), wizard.export_argv("stateful")
    for argv in (npu, stateful):
        assert argv[argv.index("--task") + 1] == wizard.EXPORT_TASK
        assert argv[argv.index("--model") + 1] == config.MODEL_ID


def test_an_unknown_variant_is_rejected():
    with pytest.raises(ValueError, match="npu"):
        wizard.export_argv("int8")


def test_the_generated_service_points_into_this_environment():
    service, _ = wizard.unit_files()
    exec_start = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    assert str(sys.executable) in exec_start or "vinowhisper-server" in exec_start
    # The old checked-in unit hardcoded this literal, unexpanded specifier
    # path regardless of where the repo actually lived. A dynamically
    # generated ExecStart legitimately contains "Development/vinoWhisper" as
    # a substring on any machine whose checkout happens to live there, so
    # this must check for the literal old string, not that substring.
    assert "%h/Development/vinoWhisper" not in service


def test_the_service_is_socket_activated_only():
    """No [Install]: it is started by the socket unit, never enabled directly."""
    service, socket = wizard.unit_files()
    # A section header, not the comment explaining why there isn't one.
    assert not any(line.strip() == "[Install]" for line in service.splitlines())
    assert "Requires=vinowhisper-server.socket" in service
    assert any(line.strip() == "[Install]" for line in socket.splitlines())
    assert f"ListenStream=%t/{config.SERVER_SOCKET_RELPATH}" in socket


def test_the_socket_is_private_to_the_user():
    """Not a TCP port, which any local user could connect to, or bind first."""
    _, socket = wizard.unit_files()
    listen = [line for line in socket.splitlines() if line.startswith("ListenStream=")]
    assert listen == ["ListenStream=%t/vinowhisper/server.sock"]
    assert "SocketMode=0600" in socket.splitlines()
    assert "DirectoryMode=0700" in socket.splitlines()


def test_the_reference_socket_unit_matches_the_generated_one():
    _, generated = wizard.unit_files()
    reference = (Path(__file__).parent.parent / "systemd/vinowhisper-server.socket").read_text()

    def directives(text):
        return [line for line in text.splitlines() if line and line[0] not in ";#"]

    assert directives(reference) == directives(generated)


def test_reinstalling_rebinds_the_socket(monkeypatch, tmp_path):
    """An existing install's socket keeps its old ListenStream until it is restarted."""
    calls = []
    monkeypatch.setattr(wizard, "UNIT_DIR", tmp_path)
    monkeypatch.setattr(wizard, "_has_systemd", lambda: True)
    monkeypatch.setattr(
        wizard.subprocess,
        "run",
        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0),
    )
    outcome = wizard.Wizard(assume_yes=True).install_units()
    assert outcome.ok
    assert "ListenStream=%t/" in (tmp_path / "vinowhisper-server.socket").read_text()
    systemctl = [argv[2:] for argv in calls]
    assert systemctl == [
        ["daemon-reload"],
        ["stop", "vinowhisper-server.service"],
        ["reset-failed", "vinowhisper-server.service", "vinowhisper-server.socket"],
        ["enable", "vinowhisper-server.socket"],
        ["restart", "vinowhisper-server.socket"],
    ]


def test_the_device_choice_reaches_the_unit():
    service, _ = wizard.unit_files(device="CPU")
    assert "--device CPU" in service


def test_print_units_writes_both_and_changes_nothing(capsys):
    assert wizard.main(["--print-units"]) == 0
    output = capsys.readouterr().out
    assert "vinowhisper-server.service" in output
    assert "vinowhisper-server.socket" in output


def test_yes_and_dry_run_contradict_each_other():
    assert wizard.main(["--yes", "--dry-run"]) == 2


def test_dry_run_never_confirms_anything():
    dry = wizard.Wizard(dry_run=True)
    assert dry.confirm("do the thing?") is False
    assert dry.run(["false"], "run it?") is False


def test_assume_yes_confirms_without_a_terminal():
    assert wizard.Wizard(assume_yes=True).confirm("do the thing?") is True


def test_a_step_that_fails_is_tallied(capsys):
    instance = wizard.Wizard(dry_run=True)
    instance.step("Broken thing", lambda: wizard.Outcome(False, "it did not work"))
    instance.step("Deferred thing", lambda: wizard.Outcome(None, "left for you"))
    instance.step("Fine thing", lambda: wizard.Outcome(True, "done"))
    assert instance.failed == ["Broken thing"]
    assert instance.skipped == ["Deferred thing"]
    assert "✗ it did not work" in capsys.readouterr().out


def test_a_layered_capture_tool_waits_for_a_reboot(monkeypatch):
    instance = wizard.Wizard(assume_yes=True)
    instance.distro = distro.Distro(
        id="fedora", name="Silverblue", version="45", family="fedora", image="ostree"
    )
    ran = []
    monkeypatch.setattr(capture, "available_backends", lambda: [])
    monkeypatch.setattr(instance, "execute", lambda argv, env=None: ran.append(argv) or True)
    outcome = instance.check_audio()
    assert ran == [["sudo", "rpm-ostree", "install", "--idempotent", "pipewire-utils"]]
    assert outcome.ok is None and "reboot" in outcome.summary


def test_python_314_is_reported_as_unusable(monkeypatch):
    """It made functools.partial a descriptor, which breaks optimum's export."""
    monkeypatch.setattr(sys, "version_info", fake_version(3, 14, 0))
    outcome = wizard.Wizard(dry_run=True).check_python()
    assert outcome.ok is False
    assert "3.13" in outcome.summary


# --- Which export command this install should name -----------------------
#
# scripts/convert_model.sh ships in the git checkout and not in the wheel, so
# an error message that names it unconditionally sends a pip user to a file
# they do not have.


def test_a_checkout_names_the_script(monkeypatch, tmp_path):
    script = tmp_path / "convert_model.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(config, "CONVERT_SCRIPT", script)
    assert config.export_command("npu") == "./scripts/convert_model.sh --variant npu"
    assert config.export_command("stateful").endswith("--variant stateful")


def test_a_wheel_install_names_the_wizard_instead(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONVERT_SCRIPT", tmp_path / "not-here.sh")
    # No --variant: the wizard picks it from the device it finds, so naming a
    # flag it does not take would be worse than naming none.
    assert config.export_command("npu") == "vinowhisper-setup"
    assert config.export_command("stateful") == "vinowhisper-setup"


# --- digest verification --------------------------------------------------


def _wizard():
    return wizard.Wizard(dry_run=True)


def _verification(status, **kwargs):
    return integrity.Verification(
        status=status,
        model_id=config.MODEL_ID,
        variant="npu",
        directory=config.MODEL_DIR,
        **kwargs,
    )


@pytest.mark.parametrize(
    "status, expected_ok",
    [
        (integrity.VERIFIED, True),
        (integrity.UNPINNED, True),
        (integrity.DRIFT, True),
        (integrity.MISMATCH, False),
        (integrity.INCOMPLETE, False),
        (integrity.KNOWN_BAD, False),
    ],
)
def test_setup_only_fails_on_a_severe_digest_result(monkeypatch, capsys, status, expected_ok):
    """characterization: an unpinned or drifted export does not block setup.

    Only three of the six statuses mean the export should not be used. The
    other three are the normal state of an export nobody has pinned, or of a
    toolchain that has moved since the pin was made, and failing setup on
    either would make `--model` anything unusable. If this goes red, the
    question is whether setup was meant to start refusing to finish.
    """
    monkeypatch.setattr(
        integrity, "verify", lambda directory, variant, *args, **kwargs: _verification(status)
    )
    outcome = _wizard().check_digests("npu", config.MODEL_DIR, "export present")
    assert outcome.ok is expected_ok
    # Whatever it decides, it says why: a silent pass on a drifted export is
    # the same as not checking.
    printed = capsys.readouterr().out
    assert (status == integrity.VERIFIED) == (printed.strip() == "")


def _directives(unit: str) -> list[str]:
    return [
        line
        for line in unit.splitlines()
        if line and not line.startswith(("#", ";", "ExecStart=", "Documentation="))
    ]


def test_the_reference_service_matches_the_generated_one():
    """The checked-in copy is what people read and hand-install; keep it honest."""
    reference = Path(__file__).resolve().parent.parent / "systemd/vinowhisper-server.service"
    service, _ = wizard.unit_files()
    assert _directives(reference.read_text(encoding="utf-8")) == _directives(service)


def test_characterization_the_service_hardening_leaves_the_accelerators_reachable():
    """characterization: the hardening stops short of the obvious next steps.

    MemoryDenyWriteExecute kills OpenVINO's CPU plugin, which JIT-compiles its
    kernels. PrivateDevices hides /dev/accel and /dev/dri, so the NPU and GPU
    vanish and "auto" quietly lands on the CPU. ProtectClock and the other
    namespace settings need PrivateUsers in a user unit, which needs
    unprivileged user namespaces, which not every distro allows. Adding any of
    them is a hardware-tested change, not a tidy-up.
    """
    service, _ = wizard.unit_files()
    directives = _directives(service)
    assert "NoNewPrivileges=yes" in directives
    assert "UMask=0077" in directives
    for absent in ("MemoryDenyWriteExecute", "PrivateDevices", "ProtectClock", "PrivateUsers"):
        assert not any(line.startswith(f"{absent}=") for line in directives)


@pytest.fixture
def venv_bin(monkeypatch, tmp_path):
    """A venv's bin/ holding every entry point, and an empty ~/.local/bin."""
    bin_dir = tmp_path / "venv/bin"
    bin_dir.mkdir(parents=True)
    for name in wizard.COMMANDS:
        (bin_dir / f"vinowhisper-{name}").write_text("#!/bin/sh\n")
    monkeypatch.setattr(wizard.sys, "executable", str(bin_dir / "python"))
    monkeypatch.setattr(wizard, "BIN_DIR", tmp_path / "local/bin")
    monkeypatch.setenv("PATH", str(tmp_path / "local/bin"))
    return bin_dir


def test_linking_points_every_command_at_the_venv(venv_bin):
    outcome = wizard.Wizard(assume_yes=True).link_binaries()
    assert outcome.ok
    for name in wizard.COMMANDS:
        link = wizard.BIN_DIR / f"vinowhisper-{name}"
        assert link.is_symlink()
        assert link.resolve() == (venv_bin / f"vinowhisper-{name}").resolve()


def test_linking_replaces_a_stale_link_and_an_old_script(venv_bin):
    # A previous checkout's link, and a `pip install --user` entry point: both are ours.
    wizard.BIN_DIR.mkdir(parents=True)
    (wizard.BIN_DIR / "vinowhisper-caption").symlink_to("/nonexistent/vinowhisper-caption")
    (wizard.BIN_DIR / "vinowhisper-doctor").write_text("#!/bin/sh\nold\n")
    assert wizard.Wizard(assume_yes=True).link_binaries().ok
    for name in ("caption", "doctor"):
        link = wizard.BIN_DIR / f"vinowhisper-{name}"
        assert link.resolve() == (venv_bin / f"vinowhisper-{name}").resolve()


def test_linking_says_when_the_directory_is_not_on_path(venv_bin, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    outcome = wizard.Wizard(assume_yes=True).link_binaries()
    assert outcome.ok
    assert "not on your PATH" in outcome.summary


def test_a_dry_run_links_nothing(venv_bin):
    outcome = wizard.Wizard(dry_run=True).link_binaries()
    assert outcome.ok is None
    assert not wizard.BIN_DIR.exists()


def test_linking_names_a_missing_entry_point(venv_bin):
    (venv_bin / "vinowhisper-replay").unlink()
    outcome = wizard.Wizard(assume_yes=True).link_binaries()
    assert outcome.ok is False
    assert "vinowhisper-replay" in outcome.summary
    assert "uv sync" in outcome.summary
    assert not wizard.BIN_DIR.exists()


def test_completion_links_each_command_to_the_one_script(monkeypatch, tmp_path):
    monkeypatch.setattr(wizard, "COMPLETION_DIR", tmp_path / "completions")
    tmp_path.joinpath("completions").mkdir()
    (tmp_path / "completions/vinowhisper-caption").write_text("stale")
    assert wizard.Wizard(dry_run=True).install_completion().ok is None
    assert (tmp_path / "completions/vinowhisper-caption").read_text() == "stale"
    assert wizard.Wizard(assume_yes=True).install_completion().ok
    source = wizard._repo_root() / "scripts/vinowhisper-completion.bash"
    for name in wizard.COMMANDS:
        assert (tmp_path / f"completions/vinowhisper-{name}").resolve() == source.resolve()


def test_completion_from_a_wheel_is_skipped_not_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(wizard, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(wizard, "COMPLETION_DIR", tmp_path / "completions")
    outcome = wizard.Wizard(assume_yes=True).install_completion()
    assert outcome.ok is None
    assert "wheel" in outcome.summary
    assert not (tmp_path / "completions").exists()


@pytest.mark.parametrize("failing", ["enable", "restart"])
def test_a_failed_systemctl_fails_the_step_after_writing_the_units(monkeypatch, tmp_path, failing):
    monkeypatch.setattr(wizard, "UNIT_DIR", tmp_path)
    monkeypatch.setattr(wizard, "_has_systemd", lambda: True)
    monkeypatch.setattr(
        wizard.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, int(argv[2] == failing)),
    )
    outcome = wizard.Wizard(assume_yes=True).install_units()
    assert outcome.ok is False
    assert f"systemctl --user {failing}` failed" in outcome.summary
    assert (tmp_path / "vinowhisper-server.service").is_file()
    assert (tmp_path / "vinowhisper-server.socket").is_file()


def test_no_systemd_skips_the_units(monkeypatch, tmp_path):
    monkeypatch.setattr(wizard, "UNIT_DIR", tmp_path / "units")
    monkeypatch.setattr(wizard, "_has_systemd", lambda: False)
    outcome = wizard.Wizard(assume_yes=True).install_units()
    assert outcome.ok is None
    assert "vinowhisper-server" in outcome.summary
    assert not (tmp_path / "units").exists()


STEPS = (
    "check_python",
    "check_audio",
    "check_device",
    "check_model",
    "save_language",
    "install_units",
    "link_binaries",
    "install_completion",
)


def _steps(monkeypatch, **results):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(wizard.devices, "hardware", lambda: [])
    for name in STEPS:
        ok = results.get(name, True)
        monkeypatch.setattr(
            wizard.Wizard, name, lambda self, ok=ok, name=name: wizard.Outcome(ok, name)
        )


def test_run_all_is_ready_when_every_step_passes(monkeypatch, capsys):
    _steps(monkeypatch)
    assert wizard.Wizard(assume_yes=True).run_all() == 0
    assert "Ready." in capsys.readouterr().out


def test_run_all_succeeds_but_lists_skipped_steps(monkeypatch, capsys):
    _steps(monkeypatch, install_units=None)
    wiz = wizard.Wizard(assume_yes=True)
    assert wiz.run_all() == 0
    assert wiz.skipped == ["Systemd units"]
    assert "left undone" in capsys.readouterr().out


def test_run_all_fails_when_any_step_fails(monkeypatch, capsys):
    _steps(monkeypatch, check_model=False, install_units=None)
    wiz = wizard.Wizard(assume_yes=True)
    assert wiz.run_all() == 1
    assert wiz.failed == ["Model export"]
    assert "vinowhisper-doctor" in capsys.readouterr().out
