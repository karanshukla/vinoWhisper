from pathlib import Path
from typing import Protocol

import numpy as np

from .daf import DAF, M, load_weights

CHUNK = 512


class GateModel(Protocol):
    block: int

    def reset(self) -> None: ...

    def __call__(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray: ...


class Echo:
    def __init__(self, gate: GateModel, weights: Path | None = None) -> None:
        if gate.block != CHUNK:
            raise ValueError(f"the gate step takes {gate.block} samples at a time, not {CHUNK}")
        self._daf = DAF(load_weights(weights) if weights else load_weights())
        self._gate = gate
        self._mic = np.zeros(0, np.float32)
        self._ref = np.zeros(0, np.float32)

    @property
    def locked(self) -> bool:
        return self._daf.locked

    def reset(self) -> None:
        self._daf.reset()
        self._gate.reset()
        self._mic = np.zeros(0, np.float32)
        self._ref = np.zeros(0, np.float32)

    def state(self) -> dict[str, np.ndarray]:
        return self._daf.state()

    def restore(self, saved: dict[str, np.ndarray]) -> None:
        self._daf.restore(saved)
        self._gate.reset()

    def process(self, mic: np.ndarray, ref: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        self._mic = np.concatenate([self._mic, np.asarray(mic, np.float32)])
        self._ref = np.concatenate([self._ref, np.asarray(ref, np.float32)])
        ready = min(self._mic.size, self._ref.size) // CHUNK * CHUNK
        cleaned = np.empty(ready, np.float32)
        gate = np.empty(ready, np.float32)
        for start in range(0, ready, CHUNK):
            end = start + CHUNK
            mic_chunk, ref_chunk = self._mic[start:end], self._ref[start:end]
            cleaned[start:end] = self._daf.process(mic_chunk, ref_chunk)
            gate[start:end] = self._gate(mic_chunk, ref_chunk)
        self._mic, self._ref = self._mic[ready:], self._ref[ready:]
        return cleaned, gate


__all__ = ["CHUNK", "DAF", "Echo", "GateModel", "M", "load_weights"]
