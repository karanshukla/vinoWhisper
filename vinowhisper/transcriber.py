"""Whisper transcription via OpenVINO GenAI, on whatever device is available.

NPU is the target and the only device the latency design assumes. GPU and CPU
exist so a broken driver degrades the tool instead of bricking it — see
devices.py for the fallback policy and config.model_dir() for why each device
class needs its own export.
"""

import queue
import threading
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from . import config, devices

_SENTINEL = object()


class WhisperTranscriber:
    """Loads once, transcribes many times. Model load is the expensive part."""

    def __init__(
        self,
        model_dir: Path | None = None,
        device: str | None = None,
    ) -> None:
        # Both resolved at load() time rather than here: the server constructs
        # this at import, before it has had a chance to enumerate anything.
        self._requested_device = device
        self._model_dir_override = model_dir
        self.selection: devices.Selection | None = None
        self.model_dir: Path | None = None
        self._pipeline: object | None = None
        # WhisperPipeline is not documented as thread-safe and the NPU static
        # pipeline holds one set of compiled request objects, so serialize.
        # The server runs threaded so /health stays answerable during a decode.
        self._lock = threading.Lock()

    @property
    def device(self) -> str:
        return self.selection.device.name if self.selection else (self._requested_device or "?")

    def describe(self) -> dict:
        """What /health reports. Safe to call before load()."""
        if self.selection is None:
            return {"device": self._requested_device or config.DEFAULT_DEVICE, "loaded": False}
        return {
            "device": self.selection.device.name,
            "device_kind": self.selection.kind,
            "device_full_name": self.selection.device.full_name,
            "degraded": self.selection.degraded,
            "warnings": list(self.selection.warnings),
            "model_dir": str(self.model_dir) if self.model_dir else "",
            "loaded": self._pipeline is not None,
        }

    def select_device(self) -> devices.Selection:
        self.selection = devices.select(self._requested_device or config.DEFAULT_DEVICE)
        return self.selection

    def load(self) -> None:
        import openvino_genai as ov_genai

        selection = self.selection or self.select_device()
        kind = selection.kind
        model_dir = self._model_dir_override or config.model_dir(kind)
        self.model_dir = model_dir
        self._check_export(kind, model_dir)

        # STATIC_PIPELINE=True is required for NPU, and must NOT be set
        # anywhere else: the static pipeline is the NPU-specific code path.
        # Confirmed 2026-08-03 against openvino_genai 2026.4.0.0.dev and
        # re-confirmed 2026-08-31 against stable 2026.3.1. Omitting it does not
        # fall back gracefully, it fails in the generic stateful path with
        # "Stateful models without `beam_idx` input are not supported in
        # StatefulToStateless transformation", which reads like a bad export
        # and is not one.
        kwargs = {"STATIC_PIPELINE": True} if kind == "NPU" else {}
        self._pipeline = ov_genai.WhisperPipeline(
            str(self.model_dir), device=selection.device.name, **kwargs
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

    def _check_export(self, kind: str, model_dir: Path) -> None:
        """Fail with the command to run, rather than inside the constructor.

        The mismatch this catches is the single most confusing failure here:
        the NPU export and the CPU/GPU export are both "the model", they live
        next to each other, and using the wrong one dies with an assertion
        about attention-mask nodes or a beam_idx port that names neither the
        device nor the fix.
        """
        variant = "npu" if kind == "NPU" else "stateful"
        if not model_dir.is_dir():
            raise FileNotFoundError(
                f"no {variant} model export at {model_dir}\n"
                f"  run: ./scripts/convert_model.sh --variant {variant}\n"
                f"  or:  vinowhisper-setup"
            )

        has_with_past = any(model_dir.glob("*decoder_with_past*.xml"))
        if kind == "NPU" and not has_with_past:
            raise RuntimeError(
                f"the export at {model_dir} has no decoder_with_past submodel, so it "
                "was not exported with --disable-stateful and the NPU static pipeline "
                "cannot build it.\n"
                "  run: ./scripts/convert_model.sh --variant npu"
            )
        if kind != "NPU" and has_with_past:
            raise RuntimeError(
                f"the export at {model_dir} is the --disable-stateful (NPU) export, "
                f"which fails on {kind} with a beam_idx port error.\n"
                "  run: ./scripts/convert_model.sh --variant stateful"
            )

    def transcribe_stream(self, samples: np.ndarray) -> Iterator[str]:
        """Yield text pieces as they decode.

        `samples` must be float32, 16kHz, mono, and under 30s — the streamer
        callback only supports short-form audio.
        """
        pipeline = self._pipeline
        if pipeline is None:
            raise RuntimeError("call load() before transcribe_stream()")

        pieces: queue.Queue = queue.Queue()

        def streamer(text_piece: str) -> bool:
            pieces.put(text_piece)
            return False  # keep generating

        def run() -> None:
            try:
                with self._lock:
                    # Untyped: openvino_genai ships no stubs, and it is imported
                    # lazily in load() so this module stays importable without it.
                    pipeline.generate(samples, streamer=streamer)  # type: ignore[attr-defined]
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
