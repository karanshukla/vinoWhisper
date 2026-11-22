from collections import deque

import numpy as np

from . import config

CHUNK = 512
WINDOW_S = 10.0
REF_ACTIVE_RMS = 0.01
NEAR_GATE_RMS = 0.003
ECHO_HEARD_RMS = 0.01
MIN_FAR_END_S = 5.0
LOUD_SHARE = 0.5
REPEAT_AFTER_S = 300.0

HINT = "Their voice is coming through your speakers into the mic. Headphones will caption you more cleanly."


def _rms(chunk: np.ndarray) -> float:
    return float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2))) if chunk.size else 0.0


class EchoMeter:
    def __init__(self) -> None:
        self._window: deque[tuple[bool, bool]] = deque(
            maxlen=int(WINDOW_S * config.SAMPLE_RATE_HZ / CHUNK)
        )
        self._hinted_at: float | None = None

    def push(self, mic: np.ndarray, ref: np.ndarray, gate: np.ndarray) -> None:
        for start in range(0, mic.size - CHUNK + 1, CHUNK):
            end = start + CHUNK
            far_end = _rms(ref[start:end]) >= REF_ACTIVE_RMS
            only_them = far_end and _rms(gate[start:end]) < NEAR_GATE_RMS
            self._window.append((far_end, only_them and _rms(mic[start:end]) >= ECHO_HEARD_RMS))

    def loud(self) -> bool:
        far_end = sum(1 for active, _ in self._window if active)
        if far_end * CHUNK / config.SAMPLE_RATE_HZ < MIN_FAR_END_S:
            return False
        return sum(1 for _, heard in self._window if heard) >= LOUD_SHARE * far_end

    def hint(self, now_s: float) -> str | None:
        if not self.loud():
            return None
        if self._hinted_at is not None and now_s - self._hinted_at < REPEAT_AFTER_S:
            return None
        self._hinted_at = now_s
        return HINT
