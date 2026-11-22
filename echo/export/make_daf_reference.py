"""Regenerate tests/fixtures/echo_call.npz: two seconds of a LibriSpeech-based call (CC BY 4.0,
speakers' voices from LibriSpeech test-clean) and LocalVQE's own output for it.
python echo/export/make_daf_reference.py MIC.wav REF.wav PATH/TO/daf_cli MODEL.gguf
daf_cli is built by `cmake --build ggml/build --target daf_cli` in a LocalVQE checkout. Run from the
repo root; needs soundfile."""

import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

START_S, SECONDS, RATE = 2, 2, 16000


def _int16(path: str) -> np.ndarray:
    audio, rate = sf.read(path, dtype="float32")
    assert rate == RATE
    clip = audio[START_S * RATE : (START_S + SECONDS) * RATE]
    return np.round(clip * 32767).astype(np.int16)


def main(mic_wav: str, ref_wav: str, daf_cli: str, gguf: str) -> None:
    mic, ref = _int16(mic_wav), _int16(ref_wav)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        np.save(work / "mic.npy", mic.astype(np.float32) / 32767)
        np.save(work / "ref.npy", ref.astype(np.float32) / 32767)
        subprocess.run(
            [daf_cli, gguf, work / "mic.npy", work / "ref.npy", work / "e.npy", "--no-prealign"],
            check=True,
        )
        out = np.load(work / "e.npy")
    np.savez_compressed("tests/fixtures/echo_call.npz", mic=mic, ref=ref, daf=out)


if __name__ == "__main__":
    main(*sys.argv[1:5])
