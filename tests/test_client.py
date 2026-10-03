"""The transcription client reaches the server over its Unix socket, and nothing else."""

import json
import shutil
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import numpy as np
import pytest
import requests

from vinowhisper import __version__, client, config, doctor
from vinowhisper.client import TranscriptionClient


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self):
        return "unix"  # an AF_UNIX peer has no (host, port)

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps({"status": "ok", "device": "FAKE", "version": __version__}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        received = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.received.append(received)
        text = f"{len(received)} bytes, café".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        # Split inside the two-byte "é", as a streamed decode can.
        for piece in (text[:-1], text[-1:]):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def runtime_dir(monkeypatch):
    # Not tmp_path: AF_UNIX paths are limited to 107 bytes.
    path = Path(tempfile.mkdtemp(prefix="vw-"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(path))
    yield path
    shutil.rmtree(path)


@pytest.fixture
def server(runtime_dir):
    path = runtime_dir / config.SERVER_SOCKET_RELPATH
    path.parent.mkdir()
    instance = _Server(str(path), _Handler)
    instance.received = []
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()


def test_health_over_the_socket(server):
    assert TranscriptionClient().wait_ready()["device"] == "FAKE"


def test_transcribe_streams_over_the_socket(server):
    samples = np.linspace(-1, 1, 1600, dtype=np.float32)
    text, first_piece_s = TranscriptionClient().transcribe(samples)
    assert text == "6400 bytes, café"
    assert first_piece_s is not None
    assert server.received == [samples.astype("<f4").tobytes()]


def test_an_explicit_socket_path_wins(server, monkeypatch):
    path = Path(server.server_address)
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert TranscriptionClient(socket_path=path).wait_ready()["status"] == "ok"


def test_doctor_checks_the_same_socket(server):
    [result] = doctor._server()
    assert result.status == doctor.OK
    assert config.server_address() in result.detail


def test_no_server_is_a_connection_error_naming_the_socket(runtime_dir):
    with pytest.raises(requests.ConnectionError, match="server.sock"):
        TranscriptionClient().wait_ready()


def test_no_runtime_dir_is_a_connection_error_not_a_tmp_fallback(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert config.server_socket() is None
    with pytest.raises(requests.ConnectionError, match="XDG_RUNTIME_DIR"):
        TranscriptionClient().wait_ready()


def test_a_relative_runtime_dir_counts_as_unset(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "run/user/1000")
    assert config.server_socket() is None


def test_nothing_but_the_socket_is_reachable(runtime_dir):
    session = client.session()
    assert list(session.adapters) == ["http://"]
    with pytest.raises(requests.exceptions.InvalidSchema):
        session.get("https://example.com/", timeout=1)


def test_proxy_settings_in_the_environment_are_ignored(monkeypatch):
    # requests would otherwise apply http_proxy, sending the audio and transcripts to the proxy.
    monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    session = TranscriptionClient()._session
    settings = session.merge_environment_settings(
        f"{client.BASE_URL}/transcribe", {}, None, None, None
    )
    assert not settings["proxies"]
