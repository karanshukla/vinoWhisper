"""The server's socket: private to the user, and never anywhere but $XDG_RUNTIME_DIR."""

import os
import shutil
import socket
import stat
import tempfile
from pathlib import Path

import pytest

from vinowhisper import config, listen


@pytest.fixture
def runtime_dir(monkeypatch):
    # Not tmp_path: AF_UNIX paths are limited to 107 bytes.
    path = Path(tempfile.mkdtemp(prefix="vw-"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(path))
    monkeypatch.delenv("LISTEN_PID", raising=False)
    monkeypatch.delenv("LISTEN_FDS", raising=False)
    yield path
    shutil.rmtree(path)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_binds_0600_in_a_0700_dir(runtime_dir):
    old_umask = os.umask(0o022)
    try:
        with listen.listening_socket() as sock:
            path = runtime_dir / config.SERVER_SOCKET_RELPATH
            assert sock.family == socket.AF_UNIX
            assert sock.getsockname() == str(path)
            assert stat.S_ISSOCK(os.lstat(path).st_mode)
            assert _mode(path) == 0o600
            assert _mode(path.parent) == 0o700
    finally:
        assert os.umask(old_umask) == 0o022, "the umask is restored"


def test_answers_connections(runtime_dir):
    with (
        listen.listening_socket() as sock,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer,
    ):
        peer.connect(sock.getsockname())
        accepted, _ = sock.accept()
        accepted.close()


def test_refuses_to_start_without_xdg_runtime_dir(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("LISTEN_PID", raising=False)
    with pytest.raises(listen.SocketError, match="XDG_RUNTIME_DIR"):
        listen.listening_socket()


def test_tightens_a_loose_directory(runtime_dir):
    directory = runtime_dir / "vinowhisper"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with listen.listening_socket():
        assert _mode(directory) == 0o700


def test_refuses_a_symlinked_directory(runtime_dir):
    elsewhere = runtime_dir / "elsewhere"
    elsewhere.mkdir()
    (runtime_dir / "vinowhisper").symlink_to(elsewhere)
    with pytest.raises(listen.SocketError, match="not a directory owned by you"):
        listen.listening_socket()


def test_replaces_a_stale_socket(runtime_dir):
    with listen.listening_socket():
        pass  # closed without unlinking, as a killed server leaves it
    with listen.listening_socket() as sock:
        assert _mode(Path(sock.getsockname())) == 0o600


def test_refuses_to_steal_a_live_socket(runtime_dir):
    with (
        listen.listening_socket(),
        pytest.raises(listen.SocketError, match="already listening"),
    ):
        listen.listening_socket()


def test_refuses_to_replace_a_regular_file(runtime_dir):
    path = runtime_dir / config.SERVER_SOCKET_RELPATH
    path.parent.mkdir(mode=0o700)
    path.write_text("not a socket")
    with pytest.raises(listen.SocketError, match="not a socket"):
        listen.listening_socket()
    assert path.read_text() == "not a socket"


def _activate(monkeypatch, sock: socket.socket) -> None:
    """What systemd does, minus fd 3: a listening socket handed to the server."""
    monkeypatch.setattr(listen, "_systemd_socket_fd", lambda: os.dup(sock.fileno()))


def test_listen_fds_is_only_for_this_process(monkeypatch):
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setenv("LISTEN_PID", str(os.getpid() + 1))
    assert listen._systemd_socket_fd() is None
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    assert listen._systemd_socket_fd() == 3


def test_uses_the_unix_socket_systemd_passes(runtime_dir, monkeypatch):
    path = runtime_dir / "activated.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as original:
        original.bind(str(path))
        original.listen()
        _activate(monkeypatch, original)
        with listen.listening_socket() as sock:
            assert sock.getsockname() == str(path)
    assert not (runtime_dir / "vinowhisper").exists(), "nothing bound of its own"


def test_refuses_a_tcp_socket_from_an_old_unit(runtime_dir, monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as original:
        original.bind(("127.0.0.1", 0))
        original.listen()
        _activate(monkeypatch, original)
        with pytest.raises(listen.SocketError, match="vinowhisper-setup"):
            listen.listening_socket()
