import os
import socket
import stat
from pathlib import Path

from . import config


def _systemd_socket_fd() -> int | None:
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return None
    try:
        listen_fds = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError:
        return None
    return 3 if listen_fds >= 1 else None  # SD_LISTEN_FDS_START


class SocketError(Exception):
    pass


def _inherited_socket(fd: int) -> socket.socket:
    sock = socket.socket(fileno=fd)
    if sock.family != socket.AF_UNIX:
        sock.detach()
        raise SocketError(
            "systemd passed a non-Unix socket: the installed socket unit predates the move "
            "off TCP. Re-run `vinowhisper-setup` to rewrite it."
        )
    return sock


def _bind_socket(path: Path) -> socket.socket:
    directory = path.parent
    directory.mkdir(mode=0o700, exist_ok=True)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SocketError(f"{directory} is not a directory owned by you; refusing to use it")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(directory, 0o700)

    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise SocketError(f"{path} exists and is not a socket; refusing to replace it")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(str(path))
            except OSError:
                path.unlink()  # left behind by a server that is gone
            else:
                raise SocketError(f"a server is already listening on {path}")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        sock.bind(str(path))
    except OSError:
        sock.close()
        raise
    finally:
        os.umask(old_umask)
    sock.listen(socket.SOMAXCONN)
    return sock


def listening_socket() -> socket.socket:
    fd = _systemd_socket_fd()
    if fd is not None:
        return _inherited_socket(fd)
    path = config.server_socket()
    if path is None:
        raise SocketError(
            "XDG_RUNTIME_DIR is not set, so there is nowhere private to put the server "
            "socket. Start it from a login session, or let systemd do it."
        )
    return _bind_socket(path)
