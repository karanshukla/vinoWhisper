"""NPU-backed Whisper transcription via OpenVINO GenAI."""

import queue
import threading
from collections.abc import Iterator
from pathlib import Path

import numpy as np

import openvino_genai as ov_genai

from . import config

_SENTINEL = object()


class WhisperTranscriber:
    """Loads once, transcribes many times. NPU model load is the expensive part."""

    def __init__(self, model_dir: Path = config.MODEL_DIR, device: str = "NPU") -> None:
        self.model_dir = model_dir
        self.device = device
        self._pipeline: ov_genai.WhisperPipeline | None = None
        # WhisperPipeline is not documented as thread-safe and the NPU static
        # pipeline holds one set of compiled request objects, so serialize.
        # The server runs threaded so /health stays answerable during a decode.
        self._lock = threading.Lock()

    def load(self) -> None:
        # STATIC_PIPELINE=True is required for NPU (confirmed 2026-08-03
        # against openvino_genai nightly 2026.4.0.0.dev — see pyproject.toml
        # for why the nightly is needed). The model must be exported with
        # --disable-stateful (scripts/convert_model.sh does) or pipeline
        # construction fails on a self_attn_nodes assertion.
        if not self.model_dir.is_dir():
            raise FileNotFoundError(
                f"model not found at {self.model_dir} — run scripts/convert_model.sh"
            )

        kwargs = {"STATIC_PIPELINE": True} if self.device == "NPU" else {}
        self._pipeline = ov_genai.WhisperPipeline(
            str(self.model_dir), device=self.device, **kwargs
        )

        # Two generation-config levers that look like they'd help here and
        # don't, both confirmed dead 2026-08-03:
        #
        # - no_repeat_ngram_size, for the runaway single-token repeats greedy
        #   decoding falls into on ambiguous audio: a no-op for Whisper.
        #   pipeline_static.cpp / whisper.cpp / logit_processor.cpp(.hpp) have
        #   zero references to "ngram" or "repeat" — the field exists on
        #   WhisperGenerationConfig but Whisper's decode loop never reads it.
        #   Handled client-side instead, in stitch.collapse_repeats.
        # - initial_prompt, for priming decoding with prior transcript context
        #   to reduce cross-window wording drift: hard-blocked on this device.
        #   `RuntimeError: 'initial_prompt' parameter is not supported on NPU
        #   device` (pipeline_static.cpp:1147).

    def transcribe_stream(self, samples: np.ndarray) -> Iterator[str]:
        """Yield text pieces as they decode.

        `samples` must be float32, 16kHz, mono, and under 30s — the streamer
        callback only supports short-form audio.
        """
        if self._pipeline is None:
            raise RuntimeError("call load() before transcribe_stream()")

        pieces: queue.Queue = queue.Queue()

        def streamer(text_piece: str) -> bool:
            pieces.put(text_piece)
            return False  # keep generating

        def run() -> None:
            try:
                with self._lock:
                    self._pipeline.generate(samples, streamer=streamer)
            except Exception as exc:  # noqa: BLE001 — re-raised in the consumer below
                pieces.put(exc)
            finally:
                pieces.put(_SENTINEL)

        thread = threading.Thread(target=run, name="whisper-generate", daemon=True)
        thread.start()

        try:
            while True:
                piece = pieces.get()
                if piece is _SENTINEL:
                    break
                if isinstance(piece, Exception):
                    raise piece
                yield piece
        finally:
            # Also runs when the consumer abandons the generator (client
            # disconnect, GeneratorExit). Bounded: the decode is short-form and
            # the streamer never blocks, since the queue is unbounded.
            thread.join(timeout=config.REQUEST_TIMEOUT_S)
