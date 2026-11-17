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


# --- transcripts ------------------------------------------------------------
#
# Each of these runs one method of the wizard against fakes and asserts everything it said,
# asked (with the answer given), and would have run, downloaded or written, in order.


class _Transcript:
    def __init__(self, tmp_path):
        self.log = []
        self.answers = True
        self.exec_ok = True
        self.tmp = str(tmp_path)

    def _norm(self, value):
        if isinstance(value, (list, tuple)):
            return type(value)(self._norm(item) for item in value)
        if isinstance(value, dict):
            return {key: self._norm(item) for key, item in value.items()}
        if value is None or isinstance(value, (bool, int)):
            return value
        return str(value).replace(self.tmp, "<tmp>")

    def add(self, *entry):
        self.log.append(entry)

    def play(self, method):
        outcome = method()
        self.add("outcome", outcome.ok, outcome.summary)
        return self._norm(self.log)


@pytest.fixture
def transcript(monkeypatch, tmp_path):
    record = _Transcript(tmp_path)

    def say(self, text=""):
        record.add("say", text)

    def confirm(self, prompt):
        given = record.answers
        answer = given.pop(0) if isinstance(given, list) else given
        record.add("ask", prompt, answer)
        return answer

    def execute(self, argv, env=None):
        record.add("run", argv, env)
        return record.exec_ok

    monkeypatch.setattr(wizard.Wizard, "say", say)
    monkeypatch.setattr(wizard.Wizard, "confirm", confirm)
    monkeypatch.setattr(wizard.Wizard, "execute", execute)
    return record


def _server_fakes(monkeypatch, record, units=(), stopped=True, save_error=None):
    monkeypatch.setattr(wizard.launch, "old_units", lambda: list(units))
    monkeypatch.setattr(wizard.launch, "retire_units", lambda: record.add("retire_units") or [])

    def stop():
        record.add("stop")
        return stopped

    monkeypatch.setattr(wizard.launch, "stop", stop)

    def save_device(device):
        record.add("save_device", device)
        if save_error:
            raise save_error

    monkeypatch.setattr(config, "save_device", save_device)
    monkeypatch.setattr(config, "load_device", lambda: "NPU")


_UNITS = [
    wizard.Path("/units/vinowhisper-server.socket"),
    wizard.Path("/units/vinowhisper-server.service"),
]
_UNIT_LINES = [
    ("say", "  The server no longer runs from systemd units; these are left over:"),
    ("say", "    /units/vinowhisper-server.socket"),
    ("say", "    /units/vinowhisper-server.service"),
    (
        "say",
        "  $ systemctl --user disable --now vinowhisper-server.socket vinowhisper-server.service",
    ),
]
_STARTS = "the server starts on first use and exits when idle"


def test_setup_server_with_nothing_left_over_only_stops_a_running_server(monkeypatch, transcript):
    _server_fakes(monkeypatch, transcript)
    assert transcript.play(wizard.Wizard(assume_yes=True).setup_server) == [
        ("stop",),
        ("outcome", True, f"device NPU; {_STARTS}"),
    ]


def test_setup_server_asks_before_removing_old_units_and_removes_them_on_yes(
    monkeypatch, transcript
):
    _server_fakes(monkeypatch, transcript, units=_UNITS)
    assert transcript.play(wizard.Wizard(assume_yes=True).setup_server) == [
        *_UNIT_LINES,
        ("ask", "remove them?", True),
        ("retire_units",),
        ("stop",),
        ("outcome", True, f"old systemd units removed; device NPU; {_STARTS}"),
    ]


def test_setup_server_leaves_old_units_and_stops_nothing_on_no(monkeypatch, transcript):
    transcript.answers = False
    _server_fakes(monkeypatch, transcript, units=_UNITS)
    assert transcript.play(wizard.Wizard().setup_server) == [
        *_UNIT_LINES,
        ("ask", "remove them?", False),
        (
            "outcome",
            None,
            "old units left in place; they still start the server",
        ),
    ]


def test_setup_server_saves_an_explicit_device_before_it_stops_the_server(monkeypatch, transcript):
    _server_fakes(monkeypatch, transcript)
    assert transcript.play(wizard.Wizard(assume_yes=True, device="GPU").setup_server) == [
        ("save_device", "GPU"),
        ("stop",),
        ("outcome", True, f"device GPU; {_STARTS}"),
    ]


def test_setup_server_does_not_rewrite_an_explicit_device_that_is_already_saved(
    monkeypatch, transcript
):
    _server_fakes(monkeypatch, transcript)
    assert transcript.play(wizard.Wizard(assume_yes=True, device="NPU").setup_server) == [
        ("stop",),
        ("outcome", True, f"device NPU; {_STARTS}"),
    ]


def test_setup_server_on_a_dry_run_says_what_it_would_write_and_touches_nothing(
    monkeypatch, transcript
):
    _server_fakes(monkeypatch, transcript)
    played = transcript.play(wizard.Wizard(dry_run=True, device="CPU").setup_server)
    assert played == [("outcome", None, "would write device CPU to <tmp>/no-server.json")]


def test_setup_server_on_a_dry_run_would_stop_the_server_but_does_not(monkeypatch, transcript):
    _server_fakes(monkeypatch, transcript)
    assert transcript.play(wizard.Wizard(dry_run=True).setup_server) == [
        (
            "outcome",
            None,
            "would stop a running server, so the next use starts this one",
        ),
    ]


def test_setup_server_fails_when_the_device_cannot_be_written_and_never_stops(
    monkeypatch, transcript
):
    _server_fakes(monkeypatch, transcript, save_error=OSError("read-only"))
    assert transcript.play(wizard.Wizard(assume_yes=True, device="GPU").setup_server) == [
        ("save_device", "GPU"),
        ("outcome", False, "could not write <tmp>/no-server.json: read-only"),
    ]


def test_setup_server_fails_when_the_running_server_will_not_stop(monkeypatch, transcript):
    _server_fakes(monkeypatch, transcript, units=_UNITS, stopped=False)
    assert transcript.play(wizard.Wizard(assume_yes=True).setup_server) == [
        *_UNIT_LINES,
        ("ask", "remove them?", True),
        ("retire_units",),
        ("stop",),
        (
            "outcome",
            False,
            "the running server did not stop; `vinowhisper-server --stop`",
        ),
    ]


def _fs_recorder(monkeypatch, record):
    real = {
        "mkdir": wizard.Path.mkdir,
        "unlink": wizard.Path.unlink,
        "symlink_to": wizard.Path.symlink_to,
    }

    def mkdir(self, *args, **kwargs):
        if self == wizard.BIN_DIR and kwargs.get("parents"):
            record.add("mkdir", self)
        return real["mkdir"](self, *args, **kwargs)

    def unlink(self, *args, **kwargs):
        record.add("unlink", self)
        return real["unlink"](self, *args, **kwargs)

    def symlink_to(self, target, *args, **kwargs):
        record.add("symlink", self, target)
        return real["symlink_to"](self, target, *args, **kwargs)

    monkeypatch.setattr(wizard.Path, "mkdir", mkdir)
    monkeypatch.setattr(wizard.Path, "unlink", unlink)
    monkeypatch.setattr(wizard.Path, "symlink_to", symlink_to)


def _link_entries():
    return [
        ("symlink", f"<tmp>/local/bin/vinowhisper-{name}", f"<tmp>/venv/bin/vinowhisper-{name}")
        for name in wizard.COMMANDS
    ]


def test_link_binaries_names_the_missing_entry_point_and_writes_nothing(venv_bin, transcript):
    (venv_bin / "vinowhisper-replay").unlink()
    transcript.log.clear()
    _ = transcript.play(wizard.Wizard(assume_yes=True).link_binaries)
    assert transcript.log == [
        (
            "outcome",
            False,
            f"vinowhisper-replay not next to {venv_bin / 'python'} — run `uv sync` first",
        )
    ]


def test_link_binaries_has_nothing_to_do_when_the_venv_is_the_bin_dir(
    venv_bin, monkeypatch, transcript
):
    monkeypatch.setattr(wizard, "BIN_DIR", venv_bin)
    assert transcript.play(wizard.Wizard(assume_yes=True).link_binaries) == [
        ("outcome", True, "already installed in <tmp>/venv/bin")
    ]


def test_link_binaries_asks_first_and_links_every_command_on_yes(venv_bin, monkeypatch, transcript):
    _fs_recorder(monkeypatch, transcript)
    assert transcript.play(wizard.Wizard(assume_yes=True).link_binaries) == [
        ("say", f"  Symlinking {len(wizard.COMMANDS)} commands into <tmp>/local/bin"),
        ("ask", "link them into <tmp>/local/bin?", True),
        ("mkdir", "<tmp>/local/bin"),
        *_link_entries(),
        ("outcome", True, "linked into <tmp>/local/bin"),
    ]


def test_link_binaries_links_nothing_on_no(venv_bin, monkeypatch, transcript):
    transcript.answers = False
    _fs_recorder(monkeypatch, transcript)
    assert transcript.play(wizard.Wizard().link_binaries) == [
        ("say", f"  Symlinking {len(wizard.COMMANDS)} commands into <tmp>/local/bin"),
        ("ask", "link them into <tmp>/local/bin?", False),
        (
            "outcome",
            None,
            "skipped; run with `uv run` or add <tmp>/venv/bin to PATH",
        ),
    ]


def test_link_binaries_unlinks_what_is_there_before_it_links_and_warns_about_path(
    venv_bin, monkeypatch, transcript
):
    monkeypatch.setenv("PATH", "/usr/bin")
    wizard.BIN_DIR.mkdir(parents=True)
    (wizard.BIN_DIR / "vinowhisper-caption").symlink_to("/nonexistent")
    (wizard.BIN_DIR / "vinowhisper-doctor").write_text("old")
    _fs_recorder(monkeypatch, transcript)
    played = transcript.play(wizard.Wizard(assume_yes=True).link_binaries)
    expected_entries = []
    for entry in _link_entries():
        if entry[1].endswith(("-caption", "-doctor")):
            expected_entries.append(("unlink", entry[1]))
        expected_entries.append(entry)
    assert played == [
        ("say", f"  Symlinking {len(wizard.COMMANDS)} commands into <tmp>/local/bin"),
        ("ask", "link them into <tmp>/local/bin?", True),
        ("mkdir", "<tmp>/local/bin"),
        *expected_entries,
        (
            "outcome",
            True,
            "linked, but <tmp>/local/bin is not on your PATH — add it",
        ),
    ]


def _pin(component=wizard.overlay.TRAY):
    return wizard.overlay.Pin("1.2.3", "x86_64", "ab" * 32, component)


def _download_lines(pin):
    return [
        ("say", f"  Download {pin.url}"),
        ("say", f"  sha256   {pin.sha256} (pinned in vinowhisper 1.2.3)"),
        ("ask", "install it into <tmp>/bin?", True),
    ]


def _overlay_fakes(
    monkeypatch, record, tmp_path, found=(), available=None, builds=False, fetch_error=None
):
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(wizard, "BIN_DIR", bin_dir)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr(wizard, "VERSION", "1.2.3")
    present = {binary: path for binary, path in found}
    pins = available or {}

    monkeypatch.setattr(
        wizard.overlay,
        "installed",
        lambda directory, component=wizard.overlay.TRAY: present.get(component.binary),
    )
    monkeypatch.setattr(
        wizard.overlay, "availability", lambda component=wizard.overlay.TRAY: pins[component.binary]
    )
    monkeypatch.setattr(wizard.overlay, "can_build", lambda component=wizard.overlay.TRAY: builds)

    def fetch(pin, dest):
        record.add("fetch", pin.url, dest)
        if fetch_error:
            raise wizard.overlay.OverlayError(fetch_error)
        return dest

    def install_binary(source, dest):
        record.add("install_binary", source, dest)
        return dest

    monkeypatch.setattr(wizard.overlay, "fetch", fetch)
    monkeypatch.setattr(wizard.overlay, "install_binary", install_binary)
    return bin_dir


def _tray_commands(path, *, launcher=True, restart=False):
    entries = []
    if launcher:
        entries += [
            ("say", f"  $ {path} --install"),
            ("ask", "add a launcher entry for it?", True),
            ("run", [path, "--install"], None),
        ]
    entries += [
        ("say", f"  $ {path} --autostart"),
        ("ask", "start it in the tray at login?", True),
        ("run", [path, "--autostart"], None),
    ]
    if restart:
        entries += [
            ("say", f"  $ {path} restart"),
            ("ask", "restart a running tray so it uses this one?", True),
            ("run", [path, "restart"], None),
        ]
    return entries


_TRAY = "<tmp>/bin/vinowhisper-tray"
_GUI = "<tmp>/bin/vinowhisper-gui"
_TRAY_SUMMARY = "(open it from the app menu, or run vinowhisper-tray)"


def test_install_overlay_is_skipped_on_gnome_without_looking_for_anything(
    monkeypatch, tmp_path, transcript
):
    _overlay_fakes(monkeypatch, transcript, tmp_path)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    assert transcript.play(wizard.Wizard(assume_yes=True).install_overlay) == [
        ("outcome", None, "skipped on GNOME, which has no layer-shell (docs/tray.md)")
    ]


def test_install_overlay_with_both_binaries_present_adds_the_launcher_and_autostart_only(
    monkeypatch, tmp_path, transcript
):
    bin_dir = _overlay_fakes(
        monkeypatch,
        transcript,
        tmp_path,
        found=[
            ("vinowhisper-tray", tmp_path / "bin/vinowhisper-tray"),
            ("vinowhisper-gui", tmp_path / "bin/vinowhisper-gui"),
        ],
    )
    assert bin_dir == tmp_path / "bin"
    assert transcript.play(wizard.Wizard(assume_yes=True).install_overlay) == [
        ("say", f"  Found {_TRAY}"),
        *_tray_commands(_TRAY),
        ("say", f"  Found {_GUI}"),
        ("outcome", True, f"{_TRAY} {_TRAY_SUMMARY}; settings window {_GUI}"),
    ]


def test_install_overlay_does_not_add_a_launcher_for_a_tray_outside_the_home(
    monkeypatch, tmp_path, transcript
):
    _overlay_fakes(
        monkeypatch,
        transcript,
        tmp_path,
        found=[
            ("vinowhisper-tray", wizard.Path("/usr/bin/vinowhisper-tray")),
            ("vinowhisper-gui", wizard.Path("/usr/bin/vinowhisper-gui")),
        ],
    )
    assert transcript.play(wizard.Wizard(assume_yes=True).install_overlay) == [
        ("say", "  Found /usr/bin/vinowhisper-tray"),
        *_tray_commands("/usr/bin/vinowhisper-tray", launcher=False),
        ("say", "  Found /usr/bin/vinowhisper-gui"),
        (
            "outcome",
            True,
            f"/usr/bin/vinowhisper-tray {_TRAY_SUMMARY}; settings window /usr/bin/vinowhisper-gui",
        ),
    ]


def test_install_overlay_downloads_both_on_yes_and_restarts_the_tray_it_replaced(
    monkeypatch, tmp_path, transcript
):
    _overlay_fakes(
        monkeypatch,
        transcript,
        tmp_path,
        available={
            "vinowhisper-tray": _pin(),
            "vinowhisper-gui": _pin(wizard.overlay.SETTINGS),
        },
    )
    played = transcript.play(wizard.Wizard(assume_yes=True).install_overlay)
    tray, gui = _pin(), _pin(wizard.overlay.SETTINGS)
    assert played == [
        *_download_lines(tray),
        ("fetch", tray.url, _TRAY),
        *_tray_commands(_TRAY, restart=True),
        *_download_lines(gui),
        ("fetch", gui.url, _GUI),
        ("outcome", True, f"{_TRAY} {_TRAY_SUMMARY}; settings window {_GUI}"),
    ]


def test_install_overlay_skips_everything_when_the_tray_download_is_declined(
    monkeypatch, tmp_path, transcript
):
    transcript.answers = False
    _overlay_fakes(monkeypatch, transcript, tmp_path, available={"vinowhisper-tray": _pin()})
    assert transcript.play(wizard.Wizard().install_overlay) == [
        ("say", f"  Download {_pin().url}"),
        ("say", f"  sha256   {_pin().sha256} (pinned in vinowhisper 1.2.3)"),
        ("ask", "install it into <tmp>/bin?", False),
        ("outcome", None, "skipped; `vinowhisper-setup --gui` installs it any time"),
    ]


def test_install_overlay_fails_when_the_download_does_not_verify(monkeypatch, tmp_path, transcript):
    _overlay_fakes(
        monkeypatch,
        transcript,
        tmp_path,
        available={"vinowhisper-tray": _pin()},
        fetch_error="sha256 mismatch",
    )
    assert transcript.play(wizard.Wizard(assume_yes=True).install_overlay) == [
        *_download_lines(_pin()),
        ("fetch", _pin().url, _TRAY),
        ("outcome", False, "sha256 mismatch"),
    ]


def test_install_overlay_builds_with_cargo_when_there_is_no_pin_and_installs_the_result(
    monkeypatch, tmp_path, transcript
):
    gui_path = tmp_path / "bin/vinowhisper-gui"
    _overlay_fakes(
        monkeypatch,
        transcript,
        tmp_path,
        found=[("vinowhisper-gui", gui_path)],
        available={"vinowhisper-tray": "no pin for this version"},
        builds=True,
    )
    build = wizard.overlay.build_argv(wizard.overlay.TRAY)
    built = wizard.overlay.built_binary(wizard.overlay.TRAY)
    assert transcript.play(wizard.Wizard(assume_yes=True).install_overlay) == [
        ("say", f"  $ {' '.join(build)}"),
        ("ask", "build it with cargo? (minutes, the first time)", True),
        ("run", build, None),
        ("install_binary", str(built), _TRAY),
        *_tray_commands(_TRAY, restart=True),
        ("say", f"  Found {_GUI}"),
        ("outcome", True, f"{_TRAY} {_TRAY_SUMMARY}; settings window {_GUI}"),
    ]


def test_install_overlay_leaves_the_tray_unbuilt_on_no(monkeypatch, tmp_path, transcript):
    transcript.answers = False
    _overlay_fakes(
        monkeypatch,
        transcript,
        tmp_path,
        available={"vinowhisper-tray": "no pin for this version"},
        builds=True,
    )
    build = wizard.overlay.build_argv(wizard.overlay.TRAY)
    assert transcript.play(wizard.Wizard().install_overlay) == [
        ("say", f"  $ {' '.join(build)}"),
        ("ask", "build it with cargo? (minutes, the first time)", False),
        ("outcome", None, "not built; `vinowhisper-setup --gui` builds it any time"),
    ]


def test_install_overlay_says_why_when_it_can_neither_download_nor_build(
    monkeypatch, tmp_path, transcript
):
    _overlay_fakes(
        monkeypatch, transcript, tmp_path, available={"vinowhisper-tray": "no pin, no cargo"}
    )
    assert transcript.play(wizard.Wizard(assume_yes=True).install_overlay) == [
        ("outcome", None, "no pin, no cargo")
    ]


def test_install_overlay_without_a_settings_window_still_installs_the_tray(
    monkeypatch, tmp_path, transcript
):
    _overlay_fakes(
        monkeypatch,
        transcript,
        tmp_path,
        found=[("vinowhisper-tray", tmp_path / "bin/vinowhisper-tray")],
        available={"vinowhisper-gui": "no window for this version"},
    )
    assert transcript.play(wizard.Wizard(assume_yes=True).install_overlay) == [
        ("say", f"  Found {_TRAY}"),
        *_tray_commands(_TRAY),
        (
            "outcome",
            None,
            f"{_TRAY} {_TRAY_SUMMARY}; no settings window: no window for this version",
        ),
    ]


def test_install_overlay_declining_the_launcher_still_asks_about_autostart(
    monkeypatch, tmp_path, transcript
):
    transcript.answers = [False, True]
    _overlay_fakes(
        monkeypatch,
        transcript,
        tmp_path,
        found=[
            ("vinowhisper-tray", tmp_path / "bin/vinowhisper-tray"),
            ("vinowhisper-gui", tmp_path / "bin/vinowhisper-gui"),
        ],
    )
    assert transcript.play(wizard.Wizard().install_overlay) == [
        ("say", f"  Found {_TRAY}"),
        ("say", f"  $ {_TRAY} --install"),
        ("ask", "add a launcher entry for it?", False),
        ("say", f"  $ {_TRAY} --autostart"),
        ("ask", "start it in the tray at login?", True),
        ("run", [_TRAY, "--autostart"], None),
        ("say", f"  Found {_GUI}"),
        ("outcome", True, f"{_TRAY} {_TRAY_SUMMARY}; settings window {_GUI}"),
    ]


_REVISION = "e8727524f962abcdef"
_SOURCE_DIR = f"/src/openai--whisper-small.en@{_REVISION}"


def _model_fakes(
    monkeypatch,
    record,
    tmp_path,
    kind="NPU",
    present=False,
    cli="/venv/bin/optimum-cli",
    cli_after_install=None,
    pinned=True,
    fetch_error=None,
    export_ok=True,
):
    import types

    if present:
        (tmp_path / ("npu-export" if kind == "NPU" else "stateful-export")).mkdir()
        (tmp_path / ("npu-export" if kind == "NPU" else "stateful-export") / "m.xml").write_text("")
    if isinstance(kind, Exception):
        error = kind

        def unusable(*args, **kwargs):
            raise error

        monkeypatch.setattr(wizard.devices, "select", unusable)
        kind = "NPU"
    else:
        monkeypatch.setattr(
            wizard.devices, "select", lambda *a, **k: types.SimpleNamespace(kind=kind)
        )
    monkeypatch.setattr(
        config,
        "model_dir",
        lambda device_kind, multilingual=False, model=config.DEFAULT_MODEL: (
            tmp_path / ("npu-export" if device_kind == "NPU" else "stateful-export")
        ),
    )
    monkeypatch.setattr(
        config,
        "export_command",
        lambda variant, multilingual=False, model=config.DEFAULT_MODEL: f"EXPORT {variant}",
    )
    monkeypatch.setattr(config, "EXPORT_TMP_ROOT", tmp_path / "export-tmp")
    monkeypatch.setattr(wizard.model_source, "SOURCE_ROOT", wizard.Path("/src"))
    clis = [cli, cli_after_install]
    monkeypatch.setattr(wizard, "optimum_cli", lambda: clis.pop(0))
    monkeypatch.setattr(wizard, "export_extra_commands", lambda: [["uv", "sync", "--extra", "x"]])
    monkeypatch.setattr(wizard, "memory_cap_argv", lambda: [])
    source = wizard.model_source.Source("openai/whisper-small.en", _REVISION)
    monkeypatch.setattr(wizard.model_source, "load", lambda model_id: source if pinned else None)

    def fetch(src, say):
        record.add("fetch", src.model_id)
        say("fetching")
        if fetch_error:
            raise fetch_error

    monkeypatch.setattr(wizard.model_source, "fetch", fetch)

    def export(self, argv):
        record.add("export", argv)
        return export_ok

    monkeypatch.setattr(wizard.Wizard, "export", export)

    def check_digests(self, variant, directory, summary, model_id=config.MODEL_ID):
        record.add("check_digests", variant, directory, summary, model_id)
        return wizard.Outcome(True, f"{summary}, digests verified")

    monkeypatch.setattr(wizard.Wizard, "check_digests", check_digests)


def _plan(variant="npu"):
    flag = " --disable-stateful" if variant == "npu" else ""
    out = f"<tmp>/{variant}-export"
    return [
        ("say", "  This downloads ~1GB from Hugging Face and takes a few minutes."),
        (
            "say",
            f"  $ fetch openai/whisper-small.en@e8727524f962 into {_SOURCE_DIR}, "
            "checking every file's sha256",
        ),
        (
            "say",
            "  Temporary export files go under <tmp>/export-tmp (on disk, not /tmp), "
            "removed afterwards",
        ),
        (
            "say",
            "  $ HF_HUB_OFFLINE=1 CI=true TMPDIR=<tmp>/export-tmp/… /venv/bin/optimum-cli "
            f"export openvino --model {_SOURCE_DIR} "
            f"--task automatic-speech-recognition-with-past{flag} {out}",
        ),
        ("ask", "download and export it now?", True),
    ]


def _export_argv(variant="npu"):
    return [
        "/venv/bin/optimum-cli",
        "export",
        "openvino",
        "--model",
        _SOURCE_DIR,
        "--task",
        "automatic-speech-recognition-with-past",
        *(["--disable-stateful"] if variant == "npu" else []),
        f"<tmp>/{variant}-export",
    ]


def test_check_model_with_an_export_already_there_only_verifies_it(
    monkeypatch, tmp_path, transcript
):
    _model_fakes(monkeypatch, transcript, tmp_path, present=True)
    assert transcript.play(wizard.Wizard(assume_yes=True).check_model) == [
        (
            "check_digests",
            "npu",
            "<tmp>/npu-export",
            "npu export present at <tmp>/npu-export",
            "openai/whisper-small.en",
        ),
        ("outcome", True, "npu export present at <tmp>/npu-export, digests verified"),
    ]


def test_check_model_on_a_gpu_wants_the_stateful_export(monkeypatch, tmp_path, transcript):
    _model_fakes(monkeypatch, transcript, tmp_path, kind="GPU", present=True)
    assert transcript.play(wizard.Wizard(assume_yes=True).check_model) == [
        (
            "check_digests",
            "stateful",
            "<tmp>/stateful-export",
            "stateful export present at <tmp>/stateful-export",
            "openai/whisper-small.en",
        ),
        (
            "outcome",
            True,
            "stateful export present at <tmp>/stateful-export, digests verified",
        ),
    ]


def test_check_model_downloads_and_exports_on_yes_then_verifies(monkeypatch, tmp_path, transcript):
    _model_fakes(monkeypatch, transcript, tmp_path)
    assert transcript.play(wizard.Wizard(assume_yes=True).check_model) == [
        ("say", "  No npu export at <tmp>/npu-export."),
        *_plan(),
        ("fetch", "openai/whisper-small.en"),
        ("say", "  fetching"),
        ("export", _export_argv()),
        (
            "check_digests",
            "npu",
            "<tmp>/npu-export",
            "exported to <tmp>/npu-export",
            "openai/whisper-small.en",
        ),
        ("outcome", True, "exported to <tmp>/npu-export, digests verified"),
    ]


def test_check_model_downloads_nothing_on_no_and_names_the_command(
    monkeypatch, tmp_path, transcript
):
    transcript.answers = False
    _model_fakes(monkeypatch, transcript, tmp_path, kind="GPU")
    played = transcript.play(wizard.Wizard().check_model)
    assert played[:1] == [("say", "  No stateful export at <tmp>/stateful-export.")]
    assert played[-2:] == [
        ("ask", "download and export it now?", False),
        ("outcome", None, "run EXPORT stateful when ready"),
    ]
    assert not any(entry[0] in ("fetch", "export") for entry in played)


def test_check_model_when_the_device_is_unusable_plans_for_the_npu(
    monkeypatch, tmp_path, transcript
):
    _model_fakes(monkeypatch, transcript, tmp_path, kind=wizard.devices.DeviceError("none"))
    transcript.answers = False
    played = transcript.play(wizard.Wizard().check_model)
    assert played[0] == ("say", "  No npu export at <tmp>/npu-export.")
    assert played[-1] == ("outcome", None, "run EXPORT npu when ready")


def test_check_model_offers_the_export_extra_and_goes_on_once_it_is_installed(
    monkeypatch, tmp_path, transcript
):
    _model_fakes(
        monkeypatch, transcript, tmp_path, cli=None, cli_after_install="/venv/bin/optimum-cli"
    )
    played = transcript.play(wizard.Wizard(assume_yes=True).check_model)
    assert played[:6] == [
        ("say", "  No npu export at <tmp>/npu-export."),
        ("say", "  Exporting needs optimum, the optional `export` extra (it brings torch)."),
        ("say", "  $ uv sync --extra x"),
        ("ask", "install it?", True),
        ("run", ["uv", "sync", "--extra", "x"], None),
        _plan()[0],
    ]
    assert played[-1] == ("outcome", True, "exported to <tmp>/npu-export, digests verified")


def test_check_model_stops_at_the_export_extra_when_it_is_declined(
    monkeypatch, tmp_path, transcript
):
    transcript.answers = False
    _model_fakes(monkeypatch, transcript, tmp_path, cli=None)
    assert transcript.play(wizard.Wizard().check_model) == [
        ("say", "  No npu export at <tmp>/npu-export."),
        ("say", "  Exporting needs optimum, the optional `export` extra (it brings torch)."),
        ("say", "  $ uv sync --extra x"),
        ("ask", "install it?", False),
        ("outcome", None, "install the export extra, then re-run this"),
    ]


def test_check_model_fails_when_optimum_is_still_missing_after_the_extra(
    monkeypatch, tmp_path, transcript
):
    _model_fakes(monkeypatch, transcript, tmp_path, cli=None, cli_after_install=None)
    played = transcript.play(wizard.Wizard(assume_yes=True).check_model)
    assert played[-2:] == [
        ("run", ["uv", "sync", "--extra", "x"], None),
        ("outcome", False, "the export extra installed, but optimum-cli is not on PATH"),
    ]


def test_check_model_fails_when_no_source_is_pinned_before_it_asks_anything(
    monkeypatch, tmp_path, transcript
):
    _model_fakes(monkeypatch, transcript, tmp_path, pinned=False)
    assert transcript.play(wizard.Wizard(assume_yes=True).check_model) == [
        ("say", "  No npu export at <tmp>/npu-export."),
        (
            "outcome",
            False,
            "no pinned source for openai/whisper-small.en; reinstall vinowhisper",
        ),
    ]


def test_check_model_fails_on_a_source_that_does_not_verify(monkeypatch, tmp_path, transcript):
    _model_fakes(
        monkeypatch,
        transcript,
        tmp_path,
        fetch_error=wizard.model_source.SourceError("sha256 mismatch"),
    )
    played = transcript.play(wizard.Wizard(assume_yes=True).check_model)
    assert played[-3:] == [
        ("fetch", "openai/whisper-small.en"),
        ("say", "  fetching"),
        ("outcome", False, "sha256 mismatch"),
    ]


def test_check_model_defers_a_download_that_broke_off_and_never_exports(
    monkeypatch, tmp_path, transcript
):
    _model_fakes(
        monkeypatch, transcript, tmp_path, fetch_error=wizard.requests.ConnectionError("reset")
    )
    played = transcript.play(wizard.Wizard(assume_yes=True).check_model)
    assert played[-1] == ("outcome", None, "download failed (reset); re-run to resume")
    assert not any(entry[0] == "export" for entry in played)


def test_check_model_defers_to_the_command_when_the_export_itself_fails(
    monkeypatch, tmp_path, transcript
):
    _model_fakes(monkeypatch, transcript, tmp_path, export_ok=False)
    played = transcript.play(wizard.Wizard(assume_yes=True).check_model)
    assert played[-2:] == [
        ("export", _export_argv()),
        ("outcome", None, "run EXPORT npu when ready"),
    ]
    assert not any(entry[0] == "check_digests" for entry in played)


def test_check_model_hands_a_canary_choice_to_the_canary_check(monkeypatch, tmp_path, transcript):
    _model_fakes(monkeypatch, transcript, tmp_path, kind="GPU")

    def check_canary(self, kind):
        transcript.add("check_canary", kind)
        return wizard.Outcome(True, "canary")

    monkeypatch.setattr(wizard.Wizard, "check_canary", check_canary)
    instance = wizard.Wizard(assume_yes=True)
    instance.model = config.CANARY_MODEL
    assert transcript.play(instance.check_model) == [
        ("check_canary", "GPU"),
        ("outcome", True, "canary"),
    ]


def test_only_an_npu_gets_the_static_export():
    assert wizard.export_variant("NPU") == "npu"
    assert wizard.export_variant("GPU") == "stateful"
    assert wizard.export_variant("CPU") == "stateful"


def test_the_old_unit_lines_list_each_unit_before_the_command_that_removes_them():
    lines = wizard.old_unit_lines([wizard.Path("/u/a"), wizard.Path("/u/b")])
    assert lines[1:3] == ["    /u/a", "    /u/b"]
    assert lines[-1].startswith("  $ systemctl --user disable --now ")


def test_a_device_is_saved_only_when_it_was_asked_for_and_differs():
    assert wizard.device_needs_saving(True, "GPU", "NPU") is True
    assert wizard.device_needs_saving(True, "NPU", "NPU") is False
    assert wizard.device_needs_saving(False, "GPU", "NPU") is False


def test_the_server_summary_mentions_removed_units_only_when_there_were_some():
    assert wizard.server_summary(True, "CPU").startswith("old systemd units removed; device CPU")
    assert wizard.server_summary(False, "CPU").startswith("device CPU;")


def test_the_missing_commands_summary_names_each_and_the_interpreter():
    assert wizard.missing_commands_summary(["a", "b"], "/v/python") == (
        "a, b not next to /v/python — run `uv sync` first"
    )


def test_a_link_summary_warns_only_when_the_directory_is_off_the_path():
    bin_dir = wizard.Path("/h/.local/bin")
    assert wizard.linked_summary(bin_dir, "/usr/bin:/h/.local/bin") == "linked into /h/.local/bin"
    assert "not on your PATH" in wizard.linked_summary(bin_dir, "/usr/bin:/h/.local")
    assert "not on your PATH" in wizard.linked_summary(bin_dir, "")


def test_a_launcher_is_wanted_only_for_a_binary_under_the_home():
    home = wizard.Path("/h")
    assert wizard.wants_launcher(wizard.Path("/h/.local/bin/t"), home) is True
    assert wizard.wants_launcher(wizard.Path("/usr/bin/t"), home) is False


def test_a_missing_settings_window_leaves_the_overlay_step_undone_not_failed():
    tray = wizard.Path("/h/t")
    with_window = wizard.overlay_outcome(tray, wizard.Outcome(True, "/h/g"))
    assert with_window.ok is True and with_window.summary.endswith("settings window /h/g")
    without = wizard.overlay_outcome(tray, wizard.Outcome(None, "why"))
    assert without.ok is None and without.summary.endswith("no settings window: why")
