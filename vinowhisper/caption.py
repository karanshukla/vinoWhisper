import argparse
import contextlib
import itertools
import json
import os
import signal
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import requests

from . import (
    audio,
    bundle,
    capture,
    config,
    echo_lane,
    echo_policy,
    echo_text,
    events,
    playing,
    session,
    style,
    summary,
    vad,
)
from .client import TranscriptionClient
from .echo_lane import EchoRecorder
from .echo_worker import Status
from .recorder import CaptureError, Recorder, playback_streams, sink_muted
from .stitch import Stitcher, strip_controls
from .version import VERSION

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


class SpeechGate:
    def __init__(self, detect: Callable[[np.ndarray], float]) -> None:
        self._detect: Callable[[np.ndarray], float] | None = detect
        self._lock = threading.Lock()
        self._chunks: deque[tuple[int, bool]] = deque()
        self._held = 0
        self._capacity = int(config.MAX_WINDOW_S * config.SAMPLE_RATE_HZ)

    @property
    def active(self) -> bool:
        return self._detect is not None

    # Runs on the capture thread, once per chunk, in step with the ring buffer.
    def tap(self, chunk: np.ndarray) -> None:
        detect = self._detect
        if detect is None:
            return
        try:
            speech = detect(chunk) >= vad.SPEECH_PROBABILITY
        except Exception:  # noqa: BLE001 — captions go on, decoding every sound as before
            self._detect = None
            return
        with self._lock:
            self._chunks.append((chunk.size, speech))
            self._held += chunk.size
            while self._chunks and self._held - self._chunks[0][0] >= self._capacity:
                self._held -= self._chunks.popleft()[0]

    # (pinned by `test_speech_gate.py::test_a_window_with_no_speech_is_not_decoded`,
    # `test_speech_gate.py::test_sound_away_from_speech_is_scaled_to_near_silence_and_speech_is_kept`)
    def apply(self, window: np.ndarray) -> np.ndarray | None:
        if self._detect is None:
            return window
        with self._lock:
            chunks = list(self._chunks)
        if not chunks:
            return window
        sizes = np.array([size for size, _ in chunks])
        flags = np.array([speech for _, speech in chunks])
        if int(sizes[flags].sum()) < config.CAPTION_MIN_SPEECH_S * config.SAMPLE_RATE_HZ:
            return None
        pad = max(1, round(config.TRIM_PAD_S * config.SAMPLE_RATE_HZ / int(sizes.max())))
        near = np.convolve(flags.astype(np.int8), np.ones(2 * pad + 1, dtype=np.int8), "same") > 0
        mask = np.repeat(near, sizes)
        if mask.size >= window.size:
            mask = mask[mask.size - window.size :]
        else:
            # (pinned by `test_speech_gate.py::test_audio_older_than_the_detectors_history_is_kept`)
            mask = np.concatenate([np.ones(window.size - mask.size, dtype=bool), mask])
        gated = window.astype(np.float32, copy=True)
        gated[~mask] *= _NOT_SPEECH_SCALE
        return gated


_NOT_SPEECH_SCALE = 1e-4


def speech_gate(warnings: list[str]) -> SpeechGate | None:
    try:
        return SpeechGate(vad.SpeechDetector(vad.model_path()))
    except (vad.VadError, RuntimeError, ImportError, OSError) as exc:
        warnings.append(f"speech detector unavailable ({exc}); captions decode any sound")
        return None


def _taps(
    *taps: Callable[[np.ndarray], None] | None,
) -> Callable[[np.ndarray], None] | None:
    live = [tap for tap in taps if tap is not None]
    if not live:
        return None
    if len(live) == 1:
        return live[0]

    def both(chunk: np.ndarray) -> None:
        for tap in live:
            tap(chunk)

    return both


def _ready_event(health: dict, warnings: list[str], echo: Status | None = None) -> events.Ready:
    return events.Ready(
        echo_mode=echo.mode if echo else "",
        echo_active=echo.active if echo else False,
        echo_reason=echo.reason if echo else "",
        device=str(health.get("device", "?")),
        device_full=str(health.get("device_full_name", "")),
        degraded=bool(health.get("degraded", False)),
        warnings=warnings,
        server_version=str(health.get("version", "")),
        language=str(health.get("language", "")),
        task=str(health.get("task", "")),
        model=str(health.get("model", "")),
    )


def pacing_delay_s(captured_s: float, last_cycle_at_s: float) -> float:
    if captured_s < config.MIN_WINDOW_S:
        return config.MIN_WINDOW_S - captured_s
    return max(0.0, config.MIN_HOP_S - (captured_s - last_cycle_at_s))


class AgreedLanguage:
    def __init__(self) -> None:
        self.current = ""
        self._last_heard = ""

    def push(self, heard: str) -> str:
        if heard and heard == self._last_heard:
            self.current = heard
        self._last_heard = heard
        return self.current


class _SilenceClock:
    def __init__(self) -> None:
        self._since: float | None = None

    def reset(self) -> None:
        self._since = None

    def event(self, level: float, no_speech: bool) -> events.Silence:
        now = time.monotonic()
        self._since = now if self._since is None else self._since
        elapsed_s = now - self._since
        ask_the_sink = elapsed_s >= _SILENCE_NOTICE_AFTER_S and not no_speech
        return events.Silence(
            elapsed_s=elapsed_s,
            rms=level,
            sink_muted=sink_muted() if ask_the_sink else None,
            no_speech=no_speech,
        )


def _speech_in(window: np.ndarray, level: float, speech: SpeechGate | None) -> np.ndarray | None:
    if level < config.SILENCE_RMS_THRESHOLD:
        return None
    return window if speech is None else speech.apply(window)


# What each source is called when both are captioned at once (#118).
LANES = {"mic": events.LANE_YOU, "output": events.LANE_THEM}
BOTH = "both"


@dataclass
class _Lane:
    name: str
    source: str
    target: str | None
    speech: SpeechGate | None
    tap: Callable[[np.ndarray], None] | None
    stitcher: Stitcher = field(default_factory=Stitcher)
    silence: _SilenceClock = field(default_factory=_SilenceClock)
    language: AgreedLanguage = field(default_factory=AgreedLanguage)
    last_cycle_at_s: float = 0.0
    last_commit_at_s: float = 0.0
    recorder: Recorder | EchoRecorder | None = None

    def started(self) -> Recorder:
        if self.recorder is None:
            raise RuntimeError("the lane's recorder has not been started")
        return self.recorder

    def delay_s(self) -> float:
        return pacing_delay_s(self.started().captured_s, self.last_cycle_at_s)

    def backlog_s(self) -> float:
        return self.started().captured_s - self.last_cycle_at_s


def _lanes(
    source: str,
    target: str | None,
    tap: Callable[[np.ndarray], None] | None,
    lane_taps: Mapping[str, Callable[[np.ndarray], None]] | None,
    gate: Callable[[list[str]], SpeechGate | None] | None,
    warnings: list[str],
) -> list[_Lane]:
    if source != BOTH:
        return [_Lane("", source, target, gate(warnings) if gate else None, tap)]
    lanes: list[_Lane] = []
    for lane_source, name in LANES.items():
        # One detector per lane: Silero carries state, and two voices must not share it.
        # The second failure would only repeat the first one's warning.
        speech = gate(warnings if not lanes else []) if gate else None
        lane_target = target if lane_source == "output" else None
        lanes.append(
            _Lane(name, lane_source, lane_target, speech, (lane_taps or {}).get(name) or tap)
        )
    return lanes


def _cycle(
    lane: _Lane,
    client: TranscriptionClient,
    health: dict,
    window_s: float,
    index: Iterator[int],
) -> Iterator[events.Event]:
    recorder = lane.started()
    captured_s = recorder.captured_s
    hop_s = captured_s - lane.last_cycle_at_s

    stalled_for_s = recorder.captured_s - lane.last_commit_at_s
    window = recorder.window(stall_window_s(window_s, stalled_for_s, health.get("task", "")))
    lane.last_cycle_at_s = recorder.captured_s

    level = audio.rms(window)
    heard_window = _speech_in(window, level, lane.speech)
    if heard_window is None:
        lane.last_commit_at_s = lane.last_cycle_at_s
        yield replace(
            lane.silence.event(level, no_speech=level >= config.SILENCE_RMS_THRESHOLD),
            lane=lane.name,
        )
        return
    lane.silence.reset()

    window, gain = audio.normalize(heard_window, config.TARGET_RMS, config.MAX_GAIN)

    started_at = time.monotonic()
    transcript, first_piece_s = client.transcribe(window)
    total_s = time.monotonic() - started_at
    transcript = strip_controls(transcript)
    heard = lane.language.push(client.heard_language)

    confirmed = lane.stitcher.push(transcript)
    if confirmed:
        lane.last_commit_at_s = lane.last_cycle_at_s
    yield events.Cycle(
        index=next(index),
        captured_s=lane.last_cycle_at_s,
        window_s=window.size / config.SAMPLE_RATE_HZ,
        hop_s=hop_s,
        rms=level,
        gain=gain,
        first_piece_s=first_piece_s,
        total_s=total_s,
        transcript=transcript,
        confirmed=confirmed,
        pending=lane.stitcher.pending,
        language=heard,
        lane=lane.name,
    )


def _echo_for_you(lanes: list[_Lane], mode: str) -> _Lane | None:
    if mode == echo_policy.OFF:
        return None
    return next((lane for lane in lanes if lane.name == events.LANE_YOU), None)


def _with_dedup(
    stream: Iterator[events.Event], dedup: echo_text.EchoDedup | None
) -> Iterator[events.Event]:
    for event in stream:
        if dedup is not None and isinstance(event, events.Cycle):
            if event.lane == events.LANE_THEM:
                dedup.heard_them(event.transcript.split())
            elif event.lane == events.LANE_YOU and dedup.echoed(event.confirmed):
                event = replace(event, confirmed=[], dropped=event.confirmed)
        yield event


def caption_events(
    source: str,
    target: str | None,
    window_s: float,
    tap: Callable[[np.ndarray], None] | None = None,
    gate: Callable[[list[str]], SpeechGate | None] | None = None,
    lane_taps: Mapping[str, Callable[[np.ndarray], None]] | None = None,
    echo_cancel: str = echo_policy.OFF,
) -> Iterator[events.Event]:
    client = TranscriptionClient(language=True)
    health = client.wait_ready()
    warnings = [str(warning) for warning in health.get("warnings", [])]
    lanes = _lanes(source, target, tap, lane_taps, gate, warnings)
    cancelling = _echo_for_you(lanes, echo_cancel) if source == BOTH else None
    echo_status = echo_lane.initial_status(echo_cancel) if cancelling else None
    yield _ready_event(health, warnings, echo_status)
    if echo_status is not None:
        yield events.EchoStatus(echo_status.mode, echo_status.active, echo_status.reason)

    indexes = itertools.count(1)
    dedup = echo_text.EchoDedup() if cancelling else None
    last_echo = echo_status

    # Wraps the whole with-block so a Ctrl+C during cleanup still exits cleanly.
    try:
        with contextlib.ExitStack() as stack:
            ref_feed: Callable[[np.ndarray], None] | None = None
            if cancelling is not None:
                you = cancelling
                echo = echo_lane.EchoRecorder(
                    lambda feed: Recorder(source="mic", target=None, tap=feed),
                    echo_cancel,
                    on_gate=you.speech.tap if you.speech else None,
                    on_clean=you.tap,
                )
                you.recorder = stack.enter_context(echo)
                ref_feed = echo.worker.feed_ref
            for lane in lanes:
                if lane.recorder is not None:
                    continue
                feeds = [lane.speech.tap if lane.speech else None, lane.tap]
                if lane.name == events.LANE_THEM:
                    feeds.append(ref_feed)
                recorder = Recorder(source=lane.source, target=lane.target, tap=_taps(*feeds))
                lane.recorder = stack.enter_context(recorder)
            started = time.monotonic()
            while True:
                for lane in lanes:
                    lane.started().check_alive()

                delays = [lane.delay_s() for lane in lanes]
                due = [lane for lane, delay_s in zip(lanes, delays, strict=True) if delay_s <= 0]
                if not due:
                    time.sleep(min(delays))
                    continue
                # One transcription at a time (the server runs them in turn anyway); the lane
                # with the most unheard audio goes first, so neither starves the other.
                lane = max(due, key=_Lane.backlog_s)
                yield from _with_dedup(_cycle(lane, client, health, window_s, indexes), dedup)
                if cancelling is not None and isinstance(
                    cancelling.recorder, echo_lane.EchoRecorder
                ):
                    worker = cancelling.recorder.worker
                    if worker.status != last_echo:
                        last_echo = worker.status
                        yield events.EchoStatus(last_echo.mode, last_echo.active, last_echo.reason)
                    hint = (
                        worker.meter.hint(time.monotonic() - started) if last_echo.active else None
                    )
                    if hint:
                        yield events.Hint(kind="headphones", message=hint)
    except KeyboardInterrupt:
        pass

    if source == BOTH:
        for lane in lanes:
            yield events.Flushed(lane=lane.name, words=lane.stitcher.flush())
        yield events.Stopped()
    else:
        yield events.Stopped(flushed=lanes[0].stitcher.flush())


class TerminalRenderer:
    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self._started = False
        self._lane = ""
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
            self._emit(event.confirmed, event.lane)
        elif isinstance(event, events.Silence):
            if (
                event.lane != events.LANE_YOU
                and event.elapsed_s >= _SILENCE_NOTICE_AFTER_S
                and not event.no_speech
                and not self._silence_reported
            ):
                muted = _MUTED_LINE if event.sink_muted else ""
                print(
                    _SILENCE_NOTICE.format(seconds=event.elapsed_s, muted=muted),
                    file=sys.stderr,
                )
                self._silence_reported = True
        elif isinstance(event, events.Flushed):
            self._emit(event.words, event.lane)
        elif isinstance(event, events.Stopped):
            self._emit(event.flushed)
            print(flush=True)

    def _emit(self, words: list[str], lane: str = "") -> None:
        if not words:
            return
        text = " ".join(words)
        if lane and lane != self._lane:
            print(("\n" if self._started else "") + f"{lane}: {text}", end="", flush=True)
            self._lane = lane
        else:
            print((" " if self._started else "") + text, end="", flush=True)
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
        choices=["output", "mic", BOTH],
        default=config.DEFAULT_SOURCE,
        help="'output' captions system audio (default), 'mic' captions your voice, "
        "'both' captions each as its own lane (you / them), for calls.",
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
        "vinowhisper-tray reads. Human-readable messages stay on stderr.",
    )
    parser.add_argument(
        "--no-speech-gate",
        action="store_true",
        help="Decode every sound above the silence level, not only what the speech "
        "detector hears as someone talking. Mumbling and noise then get captioned too.",
    )
    parser.add_argument(
        "--echo-cancel",
        choices=list(echo_policy.MODES),
        default=echo_policy.AUTO,
        help="With --source both: take their voice out of the mic lane. 'auto' (default) "
        "does it on speakers and not on Bluetooth, USB or headphone outputs.",
    )
    parser.add_argument(
        "--echo-status",
        action="store_true",
        help="Print what --echo-cancel auto would do on the current output, as JSON, then exit.",
    )
    parser.add_argument("--version", action="version", version=f"vinowhisper {VERSION}")
    return parser.parse_args()


def _echo_status(mode: str) -> int:
    status = echo_lane.initial_status(mode)
    print(json.dumps({"mode": status.mode, "active": status.active, "reason": status.reason}))
    return 0


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
    # (pinned by `test_caption.py::test_list_targets_does_not_print_control_characters_a_stream_names`)
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
    # (pinned by `test_caption.py::test_characterization_an_inherited_ignored_sigint_is_undone`)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    args = _parse_args()
    if args.echo_status:
        return _echo_status(args.echo_cancel)
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
    lanes = tuple(LANES.values()) if args.source == BOTH else ()
    writer = session.SessionWriter(args.record, lanes=lanes) if args.record else None
    transcript = None
    if args.transcript:
        # (pinned by `test_caption.py::test_a_mic_session_does_not_track_what_is_playing`)
        tracker = playing.Tracker(args.target) if args.source in ("output", BOTH) else None
        transcript = session.TranscriptWriter(args.transcript, args.source, tracker=tracker)
    try:
        stream = caption_events(
            source=args.source,
            target=args.target,
            window_s=args.window,
            tap=writer.audio_chunk if writer and not lanes else None,
            lane_taps={lane: writer.lane_tap(lane) for lane in lanes} if writer else None,
            echo_cancel=args.echo_cancel,
            gate=None if args.no_speech_gate else speech_gate,
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
            f"  {config.SERVER_LOG}       # what the server said\n"
            "  vinowhisper-doctor        # what is actually missing",
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
                if transcript.path.suffix == bundle.SUFFIX:
                    _queue_summary()
    return 0


def _queue_summary() -> None:
    summary.queue_auto()


if __name__ == "__main__":
    sys.exit(main())
