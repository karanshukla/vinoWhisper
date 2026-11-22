"""The synthetic call vinoEcho's behaviour tests run: their noise, echoed late at 60%.
Deterministic (tests/pcm.py's LCG). The numeric reference is not this: it is a speech clip
with LocalVQE's own output (echo/export/make_daf_reference.py)."""

import numpy as np

from tests import pcm

SECONDS = 1.5
ECHO_DELAY_S = 0.005
ECHO_GAIN = 0.6


def call(delay_s: float = ECHO_DELAY_S, seconds: float = SECONDS) -> tuple[np.ndarray, np.ndarray]:
    ref = pcm.noise(seconds, amplitude=0.3, seed=7)
    echo = ECHO_GAIN * pcm.delay_by(ref, delay_s)[: ref.size]
    return echo.astype(np.float32), ref
