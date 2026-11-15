import contextlib
import os
import socket
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import config, sandbox

# Every client imports this module and almost none starts a server, so what only a start
# or a stop needs (subprocess, fcntl, shutil, shlex, threading) is imported where used.
if TYPE_CHECKING:
    import subprocess

START_WAIT_S = 20.0
STOP_WAIT_S = 10.0
# How long a client can wait past the moment the server binds its socket.
_POLL_S = 0.002
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


def server_commands() -> list[list[str]]:
    argv = server_argv()
    if sandbox.flatpak_id() is None:
        return [argv]
    # In the Flatpak the server gets a sandbox of its own, and one started inside this
    # one is the fallback where the portal that makes them is missing.
    return [sandbox.instance_argv(argv), argv]


def _open_log() -> int:
    path = config.SERVER_LOG
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        if path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".old"))
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)


def _spawn(argv: list[str]) -> "subprocess.Popen":
    import subprocess  # nosec B404 - fixed argv, never a shell
    import threading

    log = _open_log()
    try:
        process = subprocess.Popen(  # nosec B603 - server_commands is this interpreter
            argv,
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
    import fcntl

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
        deadline = time.monotonic() + wait_s
        status: int | None = None
        for argv in server_commands():
            try:
                process = _spawn(argv)
            except OSError as exc:
                raise StartError(f"could not start vinowhisper-server for {path}: {exc}") from exc
            while time.monotonic() < deadline:
                if running(path):
                    if argv[0] == sandbox.SPAWN:
                        # flatpak-spawn would hold this sandbox open; SIGKILL, since it forwards the rest.
                        process.kill()
                    return
                status = process.poll()
                if status is not None:
                    break
                time.sleep(_POLL_S)
            else:
                raise StartError(
                    f"vinowhisper-server did not listen on {path} within {wait_s:.0f}s; "
                    f"its log is {config.SERVER_LOG}"
                )
        tail = log_tail()
        raise StartError(
            f"vinowhisper-server exited as it started (status {status}); "
            f"its log is {config.SERVER_LOG}" + (f"\n{tail}" if tail else "")
        )
    finally:
        os.close(lock)


def _quit(path: Path) -> bool:
    # The standard library, not the client: stopping must not cost the imports a
    # transcription needs (requests, numpy), since the tray waits on it to switch language.
    import http.client
    import json

    class Connection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(str(path))

    connection = Connection("vinowhisper", timeout=config.CONNECT_TIMEOUT_S)
    try:
        connection.request("POST", "/quit", headers={"Content-Length": "0"})
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise ValueError(f"/quit answered {response.status}")
        return bool(json.loads(body).get("socket_activated"))
    except http.client.HTTPException as exc:
        raise ValueError(str(exc)) from exc
    finally:
        connection.close()


def stop(path: Path | None = None, wait_s: float = STOP_WAIT_S) -> bool:
    path = path or config.server_socket()
    if path is None or not running(path):
        return True
    try:
        activated = _quit(path)
    except (OSError, ValueError, AttributeError):
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


def stop_command() -> int:
    for unit in retire_units():
        print(f"[vinowhisper-server] removed {unit}", file=sys.stderr)
    if stop():
        return 0
    print("[vinowhisper-server] the running server did not stop", file=sys.stderr)
    return 1


def server_main(argv: list[str] | None = None) -> int:
    # vinowhisper-server's entry point. --stop is answered here, before the server module
    # and what it imports (Flask, numpy) are loaded.
    args = sys.argv[1:] if argv is None else argv
    if args == ["--stop"]:
        return stop_command()
    from . import server

    return server.main(argv)


def _systemctl(*args: str) -> bool:
    import shutil
    import subprocess  # nosec B404 - fixed argv, never a shell

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
            import shlex

            try:
                words = shlex.split(line.removeprefix("ExecStart="))
            except ValueError:
                return None
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
