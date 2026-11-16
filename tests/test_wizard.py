"""The guided install: the export commands, and the server step that replaced the units."""

import sys

import pytest
from conftest import fake_version

from vinowhisper import capture, config, devices, distro, integrity, wizard


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


def _units(tmp_path, monkeypatch, device="CPU"):
    units = tmp_path / "systemd/user"
    units.mkdir(parents=True)
    (units / "vinowhisper-server.service").write_text(
        f"[Service]\nExecStart=/venv/bin/vinowhisper-server --device {device}\n"
    )
    (units / "vinowhisper-server.socket").write_text("[Socket]\n")
    monkeypatch.setattr(wizard.launch, "unit_dirs", lambda: [units])
    return units


def test_the_server_step_installs_nothing_and_stops_a_running_server(monkeypatch):
    stopped = []
    monkeypatch.setattr(wizard.launch, "stop", lambda: stopped.append(True) or True)
    outcome = wizard.Wizard(assume_yes=True).setup_server()
    assert outcome.ok
    assert "starts on first use" in outcome.summary
    assert stopped == [True]
    assert not config.SERVER_FILE.exists()


def test_the_server_step_removes_units_an_earlier_version_installed(monkeypatch, tmp_path):
    """The unit's device moves to server.json, which is where the server reads it now."""
    units = _units(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(wizard.launch, "_systemctl", lambda *args: calls.append(args) or True)
    monkeypatch.setattr(wizard.launch, "stop", lambda: True)
    outcome = wizard.Wizard(assume_yes=True).setup_server()
    assert outcome.ok and "old systemd units removed" in outcome.summary
    assert not list(units.iterdir())
    assert calls[0] == ("disable", "--now", *wizard.launch.UNITS)
    assert ("daemon-reload",) in calls
    assert config.load_device() == "CPU"


def test_dry_run_leaves_the_old_units(monkeypatch, tmp_path):
    units = _units(tmp_path, monkeypatch)
    monkeypatch.setattr(wizard.launch, "stop", lambda: pytest.fail("stopped the server"))
    outcome = wizard.Wizard(dry_run=True).setup_server()
    assert outcome.ok is None
    assert len(list(units.iterdir())) == 2


def test_an_explicit_device_is_saved_and_a_plain_run_keeps_it(monkeypatch):
    monkeypatch.setattr(wizard.launch, "stop", lambda: True)
    assert wizard.Wizard(assume_yes=True, device="GPU").setup_server().ok
    assert config.load_device() == "GPU"
    again = wizard.Wizard(assume_yes=True)
    assert again.device == "GPU"
    assert again.setup_server().ok
    assert config.load_device() == "GPU"


def test_a_server_that_will_not_stop_fails_the_step(monkeypatch):
    monkeypatch.setattr(wizard.launch, "stop", lambda: False)
    outcome = wizard.Wizard(assume_yes=True).setup_server()
    assert outcome.ok is False
    assert "vinowhisper-server --stop" in outcome.summary


STEPS = (
    "check_python",
    "check_audio",
    "check_device",
    "check_model",
    "save_language",
    "setup_server",
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
    _steps(monkeypatch, setup_server=None)
    wiz = wizard.Wizard(assume_yes=True)
    assert wiz.run_all() == 0
    assert wiz.skipped == ["Server"]
    assert "left undone" in capsys.readouterr().out


def test_the_overlay_is_skipped_on_gnome(monkeypatch):
    """No layer-shell: the tray would exit at start, and its autostart fail every login."""
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    monkeypatch.setattr(wizard.overlay, "installed", lambda *a: pytest.fail("looked for the tray"))
    outcome = wizard.Wizard(assume_yes=True).install_overlay()
    assert outcome.ok is None
    assert "docs/tray.md" in outcome.summary


def test_run_all_fails_when_any_step_fails(monkeypatch, capsys):
    _steps(monkeypatch, check_model=False, setup_server=None)
    wiz = wizard.Wizard(assume_yes=True)
    assert wiz.run_all() == 1
    assert wiz.failed == ["Model export"]
    assert "vinowhisper-doctor" in capsys.readouterr().out


def test_the_file_decoder_step_installs_ffmpeg_or_is_skipped_without_failing(monkeypatch):
    instance = wizard.Wizard()
    instance.distro = distro.Distro(id="fedora", name="Fedora", version="44", family="fedora")
    ran = []
    monkeypatch.setattr(wizard.shutil, "which", lambda name: None)
    monkeypatch.setattr(instance, "confirm", lambda prompt: True)
    monkeypatch.setattr(instance, "execute", lambda argv, env=None: ran.append(argv) or True)
    assert instance.check_decoder().ok is True
    assert ran == [["sudo", "dnf", "install", "-y", "ffmpeg-free"]]

    # Optional, so an unattended --yes names the package and does not sudo for it.
    unattended = wizard.Wizard(assume_yes=True)
    unattended.distro = instance.distro
    monkeypatch.setattr(unattended, "execute", lambda argv, env=None: ran.append(argv) or True)
    outcome = unattended.check_decoder()
    assert outcome.ok is None and "ffmpeg-free" in outcome.summary
    assert len(ran) == 1

    declined = wizard.Wizard(dry_run=True)
    declined.distro = instance.distro
    outcome = declined.check_decoder()
    assert outcome.ok is None and "ffmpeg-free" in outcome.summary

    monkeypatch.setattr(wizard.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert instance.check_decoder().ok is True
    assert len(ran) == 1


def _hw(kind: str, vendor: str) -> devices.Hardware:
    return devices.Hardware(slot="0000:00:0b.0", kind=kind, vendor=vendor, pci_id="0000:0000")


def test_an_intel_npu_or_an_unreadable_bus_still_gets_the_driver_offer():
    assert wizard.npu_without_a_driver([_hw("NPU", "Intel")]) is None
    assert wizard.npu_without_a_driver([]) is None


def test_a_non_intel_npu_or_no_npu_at_all_gets_no_driver_offer():
    amd = wizard.npu_without_a_driver([_hw("NPU", "AMD")])
    assert amd is not None and "cannot drive a non-Intel NPU" in amd[0]
    assert wizard.npu_without_a_driver([_hw("GPU", "Intel")]) == [
        "No NPU on the PCI bus, so there is no NPU driver to install."
    ]


def test_the_export_plan_names_the_pinned_revision_and_the_command_before_anything_runs():
    source = wizard.model_source.Source("openai/whisper-small.en", "e8727524f962abcdef")
    plan = wizard.export_plan(source, ["optimum-cli", "export", "openvino"])
    assert "openai/whisper-small.en@e8727524f962 " in plan[1]
    assert plan[-1].endswith("optimum-cli export openvino")
    assert "TMPDIR=" in plan[-1]


def test_the_download_progress_speaks_only_when_a_new_tenth_is_reached():
    assert wizard.progress_percent(5, 100, -1) == 5
    assert wizard.progress_percent(7, 100, 5) is None
    assert wizard.progress_percent(10, 100, 5) == 10


def test_the_download_progress_always_reports_the_last_byte():
    assert wizard.progress_percent(100, 100, 99) == 100
    assert wizard.progress_percent(0, 0, 0) == 0


def test_an_ovfetch_earlier_on_path_is_named_and_a_cargo_one_gets_the_uninstall_hint():
    installed = wizard.BIN_DIR / "ovfetch"
    cargo = wizard.ovfetch_shadow_note(installed, "/home/u/.cargo/bin/ovfetch")
    assert cargo is not None and cargo.endswith("`cargo uninstall ovfetch`")
    other = wizard.ovfetch_shadow_note(installed, "/usr/bin/ovfetch")
    assert other is not None and f"put {wizard.BIN_DIR} ahead of it on PATH" in other
    assert "the older /usr/bin/ovfetch comes first on PATH" in other


def test_no_shadow_note_when_ovfetch_is_absent_from_path_or_is_the_installed_one():
    installed = wizard.BIN_DIR / "ovfetch"
    assert wizard.ovfetch_shadow_note(installed, None) is None
    assert wizard.ovfetch_shadow_note(installed, str(installed)) is None
