"""install.sh --uninstall, run for real against a scratch home with systemctl, uv and the
tray stubbed on PATH. Nothing outside tmp_path is touched."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    log = tmp_path / "calls.log"
    stubs = tmp_path / "stubs"
    bin_dir = home / ".local/bin"
    _stub(stubs / "systemctl", f'echo "systemctl $*" >> "{log}"')
    _stub(
        stubs / "uv",
        f'echo "uv $*" >> "{log}"\n'
        'if [ "$1 $2" = "tool list" ]; then echo "vinowhisper v1.5.0"; fi\n'
        'if [ "$1 $2" = "tool uninstall" ]; then rm -f "$HOME"/.local/bin/vinowhisper-caption; fi',
    )
    _stub(bin_dir / "vinowhisper-tray", f'echo "tray $*" >> "{log}"')
    _stub(bin_dir / "vinowhisper-gui", "")
    _stub(bin_dir / "vinowhisper-upgrade", "")
    _stub(bin_dir / "vinowhisper-caption", "")
    _stub(bin_dir / "ovfetch", "")
    venv = home / "src/vinoWhisper/.venv/bin"
    _stub(venv / "vinowhisper-setup", "")
    (bin_dir / "vinowhisper-setup").symlink_to(venv / "vinowhisper-setup")
    completions = home / ".local/share/bash-completion/completions"
    completions.mkdir(parents=True)
    (completions / "vinowhisper-caption").symlink_to(
        home / "src/vinoWhisper/scripts/vinowhisper-completion.bash"
    )
    (completions / "other-tool").symlink_to("/usr/share/other-tool")

    data, cache, config = home / ".local/share", home / ".cache", home / ".config"
    _touch(data / "vinowhisper/models/whisper-small.en-ov-stateful/openvino_encoder_model.xml")
    _touch(data / "vinowhisper/models/source/openai--whisper-small.en@e8/config.json")
    _touch(data / "vinowhisper/models/silero-vad/silero_vad.onnx")
    _touch(data / "vinowhisper/transcripts/2026-11-11-101500.vwsession")
    _touch(data / "applications/io.github.karanshukla.vinowhisper.desktop")
    _touch(data / "applications/io.github.karanshukla.vinowhisper.sessions.desktop")
    _touch(data / "applications/org.example.other.desktop")
    _touch(cache / "vinowhisper/cleanup-cache/NPU/blob")
    _touch(cache / "vinowhisper/export-tmp/tmpabc/partial.bin")
    _touch(cache / "ze_intel_npu_cache/blob")
    _touch(cache / "huggingface/hub/x")
    _touch(config / "vinowhisper/gui.json", "{}")
    _touch(config / "systemd/user/vinowhisper-server.service")
    _touch(config / "systemd/user/vinowhisper-server.socket")
    _touch(config / "systemd/user/other.service")
    _touch(config / "autostart/io.github.karanshukla.vinowhisper.desktop")
    _touch(home / ".local/state/vinowhisper/sessions.json", "{}")
    _touch(home / ".local/lib/ovfetch-npu/lib.so")

    env = {
        "HOME": str(home),
        "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "LANG": "C",
    }
    return home, env, log


def _run(env, *args):
    return subprocess.run(
        ["bash", str(ROOT / "install.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_uninstall_deletes_the_models_and_keeps_the_users_data(home):
    home, env, log = home
    done = _run(env, "--uninstall")
    assert done.returncode == 0, done.stderr
    data, cache, config = home / ".local/share", home / ".cache", home / ".config"

    gone = [
        data / "vinowhisper/models",
        cache / "vinowhisper",
        config / "systemd/user/vinowhisper-server.service",
        config / "systemd/user/vinowhisper-server.socket",
        config / "autostart/io.github.karanshukla.vinowhisper.desktop",
        data / "applications/io.github.karanshukla.vinowhisper.desktop",
        data / "applications/io.github.karanshukla.vinowhisper.sessions.desktop",
        data / "bash-completion/completions/vinowhisper-caption",
    ] + [
        home / ".local/bin" / name
        for name in (
            "vinowhisper-tray",
            "vinowhisper-gui",
            "vinowhisper-upgrade",
            "vinowhisper-caption",
            "vinowhisper-setup",
        )
    ]
    assert [p for p in gone if p.exists() or p.is_symlink()] == []

    kept = [
        data / "vinowhisper/transcripts/2026-11-11-101500.vwsession",
        data / "applications/org.example.other.desktop",
        data / "bash-completion/completions/other-tool",
        cache / "ze_intel_npu_cache/blob",
        cache / "huggingface/hub/x",
        config / "vinowhisper/gui.json",
        config / "systemd/user/other.service",
        home / ".local/state/vinowhisper/sessions.json",
        home / ".local/lib/ovfetch-npu/lib.so",
        home / ".local/bin/ovfetch",
        home / "src/vinoWhisper/.venv/bin/vinowhisper-setup",
    ]
    assert [p for p in kept if not (p.exists() or p.is_symlink())] == []

    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls.index("tray quit") < calls.index("tray --uninstall")
    assert (
        "systemctl --user disable --now vinowhisper-server.socket vinowhisper-server.service"
        in calls
    )
    assert "systemctl --user daemon-reload" in calls
    assert "uv tool uninstall vinowhisper" in calls
    assert "Kept your saved sessions" in done.stdout


def test_uninstall_honours_xdg_dirs(home, tmp_path):
    home, env, _ = home
    data, cache = tmp_path / "xdg-data", tmp_path / "xdg-cache"
    _touch(data / "vinowhisper/models/whisper-small-ov/openvino_encoder_model.xml")
    _touch(data / "vinowhisper/transcripts/a.txt")
    _touch(cache / "vinowhisper/cleanup-cache/blob")
    env = {**env, "XDG_DATA_HOME": str(data), "XDG_CACHE_HOME": str(cache)}
    assert _run(env, "--uninstall").returncode == 0
    assert not (data / "vinowhisper/models").exists()
    assert not (cache / "vinowhisper").exists()
    assert (data / "vinowhisper/transcripts/a.txt").is_file()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_installers_clone_goes_unless_it_has_changes(home):
    home, env, _ = home
    src = home / ".local/share/vinowhisper/src"
    _touch(src / "pyproject.toml", 'name = "vinowhisper"\n')
    _git(src, "init", "-q")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "init")
    _touch(src / "pyproject.toml", 'name = "vinowhisper"\n# mine\n')
    done = _run(env, "--uninstall")
    assert done.returncode == 0, done.stderr
    assert src.is_dir() and "uncommitted changes" in done.stdout
    _git(src, "checkout", "-q", "--", ".")
    assert _run(env, "--uninstall").returncode == 0
    assert not src.exists()


def test_a_second_uninstall_finds_nothing_and_still_succeeds(home):
    home, env, _ = home
    assert _run(env, "--uninstall").returncode == 0
    done = _run(env, "--uninstall")
    assert done.returncode == 0, done.stderr


def test_the_help_text_names_uninstall():
    done = subprocess.run(
        ["bash", str(ROOT / "install.sh"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--uninstall" in done.stdout
    assert "Saved sessions, settings and exported documents stay." in done.stdout
    assert "set -euo pipefail" not in done.stdout
