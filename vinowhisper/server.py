"""Local-only HTTP wrapper around WhisperTranscriber.

Socket-activated, not a plain always-on service (see vinowhisper-server.socket
+ vinowhisper-server.service): systemd owns the listening socket and only
spawns this process on the first connection, mirroring a serverless
scale-to-zero pattern. This process self-exits after config.IDLE_TIMEOUT_S
of inactivity; the next request through the socket respawns it and pays the
NPU model-load cost again. That cold-start/idle-unload trade is deliberate,
not an accident — see README.md.
"""

import os
import threading
import time

import numpy as np
from flask import Flask, Response, request, jsonify
from werkzeug.serving import run_simple

from . import config
from .transcriber import WhisperTranscriber

app = Flask(__name__)
transcriber = WhisperTranscriber()

_last_request_at = time.monotonic()
_activity_lock = threading.Lock()


@app.before_request
def _touch_activity() -> None:
    global _last_request_at
    with _activity_lock:
        _last_request_at = time.monotonic()


@app.route("/transcribe", methods=["POST"])
def transcribe():
    # Raw float32 PCM, no WAV container — the client is a continuous
    # rolling-buffer capture, it never has a WAV file to send.
    samples = np.frombuffer(request.get_data(), dtype="<f4")
    return Response(transcriber.transcribe_stream(samples), mimetype="text/plain")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "device": transcriber.device})


def _idle_watchdog(timeout_s: float) -> None:
    while True:
        time.sleep(30)
        with _activity_lock:
            idle_for = time.monotonic() - _last_request_at
        if idle_for >= timeout_s:
            os._exit(0)


def _systemd_socket_fd() -> int | None:
    """Fd systemd handed us via socket activation, or None if launched
    directly (e.g. manual testing outside systemd, no idle-unload behavior).
    """
    if (
        os.environ.get("LISTEN_PID") == str(os.getpid())
        and int(os.environ.get("LISTEN_FDS", "0")) >= 1
    ):
        return 3  # SD_LISTEN_FDS_START
    return None


def main() -> None:
    transcriber.load()
    threading.Thread(
        target=_idle_watchdog, args=(config.IDLE_TIMEOUT_S,), daemon=True
    ).start()

    fd = _systemd_socket_fd()
    if fd is not None:
        run_simple(config.SERVER_HOST, config.SERVER_PORT, app, fd=fd)
    else:
        run_simple(config.SERVER_HOST, config.SERVER_PORT, app)


if __name__ == "__main__":
    main()
