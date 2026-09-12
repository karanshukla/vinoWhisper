import threading

import numpy as np

BYTES_PER_SAMPLE = 4

_EMPTY = np.zeros(0, dtype=np.float32)


class RingBuffer:
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")
        self._buf = np.zeros(capacity, dtype=np.float32)
        self._capacity = capacity
        self._written = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def total_written(self) -> int:
        with self._lock:
            return self._written

    def write(self, samples: np.ndarray) -> None:
        count = samples.size
        if count == 0:
            return

        dropped = max(0, count - self._capacity)
        if dropped:
            samples = samples[-self._capacity :]

        with self._lock:
            start = (self._written + dropped) % self._capacity
            end = start + samples.size
            if end <= self._capacity:
                self._buf[start:end] = samples
            else:
                split = self._capacity - start
                self._buf[start:] = samples[:split]
                self._buf[: samples.size - split] = samples[split:]
            self._written += count

    def read_last(self, count: int) -> np.ndarray:
        if count <= 0:
            return _EMPTY
        with self._lock:
            available = min(count, self._written, self._capacity)
            if available == 0:
                return _EMPTY
            start = (self._written - available) % self._capacity
            end = start + available
            if end <= self._capacity:
                return self._buf[start:end].copy()
            split = self._capacity - start
            return np.concatenate([self._buf[start:], self._buf[: available - split]])


def rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    # float64: squaring in float32 loses precision at these levels.
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def normalize(samples: np.ndarray, target_rms: float, max_gain: float) -> tuple[np.ndarray, float]:
    level = rms(samples)
    if level <= 0.0:
        return samples, 1.0

    gain = min(target_rms / level, max_gain)
    if gain <= 1.0:
        return samples, 1.0

    return np.clip(samples * gain, -1.0, 1.0, dtype=np.float32), gain
