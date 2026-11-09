import argparse
import hashlib
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Protocol

import numpy as np
import requests

from . import __version__, audio, bundle, config, distro, summary
from .client import TranscriptionClient
from .dictate import clean
from .paragraphs import ParagraphBreaker

FRAME_SAMPLES = config.SAMPLE_RATE_HZ // 10
_FRAME_BYTES = FRAME_SAMPLES * audio.BYTES_PER_SAMPLE
_PAUSE_FRAMES = max(1, round(config.SEGMENT_PAUSE_S * 10))
_PAD_FRAMES = max(1, round(config.TRIM_PAD_S * 10))
_EMPTY = np.zeros(0, dtype=np.float32)

FFMPEG = "ffmpeg"
GSTREAMER = "gst-launch-1.0"


class TranscribeError(Exception):
    pass


class _Client(Protocol):
    def wait_ready(self) -> dict: ...
    def transcribe(
        self, samples: np.ndarray, background: bool = False
    ) -> tuple[str, float | None]: ...


@dataclass(frozen=True)
class Piece:
    start_s: float
    end_s: float
    samples: np.ndarray


def decoder_argv(path: Path, which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    if which(FFMPEG):
        return [
            FFMPEG, "-nostdin", "-v", "error",
            # file: so a name with a colon is never read as a protocol.
            "-i", f"file:{path}",
            "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-ac", "1", "-ar", str(config.SAMPLE_RATE_HZ), "-f", "f32le", "-",
        ]  # fmt: skip
    if which(GSTREAMER):
        caps = f"audio/x-raw,format=F32LE,channels=1,rate={config.SAMPLE_RATE_HZ}"
        return [
            GSTREAMER, "-q", "filesrc", f"location={path}",
            "!", "decodebin", "!", "audioconvert", "!", "audioresample",
            "!", caps, "!", "fdsink", "fd=1",
        ]  # fmt: skip
    return None


def no_decoder_message(path: Path) -> str:
    lines = [
        f"{path.name} needs ffmpeg or GStreamer to decode, and neither is installed "
        f"(only 16-bit {config.SAMPLE_RATE_HZ} Hz WAV files are read without them)",
        *distro.remediation(distro.MEDIA_DECODER).lines(),
    ]
    return "\n".join(lines)


def probe_duration_s(path: Path, which: Callable[[str], str | None] = shutil.which) -> float | None:
    if not which("ffprobe"):
        return _wav_duration_s(path)
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                f"file:{path}",
            ],  # fmt: skip
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        seconds = float(result.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return seconds if seconds > 0 else None


def _wav_duration_s(path: Path) -> float | None:
    import wave

    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getnframes() / handle.getframerate()
    except (OSError, EOFError, wave.Error, ZeroDivisionError):
        return None


def _wav_frames(path: Path) -> Iterator[np.ndarray]:
    import wave

    try:
        handle = wave.open(str(path), "rb")  # noqa: SIM115
    except (OSError, EOFError, wave.Error) as exc:
        raise TranscribeError(no_decoder_message(path)) from exc
    with handle:
        channels = handle.getnchannels()
        if handle.getframerate() != config.SAMPLE_RATE_HZ or handle.getsampwidth() != 2:
            raise TranscribeError(no_decoder_message(path))
        while True:
            raw = handle.readframes(FRAME_SAMPLES)
            if not raw:
                return
            pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            yield pcm.reshape(-1, channels).mean(axis=1) if channels > 1 else pcm


def _piped_frames(path: Path, argv: list[str]) -> Iterator[np.ndarray]:
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors
            )
        except OSError as exc:
            raise TranscribeError(f"could not run {argv[0]}: {exc}") from exc
        stdout: IO[bytes] = process.stdout  # type: ignore[assignment]
        try:
            while True:
                raw = stdout.read(_FRAME_BYTES)
                usable = len(raw) - len(raw) % audio.BYTES_PER_SAMPLE
                if usable:
                    yield np.frombuffer(raw[:usable], dtype="<f4").astype(np.float32)
                if len(raw) < _FRAME_BYTES:
                    break
            status = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            stdout.close()
        if status != 0:
            errors.seek(0)
            said = errors.read().decode("utf-8", "replace").strip().splitlines()
            reason = said[-1] if said else f"{argv[0]} exited with status {status}"
            raise TranscribeError(f"could not decode {path.name}: {reason}")


def frames(path: Path, which: Callable[[str], str | None] = shutil.which) -> Iterator[np.ndarray]:
    argv = decoder_argv(path, which)
    if argv is None:
        return _wav_frames(path)
    return _piped_frames(path, argv)


# Pieces of at most max_s, each cut at the quietest pause past min_s: no overlap, no stitching.
class Segmenter:
    def __init__(
        self, min_s: float = config.FILE_PIECE_MIN_S, max_s: float = config.MAX_WINDOW_S
    ) -> None:
        self._max_frames = int(max_s * 10)
        self._min_frames = min(int(min_s * 10), self._max_frames)
        self._frames: list[np.ndarray] = []
        self._levels: list[float] = []
        self._held = 0
        self._start = 0

    def add(self, chunk: np.ndarray) -> list[Piece]:
        out: list[Piece] = []
        self._frames.append(chunk)
        self._levels.append(audio.rms(chunk))
        self._held += chunk.size
        while self._held >= self._max_frames * FRAME_SAMPLES and len(self._frames) > 1:
            out.append(self._take(self._cut_index()))
        return out

    def finish(self) -> list[Piece]:
        if not self._frames:
            return []
        return [self._take(len(self._frames))]

    def _cut_index(self) -> int:
        levels = self._levels
        last = len(levels) - _PAUSE_FRAMES
        first = min(self._min_frames, last)
        best, best_level = len(levels), float("inf")
        for index in range(first, last + 1):
            level = sum(levels[index : index + _PAUSE_FRAMES])
            if level < best_level:
                best, best_level = index + (_PAUSE_FRAMES + 1) // 2, level
        return max(1, min(best, len(levels) - 1))

    def _take(self, count: int) -> Piece:
        head, self._frames = self._frames[:count], self._frames[count:]
        levels, self._levels = self._levels[:count], self._levels[count:]
        taken = sum(frame.size for frame in head)
        start, self._start = self._start, self._start + taken
        self._held -= taken
        rate = config.SAMPLE_RATE_HZ
        loud = [i for i, level in enumerate(levels) if level >= config.SILENCE_RMS_THRESHOLD]
        if not loud:
            return Piece(start / rate, (start + taken) / rate, _EMPTY)
        first = max(0, loud[0] - _PAD_FRAMES)
        last = min(len(head), loud[-1] + 1 + _PAD_FRAMES)
        skipped = sum(frame.size for frame in head[:first])
        kept = np.concatenate(head[first:last])
        begin = start + skipped
        return Piece(begin / rate, (begin + kept.size) / rate, kept)


class Transcript:
    def __init__(self, started: datetime, source: str, device: str) -> None:
        self._parts = [f"vinoWhisper transcript, {started:%Y-%m-%d %H:%M}, {source}, {device}\n\n"]
        self._breaker = ParagraphBreaker()
        self._quiet_from_s = 0.0
        self.words = 0

    def piece(self, piece: Piece, words: list[str]) -> None:
        if not words:
            return
        self._breaker.silence(piece.start_s - self._quiet_from_s)
        self._quiet_from_s = piece.end_s
        span = piece.end_s - piece.start_s
        for index, word in enumerate(words):
            # No word times: a break inside a piece is stamped by its place among the words.
            at_s = piece.start_s + span * index / len(words)
            if self._breaker.word(word):
                self._parts.append("\n\n" + _stamp(at_s) + word)
            elif self.words == 0:
                self._parts.append(_stamp(at_s) + word)
            else:
                self._parts.append(" " + word)
            self.words += 1

    def text(self) -> str:
        return "".join(self._parts) + "\n"


def _stamp(seconds: float) -> str:
    whole = int(seconds)
    return f"[{whole // 60:02d}:{whole % 60:02d}] "


def file_sha256(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def model_id_for(health: dict) -> str:
    if health.get("model") == config.CANARY_MODEL:
        return config.CANARY_MODEL_ID
    language = str(health.get("language") or config.DEFAULT_LANGUAGE)
    task = str(health.get("task") or config.DEFAULT_TASK)
    return config.model_id(config.is_multilingual(language, task))


def check_server(health: dict, model: str | None, language: str | None, task: str | None) -> None:
    running = {
        "model": str(health.get("model") or config.DEFAULT_MODEL),
        "language": str(health.get("language") or config.DEFAULT_LANGUAGE),
        "task": str(health.get("task") or config.DEFAULT_TASK),
    }
    asked = {"model": model, "language": language, "task": task}
    differing = [
        f"--{name} {value}" for name, value in asked.items() if value not in (None, running[name])
    ]
    if not differing:
        return
    now = f"{running['model']}, {running['language']}, {running['task']}"
    raise TranscribeError(
        f"the server is running {now}, not {' '.join(differing)}; it holds one speech model "
        "at a time.\n"
        "  switch it under Settings → Speech (or `vinowhisper-setup --language … --model …`), "
        "then run this again"
    )


def _free_bundle_path(directory: Path, started: datetime) -> Path:
    stem = started.strftime("%Y-%m-%d-%H%M%S")
    path = directory / f"{stem}{bundle.SUFFIX}"
    count = 1
    while path.exists() or path.with_suffix(".txt").exists():
        count += 1
        path = directory / f"{stem}-{count}{bundle.SUFFIX}"
    return path


def transcribe_file(
    path: Path,
    client: _Client,
    emit: Callable[[dict], None],
    *,
    directory: Path | None = None,
    model: str | None = None,
    language: str | None = None,
    task: str | None = None,
    now: Callable[[], datetime] = datetime.now,
    which: Callable[[str], str | None] = shutil.which,
) -> Path:
    if not path.is_file():
        raise TranscribeError(f"{path} is not a file")
    health = client.wait_ready()
    check_server(health, model, language, task)
    device = str(health.get("device", "?"))
    emit(
        {
            "event": "Ready",
            "device": device,
            "degraded": bool(health.get("degraded", False)),
            "warnings": [str(warning) for warning in health.get("warnings", [])],
        }
    )

    started = now().astimezone()
    duration_s = probe_duration_s(path, which)
    name = " ".join(path.name.split())
    transcript = Transcript(started, name, device)
    segmenter = Segmenter()
    decoded_s = 0.0
    working_since = time.monotonic()

    def decode(pieces: list[Piece]) -> None:
        nonlocal decoded_s
        for piece in pieces:
            if piece.samples.size:
                samples, _ = audio.normalize(piece.samples, config.TARGET_RMS, config.MAX_GAIN)
                text, _ = client.transcribe(samples, background=True)
                transcript.piece(piece, clean(text).split())
            decoded_s = piece.end_s
            emit(
                {
                    "event": "Progress",
                    "position_s": round(decoded_s, 1),
                    "duration_s": None if duration_s is None else round(duration_s, 1),
                    "elapsed_s": round(time.monotonic() - working_since, 1),
                    "words": transcript.words,
                }
            )

    total = 0
    for frame in frames(path, which):
        total += frame.size
        decode(segmenter.add(frame))
    decode(segmenter.finish())

    if total == 0:
        raise TranscribeError(f"{path.name} has no audio in it")
    if transcript.words == 0:
        raise TranscribeError(f"no speech found in {path.name}; nothing was saved")

    digest, size = file_sha256(path)
    language_ran = str(health.get("language") or "") or None
    task_ran = str(health.get("task") or "") or None
    attestation = bundle.new_attestation(
        started,
        int(total / config.SAMPLE_RATE_HZ),
        name,
        language_ran,
        task_ran,
        None if device == "?" else device,
        model_id_for(health),
        file={"name": path.name, "sha256": digest, "bytes": size},
    )
    target = _free_bundle_path(directory or config.TRANSCRIPT_DIR, started)
    bundle.write(bundle.Bundle(target, transcript.text(), attestation))
    emit(
        {
            "event": "Done",
            "path": str(target),
            "words": transcript.words,
            "audio_s": round(total / config.SAMPLE_RATE_HZ, 1),
            "elapsed_s": round(time.monotonic() - working_since, 1),
        }
    )
    return target


def _clock(seconds: float) -> str:
    whole = int(seconds)
    if whole >= 3600:
        return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"
    return f"{whole // 60}:{whole % 60:02d}"


class _Human:
    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._tty = stream.isatty()
        self._last_line_at = 0.0

    def __call__(self, record: dict) -> None:
        event = record["event"]
        if event == "Ready":
            print(f"[vinowhisper] transcribing on {record['device']}", file=self._stream)
            for warning in record["warnings"]:
                print(f"[vinowhisper] WARNING: {warning}", file=self._stream)
        elif event == "Progress":
            self._progress(record)
        elif event == "Done":
            if self._tty:
                print(file=self._stream)
            speed = record["audio_s"] / record["elapsed_s"] if record["elapsed_s"] else 0.0
            print(
                f"[vinowhisper] {_clock(record['audio_s'])} of audio, {record['words']} words, "
                f"in {_clock(record['elapsed_s'])} ({speed:.1f}x real time)",
                file=self._stream,
            )

    def _progress(self, record: dict) -> None:
        total = record["duration_s"]
        text = _clock(record["position_s"])
        if total:
            share = min(100, int(100 * record["position_s"] / total))
            text = f"{text} / {_clock(total)} ({share}%)"
        if self._tty:
            print(f"\r[vinowhisper] {text}  ", end="", file=self._stream, flush=True)
        elif time.monotonic() - self._last_line_at >= 10:
            self._last_line_at = time.monotonic()
            print(f"[vinowhisper] {text}", file=self._stream, flush=True)


def _json_lines(record: dict) -> None:
    print(json.dumps(record), flush=True)


def _queue_summary() -> None:
    if not summary.auto_enabled():
        return
    # Unreachable is fine: the server picks up unsummarised sessions when it next starts.
    try:
        TranscriptionClient().summarise()
    except requests.RequestException:
        return


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vinowhisper-transcribe",
        description="Transcribe an audio or video file into a saved session (.vwsession).",
    )
    parser.add_argument("file", type=Path, help="any file ffmpeg or GStreamer can decode")
    parser.add_argument(
        "--model",
        choices=config.MODELS,
        help="refuse to run unless the server holds this speech model",
    )
    parser.add_argument(
        "--language", metavar="LANGUAGE", help="refuse to run unless the server is set to this"
    )
    parser.add_argument("--task", choices=config.TASKS, help="likewise, for translate")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="DIR",
        help=f"where the session is saved (default: {config.TRANSCRIPT_DIR}, where the "
        "sessions window and summaries look)",
    )
    parser.add_argument(
        "--json", action="store_true", help="progress as JSON lines on stdout, for the GUI"
    )
    parser.add_argument("--version", action="version", version=f"vinowhisper {__version__}")
    args = parser.parse_args(argv)

    emit: Callable[[dict], None] = _json_lines if args.json else _Human(sys.stderr)
    signal.signal(signal.SIGTERM, _interrupt)
    path = args.file.expanduser().resolve()
    directory = args.output.expanduser() if args.output is not None else None
    try:
        saved = transcribe_file(
            path,
            TranscriptionClient(),
            emit,
            directory=directory,
            model=args.model,
            language=args.language,
            task=args.task,
        )
    except KeyboardInterrupt:
        return _fail(args.json, "cancelled; nothing was saved", 130)
    except requests.RequestException as exc:
        return _fail(
            args.json,
            f"the transcription server is not reachable at {config.server_address()} ({exc})\n"
            "  run vinowhisper-doctor to see what is missing",
        )
    except (TranscribeError, bundle.BundleError, OSError) as exc:
        return _fail(args.json, str(exc))
    if not args.json:
        print(saved)
    if directory is None:
        _queue_summary()
    return 0


def _fail(as_json: bool, message: str, status: int = 1) -> int:
    if as_json:
        _json_lines({"event": "Error", "message": message.splitlines()[0]})
    for line in message.splitlines():
        print(line if line.startswith("  ") else f"[vinowhisper] {line}", file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
