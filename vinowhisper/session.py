import argparse
import contextlib
import fcntl
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import IO

import numpy as np

from . import bundle, config, events
from .paragraphs import ParagraphBreaker

AUDIO_NAME = "audio.wav"
EVENTS_NAME = "events.jsonl"

_PCM_SCALE = 32767.0


class SessionWriter:
    def __init__(self, directory: Path) -> None:
        import wave

        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._wav_file = _open_private(directory / AUDIO_NAME, "wb")
        self._wav = wave.open(self._wav_file, "wb")  # noqa: SIM115
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(config.SAMPLE_RATE_HZ)
        self._events = _open_private(directory / EVENTS_NAME, "w")

    def audio_chunk(self, samples: np.ndarray) -> None:
        pcm = np.clip(samples * _PCM_SCALE, -32768, 32767).astype("<i2")
        self._wav.writeframes(pcm.tobytes())

    def event(self, event: events.Event) -> None:
        self._events.write(json.dumps(events.to_dict(event)) + "\n")
        self._events.flush()

    def close(self) -> None:
        self._wav.close()
        # wave does not close a file object it was handed.
        self._wav_file.close()
        self._events.close()


def _open_private(path: Path, mode: str) -> IO:
    mode_flag = os.O_APPEND if "a" in mode else os.O_TRUNC
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | mode_flag | os.O_CLOEXEC, 0o600)
    # The mode above only applies on creation; an overwritten recording keeps its old one.
    os.fchmod(fd, 0o600)
    if "b" in mode:
        return os.fdopen(fd, mode)
    return os.fdopen(fd, mode, encoding="utf-8")


_SOURCE_LABELS = {"output": "system audio", "mic": "microphone"}


class TranscriptWriter:
    def __init__(
        self,
        target: Path,
        source: str,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.path: Path | None = None
        self._target = target
        self._source = _SOURCE_LABELS.get(source, source)
        self._clock = clock
        self._now = now
        self._started_at = clock()
        self._started = now()
        self._device = "?"
        self._language = ""
        self._task = ""
        self._bundle_on_close = False
        self._breaker = ParagraphBreaker()
        self._file: IO | None = None
        self._first = True
        self._closed = False

    def event(self, event: events.Event) -> None:
        if self._closed:
            return
        if isinstance(event, events.Ready):
            self._device = event.device
            self._language, self._task = event.language, event.task
            self._started_at = self._clock()
            self._started = self._now()
        elif isinstance(event, events.Cycle):
            self._words(event.confirmed)
        elif isinstance(event, events.Silence):
            self._breaker.silence(event.elapsed_s)
        elif isinstance(event, events.Stopped):
            self._words(event.flushed)
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        file, self._file = self._file, None
        if file is None:
            return
        try:
            file.write("\n")
            file.close()
        except OSError:
            return
        if self._bundle_on_close and self.path is not None:
            self._bundle(self.path)

    def _bundle(self, transcript: Path) -> None:
        language = self._language or None
        task = self._task or None
        # On failure the .txt stays, and the server's next start bundles it (summary.recover).
        with contextlib.suppress(OSError, bundle.BundleError):
            self.path = bundle.bundle_transcript(
                transcript,
                started=self._started.astimezone(),
                duration_s=int(self._clock() - self._started_at),
                language=language,
                task=task,
                device=None if self._device == "?" else self._device,
                model_id=config.model_id(
                    config.is_multilingual(language or "en", task or config.DEFAULT_TASK)
                ),
            )

    def _words(self, words: list[str]) -> None:
        if not words:
            return
        file = self._file or self._open()
        for word in words:
            if self._breaker.word(word):
                file.write("\n\n" + self._stamp() + word)
            elif self._first:
                file.write(self._stamp() + word)
            else:
                file.write(" " + word)
            self._first = False
        file.flush()

    def _stamp(self) -> str:
        seconds = int(self._clock() - self._started_at)
        return f"[{seconds // 60:02d}:{seconds % 60:02d}] "

    def _open(self) -> IO:
        target = self._target
        if target.is_dir() or (not target.exists() and not target.suffix):
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = target / self._started.strftime("%Y-%m-%d-%H%M%S.txt")
            self._bundle_on_close = not bundle.bundle_path_for(target).exists()
        else:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = target
        self._file = _open_private(target, "a")
        # Held until close, so recovery can tell a live session from a crashed one.
        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.fstat(self._file.fileno()).st_size:
            self._file.write("\n")
        stamp = f"{self._started:%Y-%m-%d %H:%M}"
        self._file.write(f"vinoWhisper transcript, {stamp}, {self._source}, {self._device}\n\n")
        return self._file


def read_events(directory: Path) -> list[dict]:
    path = directory / EVENTS_NAME
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_cycles(directory: Path) -> Iterator[dict]:
    for record in read_events(directory):
        if record["event"] == "Cycle":
            yield record


def read_audio(directory: Path) -> np.ndarray:
    import wave

    with wave.open(str(directory / AUDIO_NAME), "rb") as handle:
        if handle.getframerate() != config.SAMPLE_RATE_HZ:
            raise ValueError(f"expected {config.SAMPLE_RATE_HZ}Hz, got {handle.getframerate()}Hz")
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / _PCM_SCALE


def _describe(path: Path, state: dict) -> dict:
    from . import summary

    entry: dict = {"path": str(path), "name": path.name}
    try:
        read = bundle.read(path)
    except bundle.BundleError as exc:
        return {**entry, "error": str(exc)}
    files = read.attestation.get("files", {})
    return {
        **entry,
        **{
            key: read.session.get(key)
            for key in ("started", "duration_s", "source", "language", "task")
        },
        "summary": read.summary is not None,
        "title": read.summary_title,
        "summary_made_by": files.get(bundle.SUMMARY, {}).get("made_by"),
        "declined": path.name in state["declined"],
        "failed": state["failures"].get(path.name, 0) >= summary.MAX_FAILURES,
        "verified": not bundle.verify(read),
    }


def _bundle_arg(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    if path.suffix != bundle.SUFFIX:
        raise argparse.ArgumentTypeError(f"{raw} is not a {bundle.SUFFIX} file")
    return path


def _kick(paths: list[Path] | None = None) -> str | None:
    import requests

    from .client import TranscriptionClient

    try:
        TranscriptionClient().summarise(paths)
    except requests.RequestException as exc:
        return f"the server is not reachable ({exc}); summaries run when it next starts"
    return None


def main(argv: list[str] | None = None) -> int:
    from . import llm, summary

    parser = argparse.ArgumentParser(
        prog="vinowhisper-session",
        description="Saved caption sessions (.vwsession bundles) and their summaries.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="every saved session, newest first")
    listing.add_argument(
        "--json", action="store_true", help="one JSON object for the settings window"
    )
    show = commands.add_parser("show", help="print a session's transcript or summary")
    show.add_argument("bundle", type=_bundle_arg)
    show.add_argument(
        "--part", choices=("transcript", "summary", "attestation"), default="transcript"
    )
    for name, text in (
        ("summarise", "summarise (again) in the background, now"),
        ("delete-summary", "remove the summary and don't make it again"),
        ("delete", "delete the whole session"),
        ("verify", "check each file against the attestation's sha256"),
    ):
        commands.add_parser(name, help=text).add_argument("bundles", type=_bundle_arg, nargs="+")
    commands.add_parser(
        "backfill", help="bundle transcripts saved before summaries, and summarise them"
    )
    args = parser.parse_args(argv)

    if args.command == "list":
        state = summary.load_state()
        sessions = [_describe(path, state) for path in bundle.listing()]
        loose = [p for p in summary.loose_transcripts() if p.stat().st_mtime < state["since"]]
        pin = llm.pinned()
        report = {
            "directory": str(config.TRANSCRIPT_DIR),
            "auto": summary.auto_enabled(),
            "model_ready": pin is not None and llm.is_present(pin),
            "backfill": len(loose),
            "sessions": sessions,
        }
        if args.json:
            print(json.dumps(report))
            return 0
        for entry in sessions:
            mark = "summary" if entry.get("summary") else "no summary"
            print(f"{entry['name']}  {entry.get('title') or ''}  ({mark})")
        if loose:
            print(f"{len(loose)} older transcripts can be bundled: vinowhisper-session backfill")
        return 0

    if args.command == "show":
        try:
            read = bundle.read(args.bundle)
        except bundle.BundleError as exc:
            print(f"[vinowhisper] {exc}", file=sys.stderr)
            return 1
        if args.part == "summary" and read.summary is None:
            print(f"[vinowhisper] {args.bundle.name} has no summary yet", file=sys.stderr)
            return 1
        text = {
            "transcript": read.transcript,
            "summary": read.summary or "",
            "attestation": json.dumps(read.attestation, indent=2, ensure_ascii=False) + "\n",
        }[args.part]
        sys.stdout.write(text)
        return 0

    if args.command == "backfill":
        made = summary.backfill()
        print(f"[vinowhisper] bundled {len(made)} transcripts", file=sys.stderr)
        if made and summary.auto_enabled():
            problem = _kick()
            if problem:
                print(f"[vinowhisper] {problem}", file=sys.stderr)
        return 0

    status = 0
    for path in args.bundles:
        try:
            status |= _bundle_command(args.command, path)
        except (OSError, bundle.BundleError) as exc:
            print(f"[vinowhisper] {path.name}: {exc}", file=sys.stderr)
            status = 1
    if args.command == "summarise" and status == 0:
        problem = _kick(args.bundles)
        if problem:
            print(f"[vinowhisper] {problem}", file=sys.stderr)
            return 1
    return status


def _bundle_command(command: str, path: Path) -> int:
    from . import summary

    if command == "summarise":
        bundle.read(path)
        return 0
    if command == "verify":
        bad = bundle.verify(bundle.read(path))
        print(f"{path.name}: {'ok' if not bad else 'changed since attested: ' + ', '.join(bad)}")
        return 1 if bad else 0
    state = summary.load_state()
    if command == "delete-summary":
        bundle.drop_summary(bundle.read(path))
        if path.name not in state["declined"]:
            state["declined"].append(path.name)
    elif command == "delete":
        path.unlink()
        state["failures"].pop(path.name, None)
        if path.name in state["declined"]:
            state["declined"].remove(path.name)
    summary.save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
