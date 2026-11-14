"""The Flatpak: what differs inside its sandbox, its update command, and its manifest.

Nothing here runs Flatpak. A sandbox is a `/.flatpak-info` file, which a test fakes by
pointing `sandbox.INFO` at one of its own; the host's `flatpak` is a recorded argv.
"""

import hashlib
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from vinowhisper import config, flatpak_update, launch, sandbox, summary, wizard

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging/flatpak"
APP = "io.github.karanshukla.vinowhisper"


def _info(tmp_path, monkeypatch, branch="stable", scope="home"):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    base = home / ".local/share/flatpak" if scope == "home" else Path("/var/lib/flatpak")
    info = tmp_path / "flatpak-info"
    info.write_text(
        f"[Application]\nname={APP}\nruntime=runtime/org.freedesktop.Platform/x86_64/26.08\n\n"
        f"[Instance]\nbranch={branch}\napp-path={base}/app/{APP}/x86_64/{branch}/abc/files\n"
    )
    monkeypatch.setattr(sandbox, "INFO", info)
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setenv("HOME", str(home))
    return home


def test_outside_a_sandbox_there_is_no_app_id(tmp_path, monkeypatch):
    """FLATPAK_ID alone can be left in the environment of something started from one."""
    monkeypatch.setattr(sandbox, "INFO", tmp_path / "absent")
    monkeypatch.setenv("FLATPAK_ID", APP)
    assert sandbox.flatpak_id() is None
    assert sandbox.info_value("Instance", "branch") is None


def test_inside_one_the_id_and_the_instance_are_read_from_the_info_file(tmp_path, monkeypatch):
    _info(tmp_path, monkeypatch)
    assert sandbox.flatpak_id() == APP
    assert sandbox.info_value("Instance", "branch") == "stable"
    assert sandbox.info_value("Application", "branch") is None
    monkeypatch.setenv("FLATPAK_ID", "org.example.Env")
    assert sandbox.flatpak_id() == "org.example.Env"


def test_a_value_keeps_its_semicolons_colons_and_hashes(tmp_path):
    """Flatpak's lists end in `;`, and a path may hold `:` or `#`: none starts a comment
    or splits a key, which is what a parser's defaults would do."""
    info = tmp_path / "flatpak-info"
    info.write_text(
        "[Application]\nname=a.b\n\n[Context]\nfilesystems=xdg-run/pipewire-0;home;\n"
        "# a comment\n[Instance]\napp-path=/a:b #1/files\nBranch=other\nbranch=stable\nflag=\n"
    )
    assert sandbox.info_value("Context", "filesystems", info) == "xdg-run/pipewire-0;home;"
    assert sandbox.info_value("Instance", "app-path", info) == "/a:b #1/files"
    assert sandbox.info_value("Instance", "branch", info) == "stable"
    assert sandbox.info_value("Instance", "flag", info) is None
    info.write_text("not a keyfile at all\n[Instance\n")
    assert sandbox.info_value("Instance", "branch", info) is None


def test_the_runtime_dir_is_the_part_every_sandbox_of_the_app_shares(tmp_path, monkeypatch):
    """characterization: each `flatpak run` gets a runtime directory of its own, so a
    socket straight under XDG_RUNTIME_DIR would be invisible to the tray's next command.
    Only app/<id> is shared, and the server socket and the summary files live there."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setattr(sandbox, "INFO", tmp_path / "absent")
    assert config.server_socket() == Path("/run/user/1000/vinowhisper/server.sock")
    _info(tmp_path, monkeypatch)
    shared = Path(f"/run/user/1000/app/{APP}")
    assert config.runtime_dir() == shared
    assert config.server_socket() == shared / "vinowhisper/server.sock"
    assert summary._runtime_file(config.SUMMARISING_RELPATH) == shared / config.SUMMARISING_RELPATH


def test_the_server_gets_a_sandbox_of_its_own_with_this_one_as_the_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "INFO", tmp_path / "absent")
    plain = [sys.executable, "-m", "vinowhisper.server"]
    assert launch.server_commands() == [plain]
    _info(tmp_path, monkeypatch)
    assert launch.server_commands() == [["flatpak-spawn", "--latest-version", *plain], plain]


def test_a_missing_portal_falls_back_to_starting_the_server_here(tmp_path, monkeypatch):
    _info(tmp_path, monkeypatch)
    runtime = tmp_path / "run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    tried = []

    def spawn(argv):
        tried.append(argv[0])
        return SimpleNamespace(poll=lambda: 1, returncode=1)

    monkeypatch.setattr(launch, "_spawn", spawn)
    with pytest.raises(launch.StartError, match="exited as it started"):
        launch.ensure()
    assert tried == ["flatpak-spawn", sys.executable]


def test_the_spawn_helper_is_let_go_once_the_server_listens(tmp_path, monkeypatch):
    """characterization: flatpak-spawn lives as long as the server it started, and keeps
    the launching command's sandbox and its stdout open with it, so `... | grep` never
    ended. It is killed, not terminated: it forwards SIGTERM to the server. Measured in
    the sandbox 2026-11-14: the server keeps answering after the helper is killed."""
    _info(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    up = iter([False, False, True])
    monkeypatch.setattr(launch, "running", lambda path: next(up))
    killed = []
    helper = SimpleNamespace(poll=lambda: None, kill=lambda: killed.append("helper"))
    spawned = []
    monkeypatch.setattr(launch, "_spawn", lambda argv: spawned.append(argv[0]) or helper)
    launch.ensure()
    assert spawned == ["flatpak-spawn"] and killed == ["helper"]


def test_a_server_started_natively_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "INFO", tmp_path / "absent")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    up = iter([False, False, True])
    monkeypatch.setattr(launch, "running", lambda path: next(up))
    process = SimpleNamespace(poll=lambda: None, kill=lambda: pytest.fail("killed the server"))
    monkeypatch.setattr(launch, "_spawn", lambda argv: process)
    launch.ensure()


def test_setup_in_the_flatpak_skips_what_the_bundle_carries(tmp_path, monkeypatch, capsys):
    _info(tmp_path, monkeypatch)
    ran = []
    for name in (
        "check_python",
        "check_audio",
        "check_decoder",
        "check_device",
        "check_model",
        "save_language",
        "setup_server",
    ):
        monkeypatch.setattr(
            wizard.Wizard,
            name,
            lambda self, name=name: ran.append(name) or wizard.Outcome(True, ""),
        )
    for name in ("link_binaries", "install_completion", "install_overlay", "install_ovfetch"):
        monkeypatch.setattr(wizard.Wizard, name, lambda self, name=name: pytest.fail(name))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert wizard.Wizard(assume_yes=True).run_all() == 0
    assert ran[-1] == "setup_server"
    assert wizard.main(["--gui"]) == 0
    assert "Flatpak carries" in capsys.readouterr().out


def test_the_export_extra_goes_in_a_venv_of_the_apps_own_data(tmp_path, monkeypatch):
    """/app is read-only, so `pip install vinowhisper[export]` into it cannot work."""
    monkeypatch.setattr(sandbox, "INFO", tmp_path / "absent")
    assert len(wizard.export_extra_commands()) == 1
    _info(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EXPORT_ENV_DIR", tmp_path / "export-env")
    declared = _toml("pyproject.toml")["project"]["optional-dependencies"]["export"]
    monkeypatch.setattr(
        "importlib.metadata.requires",
        lambda name: ["flask", *(f'{dep}; extra == "export"' for dep in declared)],
    )
    venv, install = wizard.export_extra_commands()
    assert venv == [
        sys.executable,
        "-m",
        "venv",
        "--system-site-packages",
        str(tmp_path / "export-env"),
    ]
    assert install[0] == str(tmp_path / "export-env/bin/python")
    assert install[-len(declared) :] == declared
    assert wizard.TORCH_CPU_INDEX in install
    assert wizard.optimum_cli() is None
    cli = tmp_path / "export-env/bin/optimum-cli"
    cli.parent.mkdir(parents=True)
    cli.touch()
    assert wizard.optimum_cli() == str(cli)


# The update command


def test_only_a_release_tag_is_taken():
    for tag in ("v1", "v10", "v10.1.2"):
        assert flatpak_update.release_tag(tag), tag
    for tag in ("", "v", "10", "v1.", "v1..2", "v1.2-rc1", "v1/../x", "v1;rm", "V1", "v1\n"):
        assert not flatpak_update.release_tag(tag), tag
    assert not flatpak_update.release_tag("v" + "1" * 32)


def test_a_build_is_the_release_its_version_says():
    assert flatpak_update.same_version("v1.8.0", "1.8.0")
    assert flatpak_update.same_version("v2", "2.0.0")
    assert not flatpak_update.same_version("v1.8.0", "1.7.0")
    assert not flatpak_update.same_version("v1.8.0", "dev")


def test_the_update_goes_to_the_installation_this_build_runs_from(tmp_path):
    home = tmp_path / "home"
    path = f"{home}/.local/share/flatpak/app/{APP}/x86_64/stable/abc/files"
    assert flatpak_update.installation(path, APP, home) == "--user"
    system = f"/var/lib/flatpak/app/{APP}/x86_64/stable/abc/files"
    assert flatpak_update.installation(system, APP, home) == "--system"
    with pytest.raises(flatpak_update.UpdateError, match="can't update"):
        flatpak_update.installation(f"/mnt/extra/app/{APP}/x86_64/stable/abc/files", APP, home)
    with pytest.raises(flatpak_update.UpdateError, match="Can't tell"):
        flatpak_update.installation("/app", APP, home)


def test_exactly_one_checksum_line_must_name_the_bundle():
    digest = "a" * 64
    name = flatpak_update.BUNDLE
    assert flatpak_update.expected_sum(f"{digest}  {name}\n", name) == digest
    assert flatpak_update.expected_sum(f"{digest} *{name}\n", name) == digest
    assert flatpak_update.expected_sum(f"{digest}  other.flatpak\n", name) is None
    assert flatpak_update.expected_sum(f"{digest}  {name}\n{'b' * 64}  {name}\n", name) is None
    assert flatpak_update.expected_sum(f"{'A' * 64}  {name}\n", name) is None
    assert flatpak_update.expected_sum("", name) is None


class _Response:
    def __init__(self, body: bytes, status: int = 200):
        self.body, self.status_code = body, status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code != 200:
            import requests

            raise requests.HTTPError(f"{self.status_code} Client Error")

    def iter_content(self, chunk_size):
        for at in range(0, len(self.body), chunk_size):
            yield self.body[at : at + chunk_size]


@pytest.fixture
def release(tmp_path, monkeypatch):
    home = _info(tmp_path, monkeypatch)
    bundle = b"a flatpak bundle, as far as this test goes"
    state = SimpleNamespace(
        home=home,
        bundle=bundle,
        sums=f"{hashlib.sha256(bundle).hexdigest()}  {flatpak_update.BUNDLE}\n".encode(),
        host=[],
        installed=None,
        version="1.8.0",
        restarted=[],
        urls=[],
    )

    def get(url, **_kwargs):
        state.urls.append(url)
        return _Response(state.sums if url.endswith(".sha256") else state.bundle)

    def host_flatpak(*args):
        state.host.append(args)
        if args[0] == "install":
            state.installed = Path(args[-1]).read_bytes()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0, stdout=f"vinowhisper-tray {state.version}\n", stderr=""
        )

    monkeypatch.setattr(flatpak_update, "host_flatpak", host_flatpak)
    monkeypatch.setattr(flatpak_update, "restart", lambda: state.restarted.append(True))
    state.get = get
    return state


def test_an_update_checks_the_bundle_installs_it_and_restarts(release):
    flatpak_update.update("v1.8.0", release.get)
    base = "https://github.com/karanshukla/vinoWhisper/releases/download/v1.8.0"
    assert release.urls == [
        f"{base}/vinowhisper-x86_64.flatpak.sha256",
        f"{base}/vinowhisper-x86_64.flatpak",
    ]
    assert release.installed == release.bundle
    install, run = release.host
    cache = release.home / f".var/app/{APP}/cache/vinowhisper-update"
    assert install == ("install", "--user", "-y", "--bundle", str(cache / flatpak_update.BUNDLE))
    assert run == ("run", "--user", "--command=vinowhisper-tray", APP, "--version")
    assert release.restarted == [True]
    assert not cache.exists()


def test_a_bundle_that_does_not_match_its_checksum_is_never_installed(release):
    release.bundle = b"something else entirely"
    with pytest.raises(flatpak_update.UpdateError, match="Checksum verification failed"):
        flatpak_update.update("v1.8.0", release.get)
    assert release.host == [] and release.restarted == []
    assert not (release.home / f".var/app/{APP}/cache/vinowhisper-update").exists()


def test_a_bundle_of_another_version_is_reported_and_nothing_restarts(release):
    """Flatpak installs an older build over a newer one without a word."""
    release.version = "1.7.0"
    with pytest.raises(flatpak_update.UpdateError, match="is vinoWhisper 1.7.0"):
        flatpak_update.update("v1.8.0", release.get)
    assert release.restarted == []


def test_a_build_from_a_checkout_does_not_update_itself(tmp_path, monkeypatch, release):
    _info(tmp_path, monkeypatch, branch="master")
    with pytest.raises(flatpak_update.UpdateError, match="master build, not a release"):
        flatpak_update.update("v1.8.0", release.get)
    assert release.urls == []


def test_outside_the_flatpak_the_command_says_what_updates_instead(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "INFO", tmp_path / "absent")
    assert flatpak_update.main(["v1.8.0"]) == 1
    last = capsys.readouterr().err.strip().splitlines()[-1]
    assert last.startswith("✗ ") and "vinowhisper-upgrade" in last


def test_the_tag_comes_from_the_update_button(release, monkeypatch):
    monkeypatch.setenv("VINO_UPDATE_TAG", "v1.8.0")
    import requests

    monkeypatch.setattr(requests, "get", release.get)
    assert flatpak_update.main([]) == 0
    assert release.restarted == [True]
    monkeypatch.setenv("VINO_UPDATE_TAG", "latest")
    assert flatpak_update.main([]) == 1


def test_a_download_the_server_refuses_is_an_error(release):
    release.get = lambda url, **kw: _Response(b"", status=404)
    with pytest.raises(flatpak_update.UpdateError, match="Download failed.*404"):
        flatpak_update.update("v1.8.0", release.get)
    assert release.host == []


def test_a_checksum_file_for_some_other_bundle_is_refused(release):
    release.sums = release.sums.replace(b"vinowhisper-x86_64", b"something-else")
    with pytest.raises(flatpak_update.UpdateError, match="does not name it"):
        flatpak_update.update("v1.8.0", release.get)
    assert release.host == []


# The manifest


def _toml(name: str) -> dict:
    return tomllib.loads((ROOT / name).read_text(encoding="utf-8"))


def _manifest() -> str:
    return (PACKAGING / f"{APP}.yml").read_text(encoding="utf-8")


def test_the_flatpak_installs_what_uv_lock_pins():
    """scripts/flatpak_requirements.py writes the file; this fails while it is stale."""
    locked = {package["name"]: package["version"] for package in _toml("uv.lock")["package"]}
    pinned = {}
    for line in (PACKAGING / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if "==" in line and not line.startswith((" ", "#")):
            name, _, rest = line.partition("==")
            pinned[name] = rest.split()[0]
    for name, version in pinned.items():
        assert locked[name] == version, f"{name}: re-run scripts/flatpak_requirements.py"
    declared = [
        dep.split(">")[0].split("=")[0].split("<")[0].strip()
        for dep in _toml("pyproject.toml")["project"]["dependencies"]
    ]
    assert set(declared) <= set(pinned)
    assert "setuptools" in pinned, "the package itself is built in the Flatpak"
    assert "torch" not in pinned, "the export extra is installed by setup, not bundled"


def test_every_pinned_package_is_checked_by_hash():
    text = (PACKAGING / "requirements.txt").read_text(encoding="utf-8")
    blocks = [block for block in text.split("\n") if "==" in block and not block.startswith("#")]
    assert blocks and all(block.endswith("\\") for block in blocks)
    assert "--require-hashes" in _manifest()


def test_the_manifest_is_named_for_the_app_id_the_tray_registers():
    """The portal keys the shortcuts by app id, and Flatpak's is the manifest's."""
    tray = (ROOT / "tray/src/main.rs").read_text(encoding="utf-8")
    assert f'pub const APP_ID: &str = "{APP}";' in tray
    assert f"app-id: {APP}\n" in _manifest()
    assert f"<id>{APP}</id>" in (PACKAGING / f"{APP}.metainfo.xml").read_text(encoding="utf-8")


def test_the_bundle_name_is_the_one_the_workflow_uploads():
    workflow = (ROOT / ".github/workflows/flatpak.yml").read_text(encoding="utf-8")
    assert f"BUNDLE: {flatpak_update.BUNDLE}" in workflow
    assert f"APP: {APP}" in workflow


def test_the_bundled_python_is_one_the_package_supports():
    """The runtime's own is 3.14, which requires-python excludes."""
    requires = _toml("pyproject.toml")["project"]["requires-python"]
    assert requires == ">=3.11,<3.14"
    assert "cpython-3.13." in _manifest()
    assert "python3.13/site-packages" in _manifest()
