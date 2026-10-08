import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import requests

from . import __version__, audio, capture, config, events, session, style
from .client import TranscriptionClient
from .recorder import CaptureError, Recorder, playback_streams, sink_muted
from .stitch import Stitcher, strip_controls

_SILENCE_NOTICE_AFTER_S = 45.0

_SILENCE_NOTICE = """
[vinowhisper] {seconds:.0f}s with no signal on the capture target.{muted}

  Measured 2026-08-07: on this machine the sink monitor is both pre-volume and
  pre-mute, carrying full signal at 20% volume and while muted. So neither the
  volume slider nor the system mute button explains this.

  What does, in rough order of likelihood:

    - Nothing is actually playing.
    - You muted the *application* rather than the system. An app writing
      silence into its own stream cannot be captured from anywhere, --target
      included, because there is no tap upstream of an app's own volume.
    - You are on --target and picked effect_output.bass_eq. That node is
      downstream of the volume control and really does go silent.

  Run vinowhisper-doctor to see the level on every target at once.
"""

_MUTED_LINE = "\n  (The default sink is muted. On this machine that does not silence the monitor.)"


def stall_window_s(window_s: float, stalled_for_s: float, task: str) -> float:
    if task == "translate" or stalled_for_s < config.STALL_AFTER_S:
        return window_s
    return min(window_s, config.STALL_WINDOW_S)


def caption_events(
    source: str,
    target: str | None,
    window_s: float,
    tap: Callable[[np.ndarray], None] | None = None,
) -> Iterator[events.Event]:
    client = TranscriptionClient()
    health = client.wait_ready()
    yield events.Ready(
        device=str(health.get("device", "?")),
        device_full=str(health.get("device_full_name", "")),
        degraded=bool(health.get("degraded", False)),
        warnings=[str(warning) for warning in health.get("warnings", [])],
        server_version=str(health.get("version", "")),
        language=str(health.get("language", "")),
        task=str(health.get("task", "")),
    )

    stitcher = Stitcher()
    index = 0
    last_cycle_at_s = 0.0
    last_commit_at_s = 0.0
    silent_since: float | None = None

    # Wraps the whole with-block so a Ctrl+C during cleanup still exits cleanly.
    try:
        with Recorder(source=source, target=target, tap=tap) as recorder:
            while True:
                recorder.check_alive()

                captured_s = recorder.captured_s
                if captured_s < config.MIN_WINDOW_S:
                    time.sleep(config.MIN_WINDOW_S - captured_s)
                    continue

                hop_s = captured_s - last_cycle_at_s
                if hop_s < config.MIN_HOP_S:
                    time.sleep(config.MIN_HOP_S - hop_s)
                    continue

                stalled_for_s = recorder.captured_s - last_commit_at_s
                window = recorder.window(
                    stall_window_s(window_s, stalled_for_s, health.get("task", ""))
                )
                last_cycle_at_s = recorder.captured_s

                level = audio.rms(window)
                if level < config.SILENCE_RMS_THRESHOLD:
                    now = time.monotonic()
                    silent_since = now if silent_since is None else silent_since
                    last_commit_at_s = last_cycle_at_s
                    elapsed_s = now - silent_since
                    muted = sink_muted() if elapsed_s >= _SILENCE_NOTICE_AFTER_S else None
                    yield events.Silence(elapsed_s=elapsed_s, rms=level, sink_muted=muted)
                    continue
                silent_since = None

                window, gain = audio.normalize(window, config.TARGET_RMS, config.MAX_GAIN)

                started_at = time.monotonic()
                transcript, first_piece_s = client.transcribe(window)
                total_s = time.monotonic() - started_at
                transcript = strip_controls(transcript)

                confirmed = stitcher.push(transcript)
                if confirmed:
                    last_commit_at_s = last_cycle_at_s
                index += 1
                yield events.Cycle(
                    index=index,
                    captured_s=last_cycle_at_s,
                    window_s=window.size / config.SAMPLE_RATE_HZ,
                    hop_s=hop_s,
                    rms=level,
                    gain=gain,
                    first_piece_s=first_piece_s,
                    total_s=total_s,
                    transcript=transcript,
                    confirmed=confirmed,
                    pending=stitcher.pending,
                )
    except KeyboardInterrupt:
        pass

    yield events.Stopped(flushed=stitcher.flush())


class TerminalRenderer:
    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self._started = False
        self._silence_reported = False

    def __enter__(self) -> "TerminalRenderer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def handle(self, event: events.Event) -> None:
        if isinstance(event, events.Ready):
            device = f"{event.device} ({event.device_full})" if event.device_full else event.device
            speech = events.speech_label(event.language, event.task)
            heard = f", language {speech}" if speech else ""
            print(f"[vinowhisper] ready on {device}{heard}. Ctrl+C to stop.", file=sys.stderr)
            for warning in event.warnings:
                print(f"[vinowhisper] WARNING: {warning}", file=sys.stderr)
            print(file=sys.stderr)
        elif isinstance(event, events.Cycle):
            self._silence_reported = False
            if self.debug:
                print(self._debug_line(event), file=sys.stderr)
            self._emit(event.confirmed)
        elif isinstance(event, events.Silence):
            if event.elapsed_s >= _SILENCE_NOTICE_AFTER_S and not self._silence_reported:
                muted = _MUTED_LINE if event.sink_muted else ""
                print(
                    _SILENCE_NOTICE.format(seconds=event.elapsed_s, muted=muted),
                    file=sys.stderr,
                )
                self._silence_reported = True
        elif isinstance(event, events.Stopped):
            self._emit(event.flushed)
            print(flush=True)

    def _emit(self, words: list[str]) -> None:
        if not words:
            return
        print((" " if self._started else "") + " ".join(words), end="", flush=True)
        self._started = True

    @staticmethod
    def _debug_line(event: events.Cycle) -> str:
        first = "-" if event.first_piece_s is None else f"{event.first_piece_s:.2f}s"
        return (
            f"\n--- cycle {event.index}: {event.window_s:.1f}s window, "
            f"hop {event.hop_s:.1f}s, rms {event.rms:.4f}, gain {event.gain:.1f}x, "
            f"first piece {first}, total {event.total_s:.2f}s, "
            f"+{len(event.confirmed)} confirmed, {len(event.pending)} pending "
            f"---\n{event.transcript}\n"
        )


class JsonRenderer:
    def __init__(self) -> None:
        # Looked up here, not at import, so a redirected stdout is honoured.
        self._stream = sys.stdout

    def __enter__(self) -> "JsonRenderer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def handle(self, event: events.Event) -> None:
        self._write(events.to_dict(event))

    def error(self, message: str) -> None:
        self._write({"event": "Error", "message": message})

    def _write(self, record: dict) -> None:
        self._stream.write(json.dumps(record) + "\n")
        self._stream.flush()


def _renderer(plain: bool, debug: bool):
    if plain:
        return TerminalRenderer(debug=debug)
    from .ui import RichRenderer

    return RichRenderer()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live captioning via NPU Whisper.")
    parser.add_argument(
        "--source",
        choices=["output", "mic"],
        default=config.DEFAULT_SOURCE,
        help="'output' captions system audio (default), 'mic' captions your voice.",
    )
    parser.add_argument(
        "--target",
        help="PipeWire node to capture, overriding the default sink. Use "
        "--list-targets to find an application's own playback stream, which "
        "stays audible to the capture even when the system is muted.",
    )
    parser.add_argument(
        "--list-targets",
        action="store_true",
        help="List applications currently playing audio, then exit.",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=config.WINDOW_S,
        metavar="SECONDS",
        help=f"Seconds of audio per transcription cycle (default {config.WINDOW_S}). "
        "Smaller is lower latency and less context; larger is the reverse.",
    )
    parser.add_argument(
        "--record",
        type=Path,
        metavar="DIR",
        help="Save the session (audio.wav + events.jsonl) for replay with "
        "vinowhisper-replay. Costs ~2MB per minute.",
    )
    parser.add_argument(
        "--transcript",
        nargs="?",
        type=Path,
        const=config.TRANSCRIPT_DIR,
        metavar="PATH",
        help="Also write the confirmed words to a text file, with a [MM:SS] stamp "
        "per paragraph. A directory gets a new file per session; a file is "
        f"appended to. Default {config.TRANSCRIPT_DIR}. Private (0600), and "
        "nothing is written until the first word.",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Plain stdout instead of the status bar. Implied when stdout is "
        "not a terminal, so piping to a file still works.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Per-cycle timing, levels and raw transcript to stderr, to tell "
        "apart Whisper re-decoding the same audio differently, a slow cycle, "
        "and a bug in the stitching. Implies --plain.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="One JSON object per event on stdout instead of text: what "
        "vinowhisper-gui reads. Human-readable messages stay on stderr.",
    )
    parser.add_argument("--version", action="version", version=f"vinowhisper {__version__}")
    return parser.parse_args()


def _list_targets() -> int:
    active = capture.backend()
    streams = playback_streams()
    if not streams:
        print(f"Nothing to target on the {active.name} backend.", file=sys.stderr)
        if active.supports_app_capture:
            print("No applications are currently playing audio.", file=sys.stderr)
        return 1

    if not active.supports_app_capture:
        print(
            f"[{active.name}] monitor sources only — PulseAudio cannot tap a single\n"
            "application's stream. Install PipeWire for per-application capture.",
            file=sys.stderr,
        )
    # media.name is whatever the playing app chose, e.g. a browser tab's title.
    streams = [{key: strip_controls(value) for key, value in stream.items()} for stream in streams]
    width = max(len(stream["app"]) for stream in streams)
    for stream in streams:
        style.emit(
            "  ",
            (f"--target {stream['target']:<8}", style.COMMAND),
            " ",
            (f"{stream['app']:<{width}}", style.LABEL),
            "  ",
            (stream["media"], style.MUTED),
        )
    return 0


def main() -> int:
    args = _parse_args()
    try:
        if args.list_targets:
            return _list_targets()
    except CaptureError as exc:
        print(f"[vinowhisper] {exc}", file=sys.stderr)
        return 1

    if not 0 < args.window <= config.MAX_WINDOW_S:
        print(
            f"--window must be greater than 0 and at most {config.MAX_WINDOW_S} "
            "(the short-form limit the streamer callback supports)",
            file=sys.stderr,
        )
        return 2

    print(
        "[vinowhisper] waiting for the transcription server "
        "(NPU model load on a cold start takes ~10-30s)...",
        file=sys.stderr,
    )

    plain = args.plain or args.debug or not sys.stdout.isatty()
    json_out = JsonRenderer() if args.json else None
    writer = session.SessionWriter(args.record) if args.record else None
    transcript = session.TranscriptWriter(args.transcript, args.source) if args.transcript else None
    try:
        stream = caption_events(
            source=args.source,
            target=args.target,
            window_s=args.window,
            tap=writer.audio_chunk if writer else None,
        )
        with json_out or _renderer(plain, debug=args.debug) as renderer:
            for event in stream:
                if writer is not None:
                    writer.event(event)
                if transcript is not None:
                    try:
                        transcript.event(event)
                    except OSError as exc:
                        print(f"\n[vinowhisper] transcript not saved: {exc}", file=sys.stderr)
                        transcript.close()
                        transcript = None
                renderer.handle(event)
    except CaptureError as exc:
        print(f"\n[vinowhisper] capture failed: {exc}", file=sys.stderr)
        if json_out is not None:
            json_out.error(f"Capture failed: {exc}")
        return 1
    except requests.RequestException as exc:
        print(
            f"\n[vinowhisper] server not reachable at {config.server_address()}: {exc}\n"
            "  systemctl --user status vinowhisper-server.socket\n"
            "  vinowhisper-doctor        # what is actually missing\n"
            "  vinowhisper-setup         # install the units if they were never installed",
            file=sys.stderr,
        )
        if json_out is not None:
            json_out.error(
                f"The transcription server is not reachable at {config.server_address()}. "
                "Run vinowhisper-doctor to see what is missing."
            )
        return 1
    except BrokenPipeError:
        # Stops the interpreter's exit flush raising BrokenPipeError again.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        print(file=sys.stderr if json_out else sys.stdout, flush=True)
    finally:
        if writer is not None:
            writer.close()
            print(f"\n[vinowhisper] session saved to {args.record}", file=sys.stderr)
        if transcript is not None:
            transcript.close()
            if transcript.path is not None:
                print(f"\n[vinowhisper] transcript saved to {transcript.path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
