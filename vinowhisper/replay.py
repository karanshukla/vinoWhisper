"""Re-run a recorded session (vinowhisper-caption --record DIR).

Two modes, deliberately separate because they need different things:

    vinowhisper-replay DIR --restitch
        Offline. Feeds the recorded transcripts back through the Stitcher.
        No NPU, no server, no audio. This is the one to use while changing
        stitch.py: the model output is frozen, so any difference in what gets
        printed is your change and nothing else.

    vinowhisper-replay DIR --sweep 8,12,16,20
        Needs the NPU and a running server. Slices the recorded audio at a
        fixed hop and transcribes it at each window size, so the latency cost
        of --window is measured on your own audio rather than guessed.
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import requests

from . import audio, config, session
from .client import TranscriptionClient
from .stitch import Stitcher

# Fixed, so every window size in a sweep sees the same number of decodes over
# the same audio. Not the hop a live run would use (that's whatever the last
# cycle took), which is the point: this isolates window size from pacing.
_SWEEP_HOP_S = 4.0


def _restitch(directory: Path, verbose: bool) -> int:
    cycles = list(session.read_cycles(directory))
    if not cycles:
        print(f"no Cycle events in {directory}", file=sys.stderr)
        return 1

    stitcher = Stitcher()
    printed: list[str] = []
    for record in cycles:
        confirmed = stitcher.push(record["transcript"])
        printed.extend(confirmed)
        if verbose:
            print(
                f"--- cycle {record['index']}: +{len(confirmed)} confirmed, "
                f"{len(stitcher.pending)} pending\n"
                f"    raw:  {record['transcript']}\n"
                f"    new:  {' '.join(confirmed)}",
                file=sys.stderr,
            )
    printed.extend(stitcher.flush())

    original = []
    for record in cycles:
        original.extend(record["confirmed"])

    print(" ".join(printed))
    if original and original != printed:
        print(
            f"\n[replay] differs from the recorded run: "
            f"{len(original)} words then, {len(printed)} now.",
            file=sys.stderr,
        )
        _print_first_divergence(original, printed)
    elif original:
        print(f"\n[replay] identical to the recorded run ({len(printed)} words).", file=sys.stderr)
    return 0


def _print_first_divergence(before: list[str], after: list[str]) -> None:
    for i, (old, new) in enumerate(zip(before, after)):
        if old != new:
            print(
                f"[replay] first difference at word {i}:\n"
                f"    then: ...{' '.join(before[max(0, i - 6) : i + 6])}\n"
                f"    now:  ...{' '.join(after[max(0, i - 6) : i + 6])}",
                file=sys.stderr,
            )
            return
    shorter, longer = (before, after) if len(before) < len(after) else (after, before)
    print(
        f"[replay] identical for {len(shorter)} words, then one side continues: "
        f"{' '.join(longer[len(shorter) : len(shorter) + 12])}",
        file=sys.stderr,
    )


def _sweep(directory: Path, window_sizes: list[float]) -> int:
    samples = session.read_audio(directory)
    duration_s = samples.size / config.SAMPLE_RATE_HZ
    print(f"[replay] {duration_s:.0f}s of audio, hop {_SWEEP_HOP_S}s\n", file=sys.stderr)

    client = TranscriptionClient()
    client.wait_ready()

    rows = []
    for window_s in window_sizes:
        timings: list[float] = []
        first_pieces: list[float] = []
        word_counts: list[int] = []
        position_s = window_s
        while position_s <= duration_s:
            end = int(position_s * config.SAMPLE_RATE_HZ)
            start = max(0, end - int(window_s * config.SAMPLE_RATE_HZ))
            window = samples[start:end]
            if audio.rms(window) >= config.SILENCE_RMS_THRESHOLD:
                window, _ = audio.normalize(window, config.TARGET_RMS, config.MAX_GAIN)
                started_at = time.monotonic()
                transcript, first_piece_s = client.transcribe(window)
                timings.append(time.monotonic() - started_at)
                if first_piece_s is not None:
                    first_pieces.append(first_piece_s)
                word_counts.append(len(transcript.split()))
            position_s += _SWEEP_HOP_S
        if timings:
            rows.append((window_s, timings, first_pieces, word_counts))
            print(f"  {window_s:.0f}s window: {len(timings)} decodes", file=sys.stderr)

    if not rows:
        print("no non-silent windows to measure", file=sys.stderr)
        return 1

    print("\n| window | decodes | mean | p90 | first piece | words/decode | est. lag |")
    print("|---|---|---|---|---|---|---|")
    for window_s, timings, first_pieces, word_counts in rows:
        mean = float(np.mean(timings))
        p90 = float(np.percentile(timings, 90))
        first = float(np.mean(first_pieces)) if first_pieces else float("nan")
        words = float(np.mean(word_counts))
        # The commit policy needs two cycles to agree, and the hop is whatever
        # the last cycle took, so this is the floor on how far behind the audio
        # a caption lands.
        print(
            f"| {window_s:.0f}s | {len(timings)} | {mean:.2f}s | {p90:.2f}s | "
            f"{first:.2f}s | {words:.0f} | ~{2 * mean:.1f}s |"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-run a recorded caption session.")
    parser.add_argument("directory", type=Path, help="A --record directory.")
    parser.add_argument(
        "--restitch",
        action="store_true",
        help="Re-run the recorded transcripts through the stitcher. No NPU needed.",
    )
    parser.add_argument(
        "--sweep",
        metavar="8,12,16",
        help="Comma-separated window sizes to benchmark against the recorded "
        "audio. Needs the NPU and a running server.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Per-cycle detail.")
    args = parser.parse_args()

    if not args.directory.is_dir():
        print(f"not a directory: {args.directory}", file=sys.stderr)
        return 2
    if bool(args.restitch) == bool(args.sweep):
        print("pass exactly one of --restitch or --sweep", file=sys.stderr)
        return 2

    try:
        if args.restitch:
            return _restitch(args.directory, args.verbose)
        sizes = [float(part) for part in args.sweep.split(",") if part.strip()]
        if not sizes or any(not 0 < size <= config.MAX_WINDOW_S for size in sizes):
            print(f"--sweep sizes must be in (0, {config.MAX_WINDOW_S}]", file=sys.stderr)
            return 2
        return _sweep(args.directory, sizes)
    except (OSError, ValueError, KeyError) as exc:
        print(f"could not read session: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"server not reachable at {config.SERVER_URL}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
