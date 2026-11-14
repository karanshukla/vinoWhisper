"""Starting and stopping the server with no service manager.

A systemd socket unit used to hold the socket and start the server on the first
connection. Now whichever client finds nobody listening starts it, and /quit stops it.
The server here is a thread and "spawning" it is a function call: nothing is executed.
"""

import json
import shutil
import socket
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
import requests

from vinowhisper import client, config, launch


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self):
        return "unix"

    def log_message(self, format, *args):
        pass

    def _answer(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._answer({"status": "ok"})

    def do_POST(self):
        if self.path != "/quit" or not self.server.quits:
            self.send_error(404)
            return
        self._answer({"stopping": True, "socket_activated": self.server.activated})
        if not self.server.activated:
            threading.Thread(target=self.server.stop, daemon=True).start()


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    quits = True
    activated = False

    def stop(self):
        self.shutdown()
        self.server_close()


class _Process:
    returncode = None

    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


@pytest.fixture
def socket_path(monkeypatch):
    # Not tmp_path: AF_UNIX paths are limited to 107 bytes.
    root = Path(tempfile.mkdtemp(prefix="vw-"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    yield root / config.SERVER_SOCKET_RELPATH
    shutil.rmtree(root)


@pytest.fixture
def servers(socket_path, monkeypatch):
    started = []

    def spawn(argv):
        instance = _Server(str(socket_path), _Handler)
        threading.Thread(target=instance.serve_forever, daemon=True).start()
        started.append(instance)
        return _Process()

    monkeypatch.setattr(launch, "_spawn", spawn)
    yield started
    for instance in started:
        instance.stop()


def test_the_first_connection_starts_the_server(servers, socket_path):
    assert not launch.running(socket_path)
    assert client.TranscriptionClient().wait_ready() == {"status": "ok"}
    assert len(servers) == 1
    assert launch.running(socket_path)


def test_a_running_server_is_not_started_again(servers, socket_path):
    launch.ensure()
    launch.ensure()
    client.TranscriptionClient().wait_ready()
    assert len(servers) == 1


def test_the_socket_directory_is_private(servers, socket_path):
    launch.ensure()
    assert socket_path.parent.stat().st_mode & 0o777 == 0o700


def test_a_socket_left_by_a_server_that_exited_starts_a_new_one(servers, socket_path):
    """The idle exit leaves the path behind; refused is the same as absent."""
    socket_path.parent.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX) as stale:
        stale.bind(str(socket_path))
    assert socket_path.exists() and not launch.running(socket_path)
    socket_path.unlink()  # the real server's listen._bind_socket does this
    assert client.TranscriptionClient().wait_ready() == {"status": "ok"}
    assert len(servers) == 1


def test_a_server_that_exits_as_it_starts_is_an_error_with_its_log(monkeypatch, socket_path):
    config.SERVER_LOG.write_text("[vinowhisper-server] no export at /models\n")
    monkeypatch.setattr(launch, "_spawn", lambda argv: _Process(returncode=1))
    with pytest.raises(launch.StartError, match="no export at /models") as raised:
        launch.ensure()
    assert str(config.SERVER_LOG) in str(raised.value)


def test_a_server_that_never_listens_times_out(monkeypatch, socket_path):
    monkeypatch.setattr(launch, "_spawn", lambda argv: _Process())
    with pytest.raises(launch.StartError, match="did not listen"):
        launch.ensure(wait_s=0.2)


def test_a_failed_start_reaches_the_caller_as_a_connection_error(socket_path):
    """conftest refuses every spawn, which is what a missing interpreter looks like."""
    with pytest.raises(requests.ConnectionError, match="could not start vinowhisper-server"):
        client.TranscriptionClient().wait_ready()


def test_without_a_runtime_dir_nothing_is_started(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(launch, "_spawn", lambda argv: pytest.fail("started a server"))
    with pytest.raises(launch.StartError, match="XDG_RUNTIME_DIR"):
        launch.ensure()


def test_a_session_can_decline_to_start_the_server(monkeypatch, socket_path):
    monkeypatch.setattr(launch, "_spawn", lambda argv: pytest.fail("started a server"))
    with (
        client.session(autostart=False) as session,
        pytest.raises(requests.ConnectionError, match="server.sock"),
    ):
        session.get(f"{client.BASE_URL}/health", timeout=1)


def test_stop_asks_the_server_to_quit_and_waits_for_it(servers, socket_path):
    launch.ensure()
    assert launch.stop() is True
    assert not launch.running(socket_path)


def test_stop_with_no_server_starts_none(monkeypatch, socket_path):
    monkeypatch.setattr(launch, "_spawn", lambda argv: pytest.fail("started a server"))
    assert launch.stop() is True


def test_stop_does_not_probe_a_socket_unit_that_would_start_it_again(servers, socket_path):
    """characterization: under a leftover socket unit the socket outlives the server, so
    waiting for it to go quiet would never end, and each probe would be a new activation."""
    launch.ensure()
    servers[0].activated = True
    assert launch.stop(wait_s=0.2) is True
    assert launch.running(socket_path)


def test_a_server_from_before_quit_is_stopped_through_its_unit(servers, socket_path, monkeypatch):
    launch.ensure()
    servers[0].quits = False
    calls = []
    monkeypatch.setattr(launch, "_systemctl", lambda *args: calls.append(args) or True)
    assert launch.stop() is True
    assert calls == [("stop", "vinowhisper-server.service")]


def test_the_log_is_moved_aside_once_it_is_large(monkeypatch):
    monkeypatch.setattr(launch, "LOG_MAX_BYTES", 10)
    config.SERVER_LOG.write_text("x" * 11)
    fd = launch._open_log()
    try:
        assert config.SERVER_LOG.stat().st_size == 0
        assert config.SERVER_LOG.with_name(config.SERVER_LOG.name + ".old").stat().st_size == 11
        assert config.SERVER_LOG.stat().st_mode & 0o777 == 0o600
    finally:
        import os

        os.close(fd)


def test_the_server_is_this_interpreters_module(monkeypatch):
    import sys

    assert launch.server_argv() == [sys.executable, "-m", "vinowhisper.server"]


def test_retiring_units_moves_the_device_and_removes_them(tmp_path, monkeypatch):
    units = tmp_path / "systemd/user"
    units.mkdir(parents=True)
    service = units / "vinowhisper-server.service"
    service.write_text("[Service]\nExecStart=/venv/bin/vinowhisper-server --device GPU\n")
    (units / "vinowhisper-server.socket").write_text("[Socket]\n")
    (units / "something-else.service").write_text("")
    calls = []
    monkeypatch.setattr(launch, "unit_dirs", lambda: [units])
    monkeypatch.setattr(launch, "_systemctl", lambda *args: calls.append(args) or True)
    assert sorted(path.name for path in launch.retire_units()) == sorted(launch.UNITS)
    assert [path.name for path in units.iterdir()] == ["something-else.service"]
    assert config.load_device() == "GPU"
    assert calls == [
        ("disable", "--now", *launch.UNITS),
        ("daemon-reload",),
        ("reset-failed", *launch.UNITS),
    ]
    assert launch.retire_units() == []
    assert len(calls) == 3


def test_a_saved_device_is_not_overwritten_by_an_old_unit(tmp_path, monkeypatch):
    units = tmp_path / "systemd/user"
    units.mkdir(parents=True)
    (units / "vinowhisper-server.service").write_text(
        "[Service]\nExecStart=/venv/bin/python -m vinowhisper.server --device CPU\n"
    )
    config.save_device("NPU")
    monkeypatch.setattr(launch, "unit_dirs", lambda: [units])
    monkeypatch.setattr(launch, "_systemctl", lambda *args: True)
    launch.retire_units()
    assert config.load_device() == "NPU"


def test_the_device_file_falls_back_to_auto(tmp_path):
    assert config.load_device(tmp_path / "missing.json") == config.DEFAULT_DEVICE
    broken = tmp_path / "broken.json"
    broken.write_text("[not an object")
    assert config.load_device(broken) == config.DEFAULT_DEVICE
    broken.write_text('{"device": 3}')
    assert config.load_device(broken) == config.DEFAULT_DEVICE
