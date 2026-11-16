import argparse
import contextlib
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
from flask import Flask, Response, jsonify, request, stream_with_context
from werkzeug.serving import make_server

from . import __version__, audio, bundle, cleanup, config, devices, failures, listen, llm, summary
from .canary import CanaryTranscriber
from .client import (
    BACKGROUND,
    LANGUAGE_HEADER,
    LANGUAGE_MARK,
    PRIORITY_HEADER,
    VOCABULARY_HEADER,
)
from .transcriber import BaseTranscriber, WhisperTranscriber

app = Flask(__name__)
# One longest window of raw float32 PCM, the wire format client.py sends, plus slack.
app.config["MAX_CONTENT_LENGTH"] = (
    int(config.MAX_WINDOW_S * config.SAMPLE_RATE_HZ) * audio.BYTES_PER_SAMPLE + 4096
)
transcriber: BaseTranscriber = WhisperTranscriber()
cleaner = llm.CleanupModel()

_activity_lock = threading.Lock()
_last_request_at = time.monotonic()
_in_flight = 0
# Captions and dictation only: what a background summary gives way to.
_last_npu_use_at = 0.0
_npu_in_flight = 0
# Captions and dictation decoding right now: what a file's piece waits behind.
_live_in_flight = 0
_last_live_at = float("-inf")


def _mark_activity(delta: int = 0) -> None:
    global _last_request_at, _in_flight
    with _activity_lock:
        _last_request_at = time.monotonic()
        _in_flight += delta


def _mark_npu_use(delta: int = 0) -> None:
    global _last_npu_use_at, _npu_in_flight
    with _activity_lock:
        _last_npu_use_at = time.monotonic()
        _npu_in_flight += delta


def npu_busy() -> bool:
    with _activity_lock:
        return _npu_in_flight > 0 or time.monotonic() - _last_npu_use_at < summary.QUIET_S


summariser = summary.Summariser(cleaner, npu_busy)


@app.before_request
def _touch_activity() -> None:
    _mark_activity()


@app.route("/transcribe", methods=["POST"])
def transcribe() -> Response:
    raw = request.get_data()
    refusal = audio_refusal(raw)
    if refusal is not None:
        return _error(refusal)
    # copy=True: frombuffer over bytes is read-only.
    samples = np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=True)

    background = request.headers.get(PRIORITY_HEADER) == BACKGROUND
    biased = request.headers.get(VOCABULARY_HEADER) == "1"
    heard: list[str] = []
    pieces = transcriber.transcribe_stream(
        samples, biased, heard.append if request.headers.get(LANGUAGE_HEADER) == "1" else None
    )
    stream = _track_in_flight(_then_language(pieces, heard), npu=True, live=not background)
    return Response(stream_with_context(stream), mimetype="text/plain; charset=utf-8")


@app.route("/health", methods=["GET"])
def health() -> Response:
    return jsonify(
        {
            "status": "ok",
            "version": __version__,
            **transcriber.describe(),
            "live_active": live_active(),
            "cleanup": cleaner.describe(),
            "summaries": summariser.describe(),
        }
    )


@app.route("/quit", methods=["POST"])
def quit_server() -> Response:
    # What `systemctl --user stop` was: a decode in flight is cut off with it.
    response = jsonify({"stopping": True, "socket_activated": listen.activated()})
    response.call_on_close(lambda: os._exit(0))
    return response


@app.route("/cleanup", methods=["POST"])
def clean() -> Response:
    asked = cleanup_request(request.get_json(silent=True))
    if asked is None:
        return _error("body must be JSON with a non-empty 'text'")
    text, budget_s = asked
    _mark_activity(+1)
    _mark_npu_use(+1)
    try:
        answer, truncated = cleaner.generate(
            cleanup.PROMPT,
            f"<transcript>{text}</transcript>",
            cleanup.max_tokens(text),
            budget_s=budget_s,
        )
    except llm.ModelMissing as exc:
        return _error(str(exc), 409)
    except TimeoutError as exc:
        return _error(str(exc), 504)
    except Exception as exc:
        return _error(f"cleanup failed: {exc}".splitlines()[0], 503)
    finally:
        _mark_activity(-1)
        _mark_npu_use(-1)
    return jsonify({"text": answer, "truncated": truncated, "device": cleaner.device})


@app.route("/cleanup/warm", methods=["POST"])
def warm() -> Response:
    # The dictation key is down: a summary mid-chunk would hold the model.
    _mark_npu_use()
    _mark_live()
    cleaner.warm()
    response = jsonify(cleaner.describe())
    response.status_code = 202
    return response


@app.route("/summaries", methods=["POST"])
def summaries() -> Response:
    paths = _named_sessions(request.get_json(silent=True))
    if isinstance(paths, str):
        return _error(paths)
    summariser.kick(paths)
    response = jsonify(summariser.describe())
    response.status_code = 202
    return response


def audio_refusal(raw: bytes) -> str | None:
    if not raw or len(raw) % audio.BYTES_PER_SAMPLE:
        return f"body must be a non-empty multiple of {audio.BYTES_PER_SAMPLE} bytes"
    duration_s = len(raw) // audio.BYTES_PER_SAMPLE / config.SAMPLE_RATE_HZ
    if duration_s > config.MAX_WINDOW_S:
        return (
            f"{duration_s:.1f}s of audio exceeds the {config.MAX_WINDOW_S}s short-form "
            "limit the streamer callback supports"
        )
    return None


def cleanup_request(body: object) -> tuple[str, float] | None:
    if not isinstance(body, dict):
        return None
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text, cleanup_budget_s(body.get("budget_s"))


def cleanup_budget_s(asked: object) -> float:
    if isinstance(asked, int | float) and not isinstance(asked, bool) and asked > 0:
        return min(float(asked), config.CLEANUP_MAX_BUDGET_S)
    return config.CLEANUP_MAX_BUDGET_S


def _named_sessions(body: object) -> list[Path] | str:
    paths: list[Path] = []
    if isinstance(body, dict) and isinstance(body.get("paths"), list):
        for raw in body["paths"]:
            if not isinstance(raw, str):
                return "'paths' must be a list of strings"
            path = _saved_session(raw)
            if path is None:
                return f"{raw} is not a saved session in {config.TRANSCRIPT_DIR}"
            paths.append(path)
    return paths


# Only bundles in the transcripts folder: the server never opens a path a request names.
def _saved_session(raw: str) -> Path | None:
    root = os.path.realpath(config.TRANSCRIPT_DIR)
    candidate = os.path.realpath(os.path.join(root, os.path.basename(raw)))
    if not candidate.startswith(root + os.sep):
        return None
    path = Path(candidate)
    if not bundle.is_bundle_name(path) or not path.is_file():
        return None
    return path


def _error(message: str, status: int = 400) -> Response:
    response = jsonify({"error": message})
    response.status_code = status
    return response


def _mark_live(delta: int = 0) -> None:
    global _live_in_flight, _last_live_at
    with _activity_lock:
        _live_in_flight += delta
        _last_live_at = time.monotonic()


def live_active() -> bool:
    with _activity_lock:
        return _live_in_flight > 0 or time.monotonic() - _last_live_at < config.LIVE_RECENT_S


def _wait_for_live(timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with _activity_lock:
            if _live_in_flight <= 0:
                return
        time.sleep(0.05)


def _then_language(pieces: Iterator[str], heard: list[str]) -> Iterator[str]:
    yield from pieces
    if heard:
        yield LANGUAGE_MARK + heard[-1]


def _track_in_flight(stream: Iterator[str], npu: bool = False, live: bool = True) -> Iterator[str]:
    _mark_activity(+1)
    if npu:
        _mark_npu_use(+1)
    if npu and live:
        _mark_live(+1)
    try:
        if npu and not live:
            # Bounded: captions decode back to back, and a file must still get its turn.
            _wait_for_live(config.BACKGROUND_YIELD_S)
        yield from stream
    finally:
        _mark_activity(-1)
        if npu:
            _mark_npu_use(-1)
        if npu and live:
            _mark_live(-1)


def _idle_watchdog(timeout_s: float) -> None:
    while True:
        time.sleep(config.IDLE_CHECK_INTERVAL_S)
        with _activity_lock:
            idle_for = time.monotonic() - _last_request_at
            busy = _in_flight > 0 or summariser.active
        cleaner.unload_if_idle(config.CLEANUP_IDLE_UNLOAD_S)
        if not busy and idle_for >= timeout_s:
            # The next client to connect starts it again (launch.py).
            os._exit(0)


def _resume_summaries() -> None:
    # A session that crashed before it was bundled, then anything left unsummarised.
    try:
        recovered = summary.recover()
    except OSError as exc:
        print(f"[vinowhisper-server] could not recover transcripts: {exc}", file=sys.stderr)
        recovered = []
    for path in recovered:
        print(
            f"[vinowhisper-server] bundled {path.name}, left by a session that did not end",
            file=sys.stderr,
        )
    if summary.auto_enabled() and summary.pending():
        summariser.kick()


def _harden() -> None:
    # The old unit's UMask=0077 and NoNewPrivileges=yes; its seccomp filters are gone (SECURITY.md).
    os.umask(0o077)
    import ctypes

    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0)  # PR_SET_NO_NEW_PRIVS


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="vinoWhisper transcription server.")
    parser.add_argument(
        "--device",
        default=None,
        metavar="NPU|GPU|CPU|auto",
        help=f"OpenVINO device to load the model on, overriding {config.SERVER_FILE} "
        "(vinowhisper-setup --device writes it). 'auto' (the default) walks "
        f"{'>'.join(devices.PREFERENCE)} and warns when it lands below NPU; an "
        "explicit device is refused rather than downgraded if it is unavailable.",
    )
    parser.add_argument(
        "--language",
        default=None,
        metavar="LANGUAGE",
        help=f"Spoken language, overriding {config.LANGUAGE_FILE} (default en). With Whisper, "
        f"{'|'.join(config.languages(config.WHISPER_MODEL))}: 'en' loads the English-only model; any other "
        "value loads the multilingual whisper-small, which has to be exported first "
        "(vinowhisper-setup --language ...). 'auto' detects per window and can flip "
        "mid-caption, so name the language when you know it.",
    )
    parser.add_argument(
        "--task",
        default=None,
        choices=config.TASKS,
        help="Overrides the task in the language file. 'translate' captions foreign speech in English. It is Whisper's own "
        "translate task, so it needs a spoken language or 'auto', and cannot go "
        "into any other language.",
    )
    parser.add_argument(
        "--model",
        default=None,
        choices=config.MODELS,
        help=f"Speech model, overriding {config.LANGUAGE_FILE} (default {config.DEFAULT_MODEL}). "
        f"{config.CANARY_MODEL} takes any of {len(config.CANARY_LANGUAGES)} languages by "
        "code (no auto) and translates them into English; it needs its own export.",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop the running server and exit: the next caption, dictation or import "
        "starts it again, on the settings as they are then. Also removes the systemd units "
        "earlier versions installed, which nothing needs now.",
    )
    parser.add_argument("--version", action="version", version=f"vinowhisper {__version__}")
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.device is None:
        args.device = config.load_device()
    args.language, args.task, args.model = config.resolve_language(
        args.language, args.task, args.model
    )
    try:
        config.check_language(args.language, args.task, args.model)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def _say(message: str) -> None:
    print(f"[vinowhisper-server] {message}", file=sys.stderr, flush=True)


def _use_models(args: argparse.Namespace) -> None:
    global transcriber, cleaner, summariser
    kind = CanaryTranscriber if args.model == config.CANARY_MODEL else WhisperTranscriber
    transcriber = kind(device=args.device, language=args.language, task=args.task)
    cleaner = llm.CleanupModel(device=args.device)
    summariser = summary.Summariser(cleaner, npu_busy)


def _load_speech_model(model: str) -> bool:
    try:
        selection = transcriber.select_device()
    except devices.DeviceError as exc:
        _say(str(exc))
        return False
    _say(f"loading {model} on {selection.device}")
    for warning in selection.warnings:
        _say(f"WARNING: {warning}")
    try:
        transcriber.load()
    except (FileNotFoundError, RuntimeError) as exc:
        _say(str(exc))
        if transcriber.failure is not None:
            _say(
                f"{transcriber.failure.device} is marked failed in "
                f"{config.FAILED_DEVICES_FILE}, so `--device auto` skips it from now on; "
                f"{failures.CLEAR_HINT}."
            )
        return False
    return True


def _serve(sock: socket.socket) -> None:
    threading.Thread(
        target=_idle_watchdog,
        args=(config.IDLE_TIMEOUT_S,),
        name="idle-watchdog",
        daemon=True,
    ).start()
    # make_server, not run_simple (which has no fd=); threaded so /health answers mid-decode.
    server = make_server(f"unix://{sock.getsockname()}", 0, app, threaded=True, fd=sock.fileno())
    # The poll only serves shutdown(), which nothing calls; a short one wakes an idle server.
    server.serve_forever(poll_interval=3600)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.stop:
        from . import launch

        return launch.stop_command()
    _harden()
    # Before the model load, so a missing XDG_RUNTIME_DIR or a running server fails in a second.
    try:
        sock = listen.listening_socket()
    except (listen.SocketError, OSError) as exc:
        _say(str(exc))
        return 1
    _use_models(args)
    if not _load_speech_model(args.model):
        return 1
    _resume_summaries()
    _serve(sock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
