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

from . import config, launch

# The host part is never resolved: every http:// request in the session goes to the socket.
BASE_URL = "http://vinowhisper"
# A file being transcribed: the server lets captions and dictation go first.
PRIORITY_HEADER = "X-Vinowhisper-Priority"
BACKGROUND = "background"
# Dictation: the server gives the decoder the user's vocabulary.txt as a prompt.
VOCABULARY_HEADER = "X-Vinowhisper-Vocabulary"
# Captions in auto mode: the server ends the text with LANGUAGE_MARK and the language heard.
LANGUAGE_HEADER = "X-Vinowhisper-Language"
LANGUAGE_MARK = "\x00"


class _UnixConnection(HTTPConnection):
    def __init__(
        self, *args: Any, socket_path: str | None, autostart: bool = True, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._socket_path = socket_path
        self._autostart = autostart

    def _connect(self, path: str) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if isinstance(self.timeout, int | float):
            sock.settimeout(self.timeout)
        try:
            sock.connect(path)
        except OSError:
            sock.close()
            raise
        return sock

    def _new_conn(self) -> socket.socket:
        path = self._socket_path
        if path is None:
            raise NewConnectionError(self, f"no server socket: {config.server_address()}")
        try:
            try:
                return self._connect(path)
            except (FileNotFoundError, ConnectionRefusedError):
                if not self._autostart:
                    raise
            # Nobody is listening: the server exits when idle, and whoever connects next
            # starts it, as a socket unit did.
            try:
                launch.ensure(Path(path))
            except launch.StartError as exc:
                raise NewConnectionError(self, str(exc)) from exc
            return self._connect(path)
        except TimeoutError as exc:
            raise ConnectTimeoutError(self, f"{path} timed out") from exc
        except OSError as exc:
            raise NewConnectionError(self, f"{path}: {exc}") from exc


class _UnixConnectionPool(HTTPConnectionPool):
    ConnectionCls = _UnixConnection

    def __init__(self, socket_path: str | None, autostart: bool = True) -> None:
        # maxsize: requests' own default, so concurrent callers reuse connections.
        super().__init__("vinowhisper", maxsize=10, socket_path=socket_path, autostart=autostart)


class UnixSocketAdapter(HTTPAdapter):
    def __init__(self, socket_path: Path | None, autostart: bool = True) -> None:
        super().__init__()
        self._pool = _UnixConnectionPool(
            None if socket_path is None else str(socket_path), autostart
        )

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


def session(socket_path: Path | None = None, autostart: bool = True) -> requests.Session:
    result = requests.Session()
    # Never through a proxy, which requests would otherwise take from http_proxy.
    result.trust_env = False
    # No https:// adapter and nothing over TCP: a stray URL fails instead of leaving the machine.
    result.adapters.clear()
    result.mount("http://", UnixSocketAdapter(socket_path or config.server_socket(), autostart))
    return result


class TranscriptionClient:
    def __init__(
        self, socket_path: Path | None = None, vocabulary: bool = False, language: bool = False
    ) -> None:
        # Not a default argument, which would read XDG_RUNTIME_DIR at import.
        self._session = session(socket_path)
        self._vocabulary = vocabulary
        self._language = language
        self.heard_language = ""

    def wait_ready(self) -> dict:
        response = self._session.get(
            f"{BASE_URL}/health",
            timeout=(config.CONNECT_TIMEOUT_S, config.MODEL_LOAD_TIMEOUT_S),
        )
        response.raise_for_status()
        return response.json()

    def clean_up(self, text: str, timeout_s: float) -> requests.Response:
        return self._session.post(
            f"{BASE_URL}/cleanup",
            json={"text": text, "budget_s": timeout_s},
            timeout=(config.CONNECT_TIMEOUT_S, timeout_s),
        )

    def warm_cleanup(self) -> None:
        self._session.post(
            f"{BASE_URL}/cleanup/warm",
            timeout=(config.CONNECT_TIMEOUT_S, config.MODEL_LOAD_TIMEOUT_S),
        ).raise_for_status()

    def summarise(self, paths: list[Path] | None = None) -> dict:
        response = self._session.post(
            f"{BASE_URL}/summaries",
            json={"paths": [str(path) for path in paths or []]},
            timeout=(config.CONNECT_TIMEOUT_S, config.CONNECT_TIMEOUT_S),
        )
        response.raise_for_status()
        return response.json()

    def transcribe(self, samples: np.ndarray, background: bool = False) -> tuple[str, float | None]:
        started_at = time.monotonic()
        headers = {"Content-Type": "application/octet-stream"}
        if background:
            headers[PRIORITY_HEADER] = BACKGROUND
        if self._vocabulary:
            headers[VOCABULARY_HEADER] = "1"
        if self._language:
            headers[LANGUAGE_HEADER] = "1"
        response = self._session.post(
            f"{BASE_URL}/transcribe",
            data=samples.astype("<f4", copy=False).tobytes(),
            headers=headers,
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

        text, _, self.heard_language = "".join(parts).partition(LANGUAGE_MARK)
        return text, first_piece_s
