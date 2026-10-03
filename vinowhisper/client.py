import codecs
import socket
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

from . import config

# The host part is never resolved: every http:// request in the session goes to the socket.
BASE_URL = "http://vinowhisper"


class _UnixConnection(HTTPConnection):
    def __init__(self, *args: Any, socket_path: str | None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._socket_path = socket_path

    def _new_conn(self) -> socket.socket:
        if self._socket_path is None:
            raise NewConnectionError(self, f"no server socket: {config.server_address()}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if isinstance(self.timeout, int | float):
            sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except TimeoutError as exc:
            sock.close()
            raise ConnectTimeoutError(self, f"{self._socket_path} timed out") from exc
        except OSError as exc:
            sock.close()
            raise NewConnectionError(self, f"{self._socket_path}: {exc}") from exc
        return sock


class _UnixConnectionPool(HTTPConnectionPool):
    ConnectionCls = _UnixConnection

    def __init__(self, socket_path: str | None) -> None:
        # maxsize: requests' own default, so concurrent callers reuse connections.
        super().__init__("vinowhisper", maxsize=10, socket_path=socket_path)


class UnixSocketAdapter(HTTPAdapter):
    def __init__(self, socket_path: Path | None) -> None:
        super().__init__()
        self._pool = _UnixConnectionPool(None if socket_path is None else str(socket_path))

    def get_connection_with_tls_context(
        self,
        request: requests.PreparedRequest,
        verify: bool | str | None,
        proxies: Mapping[str, str] | None = None,
        cert: tuple[str, str] | str | None = None,
    ) -> HTTPConnectionPool:
        return self._pool

    def get_connection(self, url: Any, proxies: Mapping[str, str] | None = None) -> Any:
        return self._pool

    def close(self) -> None:
        super().close()
        self._pool.close()


def session(socket_path: Path | None = None) -> requests.Session:
    result = requests.Session()
    # Never through a proxy, which requests would otherwise take from http_proxy.
    result.trust_env = False
    # No https:// adapter and nothing over TCP: a stray URL fails instead of leaving the machine.
    result.adapters.clear()
    result.mount("http://", UnixSocketAdapter(socket_path or config.server_socket()))
    return result


class TranscriptionClient:
    def __init__(self, socket_path: Path | None = None) -> None:
        # Not a default argument, which would read XDG_RUNTIME_DIR at import.
        self._session = session(socket_path)

    def wait_ready(self) -> dict:
        response = self._session.get(
            f"{BASE_URL}/health",
            timeout=(config.CONNECT_TIMEOUT_S, config.MODEL_LOAD_TIMEOUT_S),
        )
        response.raise_for_status()
        return response.json()

    def transcribe(self, samples: np.ndarray) -> tuple[str, float | None]:
        started_at = time.monotonic()
        response = self._session.post(
            f"{BASE_URL}/transcribe",
            data=samples.astype("<f4", copy=False).tobytes(),
            headers={"Content-Type": "application/octet-stream"},
            stream=True,
            timeout=(config.CONNECT_TIMEOUT_S, config.REQUEST_TIMEOUT_S),
        )
        response.raise_for_status()

        # Incremental: a chunk boundary can split a multi-byte character.
        decoder = codecs.getincrementaldecoder("utf-8")()
        first_piece_s: float | None = None
        parts: list[str] = []
        for chunk in response.iter_content(chunk_size=256):
            if not chunk:
                continue
            if first_piece_s is None:
                first_piece_s = time.monotonic() - started_at
            parts.append(decoder.decode(chunk))
        parts.append(decoder.decode(b"", final=True))

        return "".join(parts), first_piece_s
