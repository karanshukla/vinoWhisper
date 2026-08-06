"""Buffering and level helpers for the capture side.

Split out of recorder.py so the buffer semantics (fixed capacity, newest-last,
thread-safe, no reallocation) are stated in one place and can be reasoned
about without the subprocess plumbing in the way.
"""

import threading

import numpy as np

BYTES_PER_SAMPLE = 4  # f32

_EMPTY = np.zeros(0, dtype=np.float32)


class RingBuffer:
    """Fixed-capacity float32 buffer holding the most recent samples.

    Preallocated and written in place. The previous implementation did
    `np.concatenate([buf, chunk])[-cap:]` on every 100ms read, which copies the
    entire window (~1.9MB at a 29.5s capacity) twice per chunk — ~38MB/s of
    pointless memcpy competing with the NPU for memory bandwidth.
    """

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")
        self._buf = np.zeros(capacity, dtype=np.float32)
        self._capacity = capacity
        self._written = 0  # total samples ever written, not just retained
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def total_written(self) -> int:
        """Every sample ever accepted, including ones since overwritten.

        Monotonic, so the caption loop can use it to measure how much *new*
        audio has arrived since the last cycle.
        """
        with self._lock:
            return self._written

    def write(self, samples: np.ndarray) -> None:
        count = samples.size
        if count == 0:
            return

        # A chunk larger than the whole buffer can only happen if capacity is
        # misconfigured, but handle it rather than corrupting the write index:
        # keep the newest `capacity` samples and still account for the rest.
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
        """Copy of the newest `count` samples, oldest first.

        Returns fewer than requested if that's all there is; never blocks on
        the writer beyond the lock.
        """
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
    # float64 accumulator: a 29.5s window is ~472k samples, and squaring in
    # float32 loses precision fast at the ~0.002 levels this gets compared to.
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def normalize(samples: np.ndarray, target_rms: float, max_gain: float) -> tuple[np.ndarray, float]:
    """Boost a quiet window toward `target_rms`. Returns (samples, gain).

    Boost only — loud audio is left alone, since attenuating it doesn't help
    Whisper and clipping-by-gain would hurt. The caller has already rejected
    silence, so there's no risk of amplifying pure noise floor by 20x here.
    """
    level = rms(samples)
    if level <= 0.0:
        return samples, 1.0

    gain = min(target_rms / level, max_gain)
    if gain <= 1.0:
        return samples, 1.0

    return np.clip(samples * gain, -1.0, 1.0, dtype=np.float32), gain
