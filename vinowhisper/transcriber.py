"""NPU-backed Whisper transcription via OpenVINO GenAI."""

from pathlib import Path

import numpy as np
import openvino_genai as ov_genai
import soundfile as sf

from . import config


class WhisperTranscriber:
    """Loads once, transcribes many times. NPU model load is the expensive part."""

    def __init__(self, model_dir: Path = config.MODEL_DIR, device: str = "NPU"):
        self.model_dir = model_dir
        self.device = device
        self._pipeline: ov_genai.WhisperPipeline | None = None

    def load(self) -> None:
        # NPU requires the static-shape pipeline path. Confirm the exact
        # constructor kwarg against the installed openvino_genai version;
        # this may have moved since it was last checked.
        self._pipeline = ov_genai.WhisperPipeline(str(self.model_dir), device=self.device)

    def transcribe(self, wav_path: Path) -> str:
        if self._pipeline is None:
            raise RuntimeError("call load() before transcribe()")
        samples, sample_rate = sf.read(str(wav_path), dtype="float32")
        if sample_rate != config.SAMPLE_RATE_HZ:
            raise ValueError(f"expected {config.SAMPLE_RATE_HZ}Hz, got {sample_rate}Hz")
        if samples.ndim > 1:
            samples = np.mean(samples, axis=1)
        result = self._pipeline.generate(samples)
        return str(result)
