import contextlib
import fcntl
import os
import shutil
import socket
import subprocess  # nosec B404 - fixed argv, never a shell
import sys
import threading
import time
from pathlib import Path

from . import config

START_WAIT_S = 20.0
STOP_WAIT_S = 10.0
_POLL_S = 0.05
LOG_MAX_BYTES = 1 << 20
_LOG_TAIL_LINES = 6

UNITS = ("vinowhisper-server.socket", "vinowhisper-server.service")


class StartError(Exception):
    pass


def running(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(config.CONNECT_TIMEOUT_S)
        try:
            probe.connect(str(path))
        except OSError:
            return False
    return True


def server_argv() -> list[str]:
    return [sys.executable, "-m", "vinowhisper.server"]


def _open_log() -> int:
    path = config.SERVER_LOG
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        if path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".old"))
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)


def _spawn() -> subprocess.Popen:
    log = _open_log()
    try:
        process = subprocess.Popen(  # nosec B603 - server_argv is this interpreter
            server_argv(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            cwd="/",
        )
    finally:
        os.close(log)
    # Reaped here while this client lives, by init after it.
    threading.Thread(target=process.wait, name="server-reaper", daemon=True).start()
    return process


def log_tail(lines: int = _LOG_TAIL_LINES) -> str:
    try:
        text = config.SERVER_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _private_dir(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)


def ensure(path: Path | None = None, wait_s: float = START_WAIT_S) -> None:
    path = path or config.server_socket()
    if path is None:
        raise StartError(
            "XDG_RUNTIME_DIR is not set, so there is nowhere private for the server socket; "
            "run this from a login session"
        )
    if running(path):
        return
    try:
        _private_dir(path.parent)
        lock = os.open(path.with_name("server.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as exc:
        raise StartError(f"could not use {path.parent}: {exc}") from exc
    try:
        # One starter: two clients at once would otherwise race two servers to the socket.
        fcntl.flock(lock, fcntl.LOCK_EX)
        if running(path):
            return
        try:
            process = _spawn()
        except OSError as exc:
            raise StartError(f"could not start vinowhisper-server for {path}: {exc}") from exc
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if running(path):
                return
            if process.poll() is not None:
                break
            time.sleep(_POLL_S)
        else:
            raise StartError(
                f"vinowhisper-server did not listen on {path} within {wait_s:.0f}s; "
                f"its log is {config.SERVER_LOG}"
            )
        tail = log_tail()
        raise StartError(
            f"vinowhisper-server exited as it started (status {process.returncode}); "
            f"its log is {config.SERVER_LOG}" + (f"\n{tail}" if tail else "")
        )
    finally:
        os.close(lock)


def stop(path: Path | None = None, wait_s: float = STOP_WAIT_S) -> bool:
    import requests

    from . import client

    path = path or config.server_socket()
    if path is None or not running(path):
        return True
    try:
        with client.session(path, autostart=False) as session:
            response = session.post(
                f"{client.BASE_URL}/quit",
                timeout=(config.CONNECT_TIMEOUT_S, config.CONNECT_TIMEOUT_S),
            )
            response.raise_for_status()
            activated = bool(response.json().get("socket_activated"))
    except (requests.RequestException, ValueError):
        # A server from before /quit, on a socket unit not yet retired.
        return _systemctl("stop", UNITS[1])
    if activated:
        # A leftover socket unit still listens, so a probe would start the server again.
        return True
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not running(path):
            return True
        time.sleep(_POLL_S)
    return False


def _systemctl(*args: str) -> bool:
    if shutil.which("systemctl") is None:
        return False
    try:
        done = subprocess.run(  # nosec B603 B607 - fixed argv
            ["systemctl", "--user", *args], check=False, capture_output=True
        )
    except OSError:
        return False
    return done.returncode == 0


def unit_dirs() -> list[Path]:
    # Setup wrote to ~/.config whatever XDG_CONFIG_HOME said.
    dirs = [Path.home() / ".config/systemd/user", config._config_home() / "systemd/user"]
    return list(dict.fromkeys(dirs))


def old_units() -> list[Path]:
    return [d / unit for d in unit_dirs() for unit in UNITS if (d / unit).is_file()]


def _unit_device(service: Path) -> str | None:
    try:
        lines = service.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("ExecStart="):
            words = line.split()
            if "--device" in words[:-1]:
                return words[words.index("--device") + 1]
    return None


def retire_units() -> list[Path]:
    found = old_units()
    if not found:
        return []
    # The unit carried the device; the server reads it from server.json now.
    for unit in found:
        device = _unit_device(unit) if unit.name == UNITS[1] else None
        if device and device != config.DEFAULT_DEVICE and not config.SERVER_FILE.exists():
            with contextlib.suppress(OSError):
                config.save_device(device)
    _systemctl("disable", "--now", *UNITS)
    for unit in found:
        unit.unlink(missing_ok=True)
    _systemctl("daemon-reload")
    _systemctl("reset-failed", *UNITS)
    return found
