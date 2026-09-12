import argparse
import os
import sys
import threading
import time
from collections.abc import Iterator

import numpy as np
from flask import Flask, Response, jsonify, request, stream_with_context
from werkzeug.serving import make_server

from . import __version__, audio, config, devices
from .transcriber import WhisperTranscriber

app = Flask(__name__)
transcriber = WhisperTranscriber()

_activity_lock = threading.Lock()
_last_request_at = time.monotonic()
_in_flight = 0


def _mark_activity(delta: int = 0) -> None:
    global _last_request_at, _in_flight
    with _activity_lock:
        _last_request_at = time.monotonic()
        _in_flight += delta


@app.before_request
def _touch_activity() -> None:
    _mark_activity()


@app.route("/transcribe", methods=["POST"])
def transcribe() -> Response:
    raw = request.get_data()
    if not raw or len(raw) % audio.BYTES_PER_SAMPLE:
        return _error(f"body must be a non-empty multiple of {audio.BYTES_PER_SAMPLE} bytes")

    # copy=True: frombuffer over bytes is read-only.
    samples = np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=True)
    duration_s = samples.size / config.SAMPLE_RATE_HZ
    if duration_s > config.MAX_WINDOW_S:
        return _error(
            f"{duration_s:.1f}s of audio exceeds the {config.MAX_WINDOW_S}s short-form "
            "limit the streamer callback supports"
        )

    stream = _track_in_flight(transcriber.transcribe_stream(samples))
    return Response(stream_with_context(stream), mimetype="text/plain; charset=utf-8")


@app.route("/health", methods=["GET"])
def health() -> Response:
    return jsonify({"status": "ok", "version": __version__, **transcriber.describe()})


def _error(message: str) -> Response:
    response = jsonify({"error": message})
    response.status_code = 400
    return response


def _track_in_flight(stream: Iterator[str]) -> Iterator[str]:
    _mark_activity(+1)
    try:
        yield from stream
    finally:
        _mark_activity(-1)


def _idle_watchdog(timeout_s: float) -> None:
    while True:
        time.sleep(config.IDLE_CHECK_INTERVAL_S)
        with _activity_lock:
            idle_for = time.monotonic() - _last_request_at
            busy = _in_flight > 0
        if not busy and idle_for >= timeout_s:
            # _exit(0): a clean exit tells systemd this was an idle unload, not a crash.
            os._exit(0)


def _systemd_socket_fd() -> int | None:
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return None
    try:
        listen_fds = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError:
        return None
    return 3 if listen_fds >= 1 else None  # SD_LISTEN_FDS_START


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="vinoWhisper transcription server.")
    parser.add_argument(
        "--device",
        default=config.DEFAULT_DEVICE,
        metavar="NPU|GPU|CPU|auto",
        help="OpenVINO device to load the model on. 'auto' (the default) walks "
        f"{'>'.join(devices.PREFERENCE)} and warns when it lands below NPU; an "
        "explicit device is refused rather than downgraded if it is unavailable.",
    )
    parser.add_argument("--version", action="version", version=f"vinowhisper {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    global transcriber
    transcriber = WhisperTranscriber(device=args.device)

    try:
        selection = transcriber.select_device()
    except devices.DeviceError as exc:
        print(f"[vinowhisper-server] {exc}", file=sys.stderr, flush=True)
        return 1

    print(f"[vinowhisper-server] loading on {selection.device}", file=sys.stderr, flush=True)
    for warning in selection.warnings:
        print(f"[vinowhisper-server] WARNING: {warning}", file=sys.stderr, flush=True)

    try:
        transcriber.load()
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[vinowhisper-server] {exc}", file=sys.stderr, flush=True)
        return 1

    threading.Thread(
        target=_idle_watchdog,
        args=(config.IDLE_TIMEOUT_S,),
        name="idle-watchdog",
        daemon=True,
    ).start()

    fd = _systemd_socket_fd()
    # make_server, not run_simple (which has no fd=); threaded so /health answers mid-decode.
    server = make_server(config.SERVER_HOST, config.SERVER_PORT, app, threaded=True, fd=fd)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
