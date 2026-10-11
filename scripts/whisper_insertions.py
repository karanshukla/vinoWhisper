"""Words Whisper writes that were never spoken, measured against known speech.

Synthesises each sentence with espeak-ng, runs it through the live server exactly as
dictation does (normalize, then decode), under different leading and trailing audio, and
counts words in the transcript that are not in the sentence. Needs the server and
espeak-ng; nothing under tests/ may import this.

    uv run python scripts/whisper_insertions.py [--json out.json]
"""

import argparse
import difflib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from vinowhisper import audio, config, dictate
from vinowhisper.client import TranscriptionClient
from vinowhisper.stitch import _norm

SENTENCES = [
    "Hello, what's going on? Something's wrong, actually.",
    "Um, actually, like, what is it? Something's wrong.",
    "Send the report to Sam by Friday.",
    "So I think we should ship it on Friday.",
    "Let's meet at four in the lobby.",
    "Can you hear me? Are you there?",
    "The build needs to pass before we merge.",
    "Call the dentist tomorrow morning to reschedule my cleaning appointment.",
    "What is the capital of France?",
    "First fix the build. Then update the docs.",
    "I don't know what's going on with the settings window.",
    "Okay.",
]

ROOM_RMS = 0.02


class Lcg:
    def __init__(self, seed: int) -> None:
        self.state = seed

    def uniform(self, n: int) -> np.ndarray:
        out = np.empty(n, dtype=np.float32)
        for i in range(n):
            self.state = (self.state * 1664525 + 1013904223) % 2**32
            out[i] = self.state / 2**31 - 1.0
        return out


def synth(text: str, directory: Path, speed: int) -> np.ndarray:
    wav = directory / "s.wav"
    subprocess.run(
        ["espeak-ng", "-v", "en-us", "-s", str(speed), "-w", str(wav), text],
        check=True,
        capture_output=True,
    )
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(wav), "-ar", "16000", "-ac", "1", "-f", "s16le", "-"],
        check=True,
        capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def room(seconds: float, rng: Lcg) -> np.ndarray:
    noise = rng.uniform(int(seconds * config.SAMPLE_RATE_HZ)) * ROOM_RMS * 1.7
    return noise.astype(np.float32)


def scaled(signal: np.ndarray, rms: float) -> np.ndarray:
    level = float(np.sqrt(np.mean(signal**2))) or 1.0
    return (signal * (rms / level)).astype(np.float32)


def pink(seconds: float, rng: Lcg) -> np.ndarray:
    white = rng.uniform(int(seconds * config.SAMPLE_RATE_HZ))
    out = np.empty_like(white)
    state = 0.0
    for i, value in enumerate(white):
        state = 0.985 * state + float(value)
        out[i] = state
    return scaled(out, ROOM_RMS)


def clicks(seconds: float, rng: Lcg) -> np.ndarray:
    n = int(seconds * config.SAMPLE_RATE_HZ)
    out = room(seconds, rng) * 0.3
    decay = np.exp(-np.arange(80, dtype=np.float32) / 12.0)
    for at in rng.uniform(int(seconds * 6)):
        start = int((at + 1.0) / 2.0 * (n - 80))
        out[start : start + 80] += decay * 0.25
    return out.astype(np.float32)


def babble(seconds: float, directory: Path) -> np.ndarray:
    others = "Thanks for coming. We can start the meeting now. The budget looks fine to me."
    voice = synth(others, directory, 170)
    reps = int(np.ceil(seconds * config.SAMPLE_RATE_HZ / voice.size))
    return scaled(np.tile(voice, reps)[: int(seconds * config.SAMPLE_RATE_HZ)], ROOM_RMS)


def through_segmenter(clip: np.ndarray) -> np.ndarray:
    segmenter = dictate._Segmenter()
    pieces = []
    step = 1600
    for start in range(0, clip.size, step):
        cut = segmenter.add(clip[start : start + step])
        if cut is not None:
            pieces.append(cut)
    pieces.append(segmenter.finish())
    return np.concatenate(pieces)


def words(text: str) -> list[str]:
    return [w for w in (_norm(t) for t in text.split()) if any(c.isalnum() for c in w)]


def compare(spoken: str, heard: str) -> dict:
    a, b = words(spoken), words(heard)
    inserted: list[str] = []
    dropped: list[str] = []
    changed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "insert":
            inserted += b[j1:j2]
        elif tag == "delete":
            dropped += a[i1:i2]
        elif tag == "replace":
            changed += max(i2 - i1, j2 - j1)
            inserted += b[j1:j2]
            dropped += a[i1:i2]
    return {"inserted": inserted, "dropped": dropped, "changed": changed, "exact": a == b}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path)
    parser.add_argument(
        "--segmenter",
        action="store_true",
        help="send each clip through dictate._Segmenter first, as a dictation does",
    )
    parser.add_argument("--speeds", type=int, nargs="+", default=[175])
    args = parser.parse_args()
    client = TranscriptionClient()
    print(client.wait_ready())
    rng = Lcg(7)
    tmp = tempfile.mkdtemp(dir=Path.home() / ".cache")
    variants = {
        "clean": lambda s: s,
        "tail 0.25s silence": lambda s: np.concatenate([s, np.zeros(4000, np.float32)]),
        "tail 1.5s room": lambda s: np.concatenate([s, room(1.5, rng)]),
        "lead+tail room": lambda s: np.concatenate([room(0.5, rng), s, room(1.0, rng)]),
        "tail 2.5s pink": lambda s: np.concatenate([s, pink(2.5, rng)]),
        "tail 2.5s clicks": lambda s: np.concatenate([s, clicks(2.5, rng)]),
        "tail 2.5s babble": lambda s: np.concatenate([s, babble(2.5, Path(tmp))]),
        "tail 4s pink": lambda s: np.concatenate([s, pink(4.0, rng)]),
    }
    rows = []
    if True:
        for text in SENTENCES:
            for speed in args.speeds:
                speech = synth(text, Path(tmp), speed)
                for name, make in variants.items():
                    clip = make(speech)
                    if args.segmenter:
                        clip = through_segmenter(clip)
                    samples, _ = audio.normalize(clip, config.TARGET_RMS, config.MAX_GAIN)
                    transcript, _ = client.transcribe(samples)
                    heard = dictate.clean(transcript)
                    rows.append(
                        {"spoken": text, "speed": speed, "variant": name, "heard": heard}
                        | compare(text, heard)
                    )
    total = len(rows)
    print(
        f"\n{total} decodes, {len(SENTENCES)} sentences x {len(args.speeds)} speeds x {len(variants)} variants"
    )
    print(f"{'variant':20} {'exact':>6} {'with inserted words':>20}")
    for name in variants:
        mine = [r for r in rows if r["variant"] == name]
        exact = sum(r["exact"] for r in mine)
        ins = sum(bool(r["inserted"]) for r in mine)
        print(f"{name:20} {exact:3d}/{len(mine):<3d} {ins:10d}/{len(mine)}")
    print("\nEvery transcript with a word that was not spoken:")
    for r in rows:
        if r["inserted"]:
            print(
                f"  [{r['variant']}, {r['speed']}wpm] {r['spoken']!r}\n      heard {r['heard']!r}\n      inserted {r['inserted']}"
            )
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
