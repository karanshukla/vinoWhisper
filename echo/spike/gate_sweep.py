"""Replace the v1.2 gate with an echo-aware rule on 2.7K's own signals: a 512-sample
chunk is fed to Silero only if e is within THR dB of yhat (or yhat is quiet)."""
import json, os, sys
from pathlib import Path
import numpy as np
B = Path(sys.argv[1]); sys.argv = [sys.argv[0], str(B)]
sys.path.insert(0, os.path.dirname(__file__))
import laptop as L  # noqa
import soundfile as sf
from vinowhisper import vad, ovfetch  # noqa

SETS = [("sim cold", "clips", "laptop", "cold"), ("real cold", "clips_real", "laptop_clips_real", "cold"),
        ("real session", "clips_real", "laptop_clips_real", "session")]
THRS = [None, -12, -9, -6, -3, 0, 3]

def mask_decide(e, y, thr, hang):
    n = len(e) // 512
    de = 10 * np.log10(np.mean(e[: n * 512].reshape(n, 512).astype(np.float64) ** 2, 1) + 1e-12)
    dy = 10 * np.log10(np.mean(y[: n * 512].reshape(n, 512).astype(np.float64) ** 2, 1) + 1e-12)
    keep = (de - dy > thr) | (dy < -50)
    if hang:  # once you talk, hold the gate open a few chunks
        k = keep.copy()
        for i in range(n):
            if keep[i]:
                k[i:i + hang] = True
        keep = k
    out = e.copy(); out[: n * 512] *= np.repeat(keep, 512); out[n * 512:] = 0
    return out

import jiwer, openvino_genai as ov_genai
ovfetch.preload()
pipe = ov_genai.WhisperPipeline(os.path.expanduser("~/.local/share/vinowhisper/models/whisper-small.en-ov"), device="NPU", STATIC_PIPELINE=True)
cfg = pipe.get_generation_config(); cfg.max_new_tokens = 440
vad_path = vad.model_path(directory=B / "models/silero")
for label, clips, out, mode in SETS:
    metas = [m for m in json.load(open(B / clips / "index.json")) if m["kind"] != "ne"]
    variants = [("v1.2 gate", None, 0)] + [(f"thr {t}", t, 0) for t in THRS if t is not None] + [("thr -6 hang 8", -6, 8), ("thr -3 hang 8", -3, 8)]
    for name, thr, hang in variants:
        dt, fe = [], []
        for m in metas:
            mic, _ = sf.read(B / clips / m["name"] / "mic.wav", dtype="float32")
            e = np.load(B / out / "wav" / f"lvqe1.4aec2.7k@{mode}" / (m["name"] + ".npy"))
            if thr is None:
                decide = np.load(B / out / "wav" / f"lvqe1.2@{mode}" / (m["name"] + ".npy"))
            else:
                decide = mask_decide(e, mic - e, thr, hang)
            fed = L.front_end(e, vad_path, decide)
            hyp = "" if fed is None else L.norm(str(pipe.generate(fed.tolist(), cfg)))
            if m["kind"] == "fe":
                fe.append(len(hyp.split()) / (m["seconds"] / 60))
            else:
                t = L.norm(m["near_text"]); o = jiwer.process_words(t, hyp or "<empty>")
                dt.append((o.wer * len(t.split()), o.deletions, len(t.split())))
        n = sum(x[2] for x in dt)
        print(f"{label:13s} {name:14s} DT WER {sum(x[0] for x in dt)/n:6.1%}  lost {sum(x[1] for x in dt)/n:5.1%}  leak/min {np.mean(fe):6.1f}", flush=True)
