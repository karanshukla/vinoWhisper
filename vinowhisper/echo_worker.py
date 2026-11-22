import contextlib
import hashlib
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from . import config, echo_policy
from .echo_meter import EchoMeter

STATE_DIR = config.ECHO_STATE_DIR
POLL_S = 3.0
_WAIT_S = 0.25


class Engine(Protocol):
    @property
    def locked(self) -> bool: ...

    def reset(self) -> None: ...

    def state(self) -> dict[str, np.ndarray]: ...

    def restore(self, saved: dict[str, np.ndarray]) -> None: ...

    def process(self, mic: np.ndarray, ref: np.ndarray) -> tuple[np.ndarray, np.ndarray]: ...


@dataclass(frozen=True)
class Status:
    mode: str
    active: bool
    reason: str


def state_file(output: echo_policy.Output, source: str, directory: Path = STATE_DIR) -> Path:
    key = "\0".join([*output.key, source]).encode()
    return directory / f"{hashlib.sha256(key).hexdigest()[:24]}.npz"


def save_state(path: Path, saved: dict[str, np.ndarray]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            np.savez(handle, allow_pickle=False, **saved)  # type: ignore[arg-type]
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def load_state(path: Path) -> dict[str, np.ndarray] | None:
    try:
        with np.load(path) as archive:
            return {name: archive[name] for name in archive.files}
    except (OSError, ValueError, KeyError):
        return None


class EchoWorker:
    def __init__(
        self,
        mode: str,
        make_engine: Callable[[], Engine],
        on_clean: Callable[[np.ndarray], None],
        on_gate: Callable[[np.ndarray], None],
        mic_source: str = "mic",
        current_output: Callable[[], echo_policy.Output | None] = echo_policy.current_output,
        state_dir: Path = STATE_DIR,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.meter = EchoMeter()
        self._mode = mode
        self._make_engine = make_engine
        self._on_clean, self._on_gate = on_clean, on_gate
        self._source = mic_source
        self._current_output = current_output
        self._state_dir = state_dir
        self._clock = clock
        self._engine: Engine | None = None
        self._output: echo_policy.Output | None = None
        self._status = Status(mode, False, "starting")
        self._cond = threading.Condition()
        self._mic = np.zeros(0, np.float32)
        self._ref = np.zeros(0, np.float32)
        self._stop = False
        self._thread: threading.Thread | None = None
        self._next_poll_at = 0.0
        self._held_mic = np.zeros(0, np.float32)
        self._held_ref = np.zeros(0, np.float32)
        self._unavailable = ""

    @property
    def status(self) -> Status:
        with self._cond:
            return self._status

    def start(self) -> None:
        self._redecide()
        self._thread = threading.Thread(target=self._run, name="echo-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._save()

    def feed_mic(self, chunk: np.ndarray) -> None:
        with self._cond:
            self._mic = np.concatenate([self._mic, chunk])
            self._cond.notify_all()

    def feed_ref(self, chunk: np.ndarray) -> None:
        with self._cond:
            if self._status.active:
                self._ref = np.concatenate([self._ref, chunk])

    def _run(self) -> None:
        while True:
            with self._cond:
                if not self._stop and not self._mic.size:
                    self._cond.wait(_WAIT_S)
                if self._stop:
                    return
                mic, ref = self._mic, self._ref
                self._mic = np.zeros(0, np.float32)
                self._ref = np.zeros(0, np.float32)
                active = self._status.active
            if self._clock() >= self._next_poll_at:
                self._redecide()
                active = self.status.active
            if mic.size:
                self._step(mic, ref, active)

    def _step(self, mic: np.ndarray, ref: np.ndarray, active: bool) -> None:
        if not active or self._engine is None:
            self._on_clean(mic)
            self._on_gate(mic)
            return
        try:
            cleaned, gate = self._engine.process(mic, ref)
        except Exception as exc:  # noqa: BLE001 - captions go on, uncancelled
            self._set(False, f"stopped: {type(exc).__name__}")
            self._on_clean(mic)
            self._on_gate(mic)
            return
        self._held_mic = np.concatenate([self._held_mic, mic])
        self._held_ref = np.concatenate([self._held_ref, ref])
        done = cleaned.size
        self.meter.push(self._held_mic[:done], self._held_ref[:done], gate)
        self._held_mic, self._held_ref = self._held_mic[done:], self._held_ref[done:]
        if done:
            self._on_clean(cleaned)
            self._on_gate(gate)

    def _redecide(self) -> None:
        self._next_poll_at = self._clock() + POLL_S
        output = self._current_output()
        decision = echo_policy.decide(self._mode, output)
        if self._unavailable:
            self._set(False, self._unavailable)
            return
        moved = output is not None and (self._output is None or output.key != self._output.key)
        if moved:
            self._save()
            self._output = output
        if decision.active and (moved or not self.status.active):
            self._activate(decision)
        elif not decision.active and self.status.active:
            self._save()
            self._set(False, decision.label(self._mode))
        elif decision.active:
            self._set(True, decision.label(self._mode))
        else:
            self._set(False, decision.label(self._mode))

    def _activate(self, decision: echo_policy.Decision) -> None:
        try:
            if self._engine is None:
                self._engine = self._make_engine()
        except Exception as exc:  # noqa: BLE001 - captions go on, uncancelled
            self._unavailable = f"unavailable: {exc}"
            self._set(False, self._unavailable)
            return
        self._engine.reset()
        self._held_mic = np.zeros(0, np.float32)
        self._held_ref = np.zeros(0, np.float32)
        saved = (
            load_state(state_file(self._output, self._source, self._state_dir))
            if self._output
            else None
        )
        if saved is not None:
            with contextlib.suppress(KeyError, ValueError):
                self._engine.restore(saved)
        self._set(True, decision.label(self._mode))

    def _save(self) -> None:
        if self._engine is None or self._output is None or not self.status.active:
            return
        if self._engine.locked:
            with contextlib.suppress(OSError):
                save_state(
                    state_file(self._output, self._source, self._state_dir), self._engine.state()
                )

    def _set(self, active: bool, reason: str) -> None:
        with self._cond:
            self._status = Status(self._mode, active, reason)
