import queue
import threading
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from . import config, devices, failures

_SENTINEL = object()


class WhisperTranscriber:
    def __init__(
        self,
        model_dir: Path | None = None,
        device: str | None = None,
    ) -> None:
        self._requested_device = device
        self._model_dir_override = model_dir
        self.selection: devices.Selection | None = None
        self.failure: failures.Failure | None = None
        self.model_dir: Path | None = None
        self._pipeline: object | None = None
        self._lock = threading.Lock()

    @property
    def device(self) -> str:
        return self.selection.device.name if self.selection else (self._requested_device or "?")

    def describe(self) -> dict:
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
        self.selection = devices.select(
            self._requested_device or config.DEFAULT_DEVICE, failed=failures.load()
        )
        return self.selection

    def load(self) -> None:
        import openvino_genai as ov_genai

        selection = self.selection or self.select_device()
        kind = selection.kind
        model_dir = self._model_dir_override or config.model_dir(kind)
        self.model_dir = model_dir
        self._check_export(kind, model_dir)

        # STATIC_PIPELINE selects the NPU code path; set anywhere else it breaks.
        kwargs = {"STATIC_PIPELINE": True} if kind == "NPU" else {}
        try:
            self._pipeline = ov_genai.WhisperPipeline(
                str(self.model_dir), device=selection.device.name, **kwargs
            )
        except Exception as exc:
            # Past _check_export, so this is the device, not the model (issue #7).
            self.failure = failures.record(selection.device.name, str(exc))
            raise
        failures.forget(selection.device.name)

    def _check_export(self, kind: str, model_dir: Path) -> None:
        variant = "npu" if kind == "NPU" else "stateful"
        if not model_dir.is_dir():
            raise FileNotFoundError(
                f"no {variant} model export at {model_dir}\n  run: {config.export_command(variant)}"
            )

        has_with_past = any(model_dir.glob("*decoder_with_past*.xml"))
        if kind == "NPU" and not has_with_past:
            raise RuntimeError(
                f"the export at {model_dir} has no decoder_with_past submodel, so it "
                "was not exported with --disable-stateful and the NPU static pipeline "
                "cannot build it.\n"
                f"  run: {config.export_command('npu')}"
            )
        if kind != "NPU" and has_with_past:
            raise RuntimeError(
                f"the export at {model_dir} is the --disable-stateful (NPU) export, "
                f"which fails on {kind} with a beam_idx port error.\n"
                f"  run: {config.export_command('stateful')}"
            )

    def transcribe_stream(self, samples: np.ndarray) -> Iterator[str]:
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
            thread.join(timeout=config.REQUEST_TIMEOUT_S)
