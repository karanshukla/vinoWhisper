"""Live captioning entry point. Run manually in a terminal, Ctrl+C to stop.

Each cycle: take the newest WINDOW_S of captured audio, transcribe it, stitch
the result against what's already on screen, emit whatever that confirms.

The loop is synchronous and self-pacing. There's no hop timer and never more
than one request in flight, so the hop between windows is just however long
the previous cycle took. That makes cycle time the one thing that matters for
latency: the commit policy needs two cycles to agree before anything prints
(see stitch.py), so captions trail the audio by roughly twice it.

`caption_events` yields events rather than printing (see events.py). The
terminal renderer below is one consumer, the session recorder is another, and
an on-top TUI would be a third.
"""

import argparse
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import requests

from . import audio, config, events, session
from .client import TranscriptionClient
from .recorder import CaptureError, Recorder, playback_streams, sink_muted
from .stitch import Stitcher

# How long a completely silent input has to persist before the terminal
# renderer says something. Silence is normal; most of a minute of it while the
# user thinks captions should be running is a symptom worth naming.
_SILENCE_NOTICE_AFTER_S = 45.0

_SILENCE_NOTICE = """
[vinowhisper] {seconds:.0f}s with no signal on the capture target.{muted}

  A muted sink monitors as digital silence, so there is nothing to transcribe.
  (The sink's *volume* is not the problem: the monitor is pre-volume, measured
  2026-08-07.) Capturing an application's own playback stream taps upstream of
  the sink's mute:

      vinowhisper-caption --list-targets
      vinowhisper-caption --target <target>

  Run vinowhisper-doctor to check this directly.
"""

_MUTED_LINE = "\n  The default sink is currently MUTED. That is the cause."


def caption_events(
    source: str,
    target: str | None,
    window_s: float,
    tap: Callable[[np.ndarray], None] | None = None,
) -> Iterator[events.Event]:
    """Run the capture/transcribe/stitch loop, yielding events until Ctrl+C."""
    client = TranscriptionClient()
    health = client.wait_ready()
    yield events.Ready(device=str(health.get("device", "?")))

    stitcher = Stitcher()
    index = 0
    # Measured against Recorder.captured_s, which counts every sample ever
    # captured, so this stays correct regardless of how much the ring buffer
    # has since overwritten.
    last_cycle_at_s = 0.0
    silent_since: float | None = None

    try:
        with Recorder(source=source, target=target, tap=tap) as recorder:
            while True:
                recorder.check_alive()

                captured_s = recorder.captured_s
                if captured_s < config.MIN_WINDOW_S:
                    time.sleep(config.MIN_WINDOW_S - captured_s)
                    continue

                # Nothing to learn from re-decoding a window that is almost
                # entirely last cycle's window; wait for real new audio.
                hop_s = captured_s - last_cycle_at_s
                if hop_s < config.MIN_HOP_S:
                    time.sleep(config.MIN_HOP_S - hop_s)
                    continue

                window = recorder.window(window_s)
                last_cycle_at_s = recorder.captured_s

                level = audio.rms(window)
                if level < config.SILENCE_RMS_THRESHOLD:
                    now = time.monotonic()
                    silent_since = now if silent_since is None else silent_since
                    elapsed_s = now - silent_since
                    # Only shell out to pactl once the silence is worth
                    # explaining, not on every quiet half-second.
                    muted = sink_muted() if elapsed_s >= _SILENCE_NOTICE_AFTER_S else None
                    yield events.Silence(elapsed_s=elapsed_s, rms=level, sink_muted=muted)
                    continue
                silent_since = None

                window, gain = audio.normalize(window, config.TARGET_RMS, config.MAX_GAIN)

                started_at = time.monotonic()
                transcript, first_piece_s = client.transcribe(window)
                total_s = time.monotonic() - started_at

                confirmed = stitcher.push(transcript)
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
        # Wraps the whole `with` block, not just the loop, so a second or
        # mistimed Ctrl+C during Recorder cleanup still exits cleanly instead
        # of spewing a traceback (confirmed happening in real testing).
        pass

    # Whatever agreed once but never got a confirming cycle: better to show the
    # last unconfirmed guess than to drop it on exit.
    yield events.Stopped(flushed=stitcher.flush())


class TerminalRenderer:
    """Confirmed words as one growing paragraph on stdout, everything else on
    stderr so the transcript stays pipeable.
    """

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
            print(f"[vinowhisper] ready on {event.device}. Ctrl+C to stop.\n", file=sys.stderr)
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


def _renderer(plain: bool, debug: bool):
    """The status bar when there's a terminal to draw it on, plain text
    otherwise. Both consume the same events; neither knows about the loop.
    """
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
    return parser.parse_args()


def _list_targets() -> int:
    streams = playback_streams()
    if not streams:
        print("No applications are currently playing audio.", file=sys.stderr)
        return 1
    width = max(len(stream["app"]) for stream in streams)
    for stream in streams:
        print(f"  --target {stream['target']:<8} {stream['app']:<{width}}  {stream['media']}")
    return 0


def main() -> int:
    args = _parse_args()
    if args.list_targets:
        return _list_targets()

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

    # The status bar redraws, so it and the raw per-cycle dump would fight over
    # the same screen. --debug is for reading numbers, not watching captions.
    plain = args.plain or args.debug or not sys.stdout.isatty()
    writer = session.SessionWriter(args.record) if args.record else None
    try:
        stream = caption_events(
            source=args.source,
            target=args.target,
            window_s=args.window,
            tap=writer.audio_chunk if writer else None,
        )
        with _renderer(plain, debug=args.debug) as renderer:
            for event in stream:
                if writer is not None:
                    writer.event(event)
                renderer.handle(event)
    except CaptureError as exc:
        print(f"\n[vinowhisper] capture failed: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"\n[vinowhisper] server not reachable at {config.SERVER_URL}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(flush=True)
    finally:
        if writer is not None:
            writer.close()
            print(f"\n[vinowhisper] session saved to {args.record}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
