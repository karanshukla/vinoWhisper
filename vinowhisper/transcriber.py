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

    def __init__(self, model_dir: Path = config.MODEL_DIR, device: str = "NPU"):
        self.model_dir = model_dir
        self.device = device
        self._pipeline: ov_genai.WhisperPipeline | None = None

    def load(self) -> None:
        # STATIC_PIPELINE=True is required for NPU (confirmed 2026-08-03
        # against openvino_genai nightly 2026.4.0.0.dev — see pyproject.toml
        # for why the nightly build is needed). Model must be exported with
        # --disable-stateful (scripts/convert_model.sh does this) or pipeline
        # construction fails with an self_attn_nodes assertion.
        kwargs = {"STATIC_PIPELINE": True} if self.device == "NPU" else {}
        self._pipeline = ov_genai.WhisperPipeline(str(self.model_dir), device=self.device, **kwargs)

        # Greedy decoding (num_beams=1, do_sample=False, the defaults) has no
        # exploration to escape a repetition loop once it enters one —
        # confirmed in real testing 2026-08-03 (runaway single-token repeat,
        # hundreds of tokens long, on ambiguous/quiet audio). Tried
        # gen_config.no_repeat_ngram_size, confirmed it's a no-op for Whisper:
        # checked pipeline_static.cpp/whisper.cpp/logit_processor.cpp(.hpp)
        # directly, zero references to "ngram"/"repeat" anywhere — the field
        # exists on WhisperGenerationConfig but Whisper's decode loop never
        # reads it (presumably wired up for the general LLM pipeline only).
        # Repetition cleanup happens client-side instead (caption.py's
        # _collapse_repeats).
        #
        # Tried initial_prompt (priming decoding with prior-transcript
        # context, to reduce cross-window wording drift on a sliding-window
        # caller) — confirmed 2026-08-03 it's hard-blocked on NPU:
        # `RuntimeError: 'initial_prompt' parameter is not supported on NPU
        # device` (pipeline_static.cpp:1147). Not available on this backend.

    def transcribe_stream(self, samples: np.ndarray) -> Iterator[str]:
        """Yields text pieces as they decode. `samples` must be float32,
        16kHz, mono, under 30s (streamer callback only supports short-form
        audio).
        """
        if self._pipeline is None:
            raise RuntimeError("call load() before transcribe_stream()")

        chunks: queue.Queue = queue.Queue()

        def streamer(text_piece: str) -> bool:
            chunks.put(text_piece)
            return False  # continue generation

        def run() -> None:
            try:
                self._pipeline.generate(samples, streamer=streamer)
            except Exception as exc:  # noqa: BLE001 — forwarded to the generator's caller, not swallowed
                chunks.put(exc)
            finally:
                chunks.put(_SENTINEL)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        while True:
            piece = chunks.get()
            if piece is _SENTINEL:
                break
            if isinstance(piece, Exception):
                thread.join()
                raise piece
            yield piece

        thread.join()
