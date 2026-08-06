"""Live captioning entry point. Run manually in a terminal, Ctrl+C to stop.

Each cycle: take the newest WINDOW_S of captured audio, transcribe it, stitch
the result against what's already on screen, print whatever that confirms.

The loop is synchronous and self-pacing — there's no hop timer, and never more
than one request in flight, so the hop between windows is just however long
the previous cycle took. That makes cycle time the one thing that matters for
latency: the commit policy needs two cycles to agree before printing (see
stitch.py), so captions trail the audio by roughly twice it.
"""

import argparse
import sys
import time

import requests

from . import audio, config
from .client import TranscriptionClient
from .recorder import CaptureError, Recorder, playback_streams
from .stitch import Stitcher

# How long a completely silent input has to persist before saying something
# about it. Silence is normal; most of a minute of it while the user thinks
# captions should be running is a symptom worth naming.
_SILENCE_NOTICE_AFTER_S = 45.0

_SILENCE_NOTICE = """
[vinowhisper] {seconds:.0f}s with no signal on the capture target.

  If system audio is muted, that is the cause, and there is no fix on this
  side: a sink's monitor carries what the sink is playing *after* its own
  volume and mute, so a muted sink monitors as digital silence. Capturing an
  application's own playback stream taps upstream of the sink's mute:

      vinowhisper-caption --list-targets
      vinowhisper-caption --target <target>

  Otherwise: check that something is actually playing, and that --source
  matches what you meant ('output' for system audio, 'mic' for the mic).
"""


class _Transcript:
    """Prints confirmed words as one continuously growing paragraph."""

    def __init__(self) -> None:
        self._started = False

    def emit(self, words: list[str]) -> None:
        if not words:
            return
        print((" " if self._started else "") + " ".join(words), end="", flush=True)
        self._started = True


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
        "--debug",
        action="store_true",
        help="Per-cycle timing, levels and raw transcript to stderr — to tell "
        "apart Whisper re-decoding the same audio differently, a slow cycle, "
        "and a bug in the stitching.",
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


def _caption_loop(args: argparse.Namespace, client: TranscriptionClient) -> None:
    stitcher = Stitcher()
    transcript = _Transcript()
    cycle = 0
    # Measured against Recorder.captured_s, which counts every sample ever
    # captured — so this stays correct regardless of how much the ring buffer
    # has since overwritten.
    last_cycle_at_s = 0.0
    silent_since: float | None = None
    silence_reported = False

    try:
        with Recorder(source=args.source, target=args.target) as recorder:
            while True:
                recorder.check_alive()

                captured_s = recorder.captured_s
                if captured_s < config.MIN_WINDOW_S:
                    time.sleep(config.MIN_WINDOW_S - captured_s)
                    continue

                # Nothing to learn from re-decoding a window that is almost
                # entirely last cycle's window; wait for real new audio.
                new_audio_s = captured_s - last_cycle_at_s
                if new_audio_s < config.MIN_HOP_S:
                    time.sleep(config.MIN_HOP_S - new_audio_s)
                    continue

                window = recorder.window(args.window)
                last_cycle_at_s = recorder.captured_s

                level = audio.rms(window)
                if level < config.SILENCE_RMS_THRESHOLD:
                    now = time.monotonic()
                    silent_since = now if silent_since is None else silent_since
                    silent_for = now - silent_since
                    if silent_for >= _SILENCE_NOTICE_AFTER_S and not silence_reported:
                        print(_SILENCE_NOTICE.format(seconds=silent_for), file=sys.stderr)
                        silence_reported = True
                    continue
                silent_since = None
                silence_reported = False

                window, gain = audio.normalize(window, config.TARGET_RMS, config.MAX_GAIN)

                started_at = time.monotonic()
                text, first_piece_s = client.transcribe(window)
                elapsed_s = time.monotonic() - started_at

                newly_confirmed = stitcher.push(text)

                if args.debug:
                    cycle += 1
                    print(
                        f"\n--- cycle {cycle}: "
                        f"{window.size / config.SAMPLE_RATE_HZ:.1f}s window, "
                        f"hop {new_audio_s:.1f}s, rms {level:.4f}, gain {gain:.1f}x, "
                        f"first piece {_seconds(first_piece_s)}, total {elapsed_s:.2f}s, "
                        f"+{len(newly_confirmed)} confirmed, "
                        f"{len(stitcher.pending)} pending ---\n{text}\n",
                        file=sys.stderr,
                    )

                transcript.emit(newly_confirmed)
    except KeyboardInterrupt:
        # Wraps the whole `with` block, not just the loop, so a second or
        # mistimed Ctrl+C during Recorder cleanup still exits cleanly instead
        # of spewing a traceback (confirmed happening in real testing).
        pass

    # Whatever agreed once but never got a confirming cycle: better to show the
    # last unconfirmed guess than to drop it on exit.
    transcript.emit(stitcher.flush())
    print(flush=True)


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}s"


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

    client = TranscriptionClient()
    print(
        "[vinowhisper] waiting for the transcription server "
        "(NPU model load on a cold start takes ~10-30s)...",
        file=sys.stderr,
    )
    try:
        health = client.wait_ready()
    except requests.RequestException as exc:
        print(f"[vinowhisper] server not reachable at {config.SERVER_URL}: {exc}", file=sys.stderr)
        return 1
    print(f"[vinowhisper] ready on {health.get('device', '?')}. Ctrl+C to stop.\n", file=sys.stderr)

    try:
        _caption_loop(args, client)
    except CaptureError as exc:
        print(f"\n[vinowhisper] capture failed: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"\n[vinowhisper] transcription request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
