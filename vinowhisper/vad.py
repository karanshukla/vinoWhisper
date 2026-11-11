import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests

from . import config, source

PINS_PATH = Path(__file__).resolve().parent / "vad_model.json"
MODEL_DIR = config.MODEL_ROOT / "silero-vad"

CHUNK_SAMPLES = 512
CONTEXT_SAMPLES = 64
SPEECH_PROBABILITY = 0.5


class VadError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubFiles(source.Source):
    path: str = ""

    def url(self, name: str) -> str:
        return (
            f"https://raw.githubusercontent.com/{self.model_id}/{self.revision}/{self.path}/{name}"
        )


def pinned(path: Path | None = None) -> GitHubFiles | None:
    try:
        raw = json.loads((path or PINS_PATH).read_text(encoding="utf-8"))
        files = raw["files"]
        return GitHubFiles(
            model_id=str(raw["repo"]),
            revision=str(raw["revision"]),
            recorded=str(raw.get("recorded", "")),
            sha256={name: str(meta["sha256"]) for name, meta in files.items()},
            sizes={name: int(meta["size"]) for name, meta in files.items()},
            path=str(raw["path"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def model_path(directory: Path | None = None, session: requests.Session | None = None) -> Path:
    pin = pinned()
    if pin is None:
        raise VadError(f"{PINS_PATH} is missing or unreadable; reinstall vinowhisper")
    directory = directory or MODEL_DIR
    (name,) = pin.sha256
    if directory.is_dir() and not source.problems(pin, directory):
        return directory / name
    try:
        source.fetch(pin, directory, session=session, say=lambda line: None)
    except (requests.RequestException, OSError, source.SourceError) as exc:
        raise VadError(f"could not fetch the speech detector ({exc})") from exc
    return directory / name


class SpeechDetector:
    def __init__(self, path: Path) -> None:
        import openvino as ov

        core = ov.Core()
        # One thread: a 512-sample LSTM step is too small to split, and the NPU is busy decoding.
        compiled = core.compile_model(
            core.read_model(str(path)), "CPU", {"INFERENCE_NUM_THREADS": 1}
        )
        self._request = compiled.create_infer_request()
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._last = 0.0

    # The highest speech probability among the 32 ms steps this chunk completes.
    def __call__(self, chunk: np.ndarray) -> float:
        self._pending = np.concatenate([self._pending, chunk.astype(np.float32, copy=False)])
        best: float | None = None
        while self._pending.size >= CHUNK_SAMPLES:
            step, self._pending = self._pending[:CHUNK_SAMPLES], self._pending[CHUNK_SAMPLES:]
            window = np.concatenate([self._context, step])[None, :]
            out = self._request.infer({"input": window, "state": self._state})
            self._state = np.asarray(out["stateN"], dtype=np.float32)
            self._context = step[-CONTEXT_SAMPLES:]
            self._last = float(np.asarray(out["output"]).reshape(-1)[0])
            best = self._last if best is None else max(best, self._last)
        return self._last if best is None else best


def detector(say: Callable[[str], None]) -> Callable[[np.ndarray], float] | None:
    try:
        return SpeechDetector(model_path())
    except (VadError, RuntimeError, ImportError, OSError) as exc:
        say(f"{exc}; paragraphs break on silence only")
        return None
