import queue
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

from . import config, devices, failures, ovfetch

_SENTINEL = object()


class WhisperTranscriber:
    def __init__(
        self,
        model_dir: Path | None = None,
        device: str | None = None,
        language: str = config.DEFAULT_LANGUAGE,
        task: str = config.DEFAULT_TASK,
    ) -> None:
        config.check_language(language, task)
        self.language = language
        self.task = task
        self.multilingual = config.is_multilingual(language, task)
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
            return {
                "device": self._requested_device or config.DEFAULT_DEVICE,
                "loaded": False,
                "language": self.language,
                "task": self.task,
            }
        return {
            "device": self.selection.device.name,
            "device_kind": self.selection.kind,
            "device_full_name": self.selection.device.full_name,
            "degraded": self.selection.degraded,
            "warnings": list(self.selection.warnings),
            "model_dir": str(self.model_dir) if self.model_dir else "",
            "language": self.language,
            "task": self.task,
            "loaded": self._pipeline is not None,
        }

    def select_device(self) -> devices.Selection:
        self.selection = devices.select(
            self._requested_device or config.DEFAULT_DEVICE, failed=failures.load()
        )
        return self.selection

    def load(self, *, record: bool = True) -> None:
        """Build the pipeline on the selected device. record=False leaves a
        failure out of failed-devices.json (a reload after a reset, which says
        nothing about whether the device works)."""
        import openvino_genai as ov_genai

        selection = self.selection or self.select_device()
        kind = selection.kind
        model_dir = self._model_dir_override or config.model_dir(kind, self.multilingual)
        self.model_dir = model_dir
        self._check_export(kind, model_dir)

        # The compiler or driver from `ovfetch npu install`, before OpenVINO
        # opens the NPU. Only the NPU needs it; on another device a bad prefix
        # should not stop the server.
        try:
            ovfetch.preload()
        except ovfetch.OvfetchError:
            if kind == "NPU":
                raise
        # STATIC_PIPELINE selects the NPU code path; set anywhere else it breaks.
        kwargs = {"STATIC_PIPELINE": True} if kind == "NPU" else {}
        try:
            self._pipeline = ov_genai.WhisperPipeline(
                str(self.model_dir), device=selection.device.name, **kwargs
            )
        except Exception as exc:
            # Past _check_export, so this is the device, not the model (issue #7).
            if record:
                self.failure = failures.record(selection.device.name, str(exc))
            raise
        failures.forget(selection.device.name)

    def _check_export(self, kind: str, model_dir: Path) -> None:
        variant = "npu" if kind == "NPU" else "stateful"
        if not model_dir.is_dir():
            raise FileNotFoundError(
                f"no {variant} model export at {model_dir}\n"
                f"  run: {config.export_command(variant, self.multilingual)}"
            )

        has_with_past = any(model_dir.glob("*decoder_with_past*.xml"))
        if kind == "NPU" and not has_with_past:
            raise RuntimeError(
                f"the export at {model_dir} has no decoder_with_past submodel, so it "
                "was not exported with --disable-stateful and the NPU static pipeline "
                "cannot build it.\n"
                f"  run: {config.export_command('npu', self.multilingual)}"
            )
        if kind != "NPU" and has_with_past:
            raise RuntimeError(
                f"the export at {model_dir} is the --disable-stateful (NPU) export, "
                f"which fails on {kind} with a beam_idx port error.\n"
                f"  run: {config.export_command('stateful', self.multilingual)}"
            )

    def generate_options(self) -> dict[str, str]:
        # The English-only model has no language or task tokens, and refuses them.
        if not self.multilingual:
            return {}
        options = {"task": self.task}
        if self.language != config.AUTO_LANGUAGE:
            # Fixed per server: auto-detect per 12s window can flip language mid-caption.
            options["language"] = f"<|{self.language}|>"
        return options

    def transcribe_stream(self, samples: np.ndarray) -> Iterator[str]:
        if self.selection is None:
            raise RuntimeError("call load() before transcribe_stream()")

        pieces: queue.Queue = queue.Queue()
        streamed = False

        def streamer(text_piece: str) -> bool:
            nonlocal streamed
            streamed = True
            pieces.put(text_piece)
            return False  # keep generating

        def run() -> None:
            try:
                with self._lock:
                    lost = ""
                    try:
                        self._generate(samples, streamer)
                    except RuntimeError as exc:
                        if not failures.is_device_lost(exc):
                            raise
                        self._pipeline = None
                        print(
                            f"[vinowhisper-server] {self.device} was reset, reloading the model",
                            file=sys.stderr,
                            flush=True,
                        )
                        # Retrying after text went out would print it twice.
                        if streamed:
                            raise
                        lost = str(exc)
                    # Outside the except block: its traceback holds _generate's
                    # frame, and with it the dead pipeline, which would stay in
                    # memory (and on the device) through the reload.
                    if lost:
                        self._reload_after_reset(lost)
                        self._generate(samples, streamer)
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

    def _reload_after_reset(self, error: str) -> None:
        """Load again after the device reset under us (another program's fault
        resets it for everyone). A reset is not the device failing, so nothing is
        recorded. If it doesn't come back and the device was picked
        automatically, fall back to the next one for the rest of this run;
        the next start picks again. A device asked for by name is not swapped."""
        try:
            self.load(record=False)
            return
        except Exception:
            requested = self._requested_device or config.DEFAULT_DEVICE
            if self.selection is None or requested.lower() != config.AUTO_DEVICE:
                raise
        name = self.selection.device.name
        print(
            f"[vinowhisper-server] {name} did not come back after the reset; "
            "falling back for this run",
            file=sys.stderr,
            flush=True,
        )
        lost = failures.Failure(device=name, failed_at="this run", error=failures.summary(error))
        self.selection = devices.select(requested, failed={**failures.load(), name: lost})
        self.load()

    def _generate(self, samples: np.ndarray, streamer: Callable[[str], bool]) -> None:
        if self._pipeline is None:
            self.load()
        pipeline = self._pipeline
        if pipeline is None:
            raise RuntimeError(f"no pipeline on {self.device} after load()")
        pipeline.generate(  # type: ignore[attr-defined]
            samples,
            streamer=streamer,
            max_new_tokens=config.max_new_tokens(
                samples.size / config.SAMPLE_RATE_HZ, self.multilingual
            ),
            **self.generate_options(),
        )
