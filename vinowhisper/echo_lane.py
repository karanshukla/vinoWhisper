from collections.abc import Callable

import numpy as np

from . import audio, config, echo_model, echo_policy
from .echo_worker import EchoWorker, Status
from .recorder import Recorder


def make_engine():
    from vinoecho import Echo
    from vinoecho.v12 import Gate

    try:
        return Echo(Gate(echo_model.model_path()))
    except echo_model.EchoModelError as exc:
        raise RuntimeError(str(exc)) from exc


class EchoRecorder:
    def __init__(
        self,
        mic: Callable[[Callable[[np.ndarray], None]], Recorder],
        mode: str,
        on_gate: Callable[[np.ndarray], None] | None,
        on_clean: Callable[[np.ndarray], None] | None,
        make: Callable[[], object] = make_engine,
    ) -> None:
        self._ring = audio.RingBuffer(int(config.MAX_WINDOW_S * config.SAMPLE_RATE_HZ))

        def clean(chunk: np.ndarray) -> None:
            self._ring.write(chunk)
            if on_clean is not None:
                on_clean(chunk)

        self.worker = EchoWorker(
            mode,
            make,  # type: ignore[arg-type]
            clean,
            on_gate or (lambda chunk: None),
        )
        self._mic = mic(self.worker.feed_mic)

    @property
    def status(self) -> Status:
        return self.worker.status

    def __enter__(self) -> "EchoRecorder":
        self.worker.start()
        self._mic.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._mic.__exit__(*exc_info)
        self.worker.stop()

    def check_alive(self) -> None:
        self._mic.check_alive()

    @property
    def captured_s(self) -> float:
        return self._ring.total_written / config.SAMPLE_RATE_HZ

    def window(self, seconds: float) -> np.ndarray:
        return self._ring.read_last(int(seconds * config.SAMPLE_RATE_HZ))


def initial_status(mode: str) -> Status:
    decision = echo_policy.decide(mode, echo_policy.current_output() if mode == "auto" else None)
    return Status(mode, decision.active, decision.label(mode))
