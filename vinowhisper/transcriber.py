import queue
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

from . import config, devices, failures, ovfetch, stitch, vocabulary, whisper_step

_SENTINEL = object()


class BaseTranscriber:
    model = config.WHISPER_MODEL

    def __init__(
        self,
        model_dir: Path | None = None,
        device: str | None = None,
        language: str = config.DEFAULT_LANGUAGE,
        task: str = config.DEFAULT_TASK,
    ) -> None:
        config.check_language(language, task, self.model)
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
                "model": self.model,
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
            "model": self.model,
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
        selection = self.selection or self.select_device()
        kind = selection.kind
        model_dir = self._model_dir_override or self._default_model_dir(kind)
        self.model_dir = model_dir
        self._check_export(kind, model_dir)
        try:
            self._build(selection, model_dir)
        except Exception as exc:
            # Past _check_export, so this is the device, not the model (issue #7).
            if record:
                self.failure = failures.record(selection.device.name, str(exc))
            raise
        failures.forget(selection.device.name)

    def _default_model_dir(self, kind: str) -> Path:
        raise NotImplementedError

    def _check_export(self, kind: str, model_dir: Path) -> None:
        raise NotImplementedError

    def _build(self, selection: devices.Selection, model_dir: Path) -> None:
        raise NotImplementedError

    def _unload(self) -> None:
        self._pipeline = None

    def _generate(
        self, samples: np.ndarray, streamer: Callable[[str], bool], biased: bool = False
    ) -> str:
        raise NotImplementedError

    def transcribe_stream(
        self,
        samples: np.ndarray,
        biased: bool = False,
        heard: Callable[[str], None] | None = None,
    ) -> Iterator[str]:
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
                    language = ""
                    try:
                        language = self._generate(samples, streamer, biased)
                    except RuntimeError as exc:
                        if not failures.is_device_lost(exc):
                            raise
                        self._unload()
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
                        language = self._generate(samples, streamer, biased)
                if language and heard is not None:
                    heard(language)
            except Exception as exc:  # noqa: BLE001 — re-raised in the consumer below
                pieces.put(exc)
            finally:
                pieces.put(_SENTINEL)

        thread = threading.Thread(target=run, name=f"{self.model}-generate", daemon=True)
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


class WhisperTranscriber(BaseTranscriber):
    model = config.WHISPER_MODEL
    _vocabulary_stamp: tuple[int, int] | None | bool = False
    _hotwords: tuple[str, int] | None = None
    _terms: list[str] = []
    _step: whisper_step.StepWhisper | None = None
    _step_failed = False

    def _default_model_dir(self, kind: str) -> Path:
        return config.model_dir(kind, self.multilingual)

    def _build(self, selection: devices.Selection, model_dir: Path) -> None:
        import openvino_genai as ov_genai

        # The compiler or driver from `ovfetch npu install`, before OpenVINO
        # opens the NPU. Only the NPU needs it; on another device a bad prefix
        # should not stop the server.
        try:
            ovfetch.preload()
        except ovfetch.OvfetchError:
            if selection.kind == "NPU":
                raise
        # STATIC_PIPELINE selects the NPU code path; set anywhere else it breaks.
        kwargs = {"STATIC_PIPELINE": True} if selection.kind == "NPU" else {}
        self._pipeline = ov_genai.WhisperPipeline(
            str(model_dir), device=selection.device.name, **kwargs
        )

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

    def load(self, *, record: bool = True) -> None:
        super().load(record=record)
        self._warm_step()

    def _unload(self) -> None:
        self._pipeline = None
        self._step = None
        # A reload is a new device session: what failed on the old one may load now.
        self._step_failed = False

    def _step_dir(self) -> Path:
        return config.step_model_dir(self.multilingual)

    def _prompts_on_npu(self) -> bool:
        return not self._step_failed and whisper_step.exported(self._step_dir())

    def _unprompted(self) -> str | None:
        kind = self.selection.kind if self.selection else "?"
        step = kind == "NPU" and self._prompts_on_npu()
        return vocabulary.unprompted(kind, self.model, step, self.language)

    # At load, not inside the first prompted dictation (24.8 s cold, docs/models.md).
    def _warm_step(self) -> None:
        if self.selection is None or self.selection.kind != "NPU" or self._unprompted():
            return
        if not vocabulary.read()[0]:
            return
        try:
            self._step_loop()
        except Exception as exc:  # noqa: BLE001 — the pipeline loaded; the first dictation retries
            if not failures.is_device_lost(exc):
                raise

    def _step_loop(self) -> whisper_step.StepWhisper | None:
        if self._step is None and not self._step_failed and self.selection is not None:
            try:
                self._step = whisper_step.StepWhisper(self._step_dir(), self.selection.device.name)
            except Exception as exc:  # noqa: BLE001 — dictation goes on with correction only
                # A reset is not this export failing: transcribe_stream reloads and retries.
                if failures.is_device_lost(exc):
                    raise
                self._step_failed = True
                print(
                    f"[vinowhisper-server] the vocabulary prompt is off on {self.device}: "
                    f"{self._step_dir()} did not load ({failures.summary(str(exc))})",
                    file=sys.stderr,
                    flush=True,
                )
        return self._step

    def hotwords(self) -> tuple[str, int] | None:
        if self._pipeline is None or self._unprompted():
            return None
        stamp = vocabulary.stamp()
        if stamp != self._vocabulary_stamp:
            self._vocabulary_stamp = stamp
            self._terms = vocabulary.load() if stamp else []
            self._hotwords = self._fit(self._terms)
        return self._hotwords

    def _fit(self, terms: list[str]) -> tuple[str, int] | None:
        tokenizer = self._pipeline.get_tokenizer()  # type: ignore[attr-defined, union-attr]
        kept: list[str] = []
        used = 0
        for term in terms:
            text = vocabulary.prompt([*kept, term])
            # The leading space is how OpenVINO GenAI tokenizes it.
            encoded = tokenizer.encode(" " + text, add_special_tokens=False)
            count = int(encoded.input_ids.shape[-1])
            if count > config.VOCABULARY_MAX_TOKENS:
                break
            kept.append(term)
            used = count
        if len(kept) < len(terms):
            print(
                f"[vinowhisper-server] {config.VOCABULARY_FILE}: using the first {len(kept)} "
                f"of {len(terms)} terms ({config.VOCABULARY_MAX_TOKENS} tokens at most)",
                file=sys.stderr,
                flush=True,
            )
        return (vocabulary.prompt(kept), used) if kept else None

    def _generate(
        self, samples: np.ndarray, streamer: Callable[[str], bool], biased: bool = False
    ) -> str:
        if self._pipeline is None:
            self.load()
        pipeline = self._pipeline
        if pipeline is None:
            raise RuntimeError(f"no pipeline on {self.device} after load()")
        options: dict[str, str] = dict(self.generate_options())
        limit = config.max_new_tokens(samples.size / config.SAMPLE_RATE_HZ, self.multilingual)
        hotwords = self.hotwords() if biased else None
        if hotwords is None:
            result = pipeline.generate(samples, streamer=streamer, max_new_tokens=limit, **options)  # type: ignore[attr-defined]
            return self._detected(result)
        heard: list[str] = []

        def collect(piece: str) -> bool:
            heard.append(piece)
            return False

        # The prompt and the answer share the decoder's positions.
        room = config.WHISPER_MAX_TOKENS - hotwords[1] - config.MAX_TOKENS_MARGIN
        step = self._step_loop() if self.selection and self.selection.kind == "NPU" else None
        if step is not None:
            language = self.language if self.multilingual else None
            text = step.transcribe(
                samples, hotwords[0], min(limit, room), language=language, task=self.task
            )
        elif self.selection and self.selection.kind == "NPU":
            pipeline.generate(samples, streamer=streamer, max_new_tokens=limit, **options)  # type: ignore[attr-defined]
            return ""
        else:
            pipeline.generate(  # type: ignore[attr-defined]
                samples,
                streamer=collect,
                max_new_tokens=min(limit, room),
                hotwords=hotwords[0],
                **options,
            )
            text = "".join(heard)
        # On noise the prompt answers with its own terms, or loops on a word it suggests
        # ("Xochitl, Cebu, Cebu, ..." over barking on the NPU, 2026-11-11); a plain decode
        # says whether anything like them was spoken.
        looped = stitch.is_loop(text)
        if looped or vocabulary.only_terms(text, self._terms):
            biased_text, heard[:] = text, []
            pipeline.generate(samples, streamer=collect, max_new_tokens=limit, **options)  # type: ignore[attr-defined]
            if looped or vocabulary.echoes(biased_text, "".join(heard)):
                text = "".join(heard)
        streamer(text)
        return ""

    def _detected(self, result: object) -> str:
        # A forced language comes back as itself, so only auto mode says anything.
        if not self.multilingual or self.language != config.AUTO_LANGUAGE:
            return ""
        return str(getattr(result, "language", "") or "")
