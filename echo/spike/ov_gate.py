import json, os, sys, time
from pathlib import Path
import numpy as np
B = Path(sys.argv[1]); sys.argv = [sys.argv[0], str(B)]
sys.path.insert(0, os.path.dirname(__file__))
import laptop as L  # noqa
import soundfile as sf
import openvino as ov
from vinowhisper import vad, ovfetch  # noqa
FS, BLK = 16000, 8192
core = ov.Core()
models = {}
for ctx_s in (1.0, 2.0, 4.0):
    total = int((ctx_s + 0.512) * FS)
    models[ctx_s] = (total, core.compile_model(str(B / "ov_v12" / f"v12_{total}.xml"), "CPU", {"INFERENCE_NUM_THREADS": 1}))

def blockwise(ctx_s, mic, ref, times):
    total, cm = models[ctx_s]
    req = cm.create_infer_request()
    out = np.zeros(len(mic), np.float32)
    pad = total - BLK
    m = np.concatenate([np.zeros(pad, np.float32), mic]); r = np.concatenate([np.zeros(pad, np.float32), ref])
    for s in range(0, len(mic), BLK):
        a, b = m[s: s + total], r[s: s + total]
        if len(a) < total:
            a = np.pad(a, (0, total - len(a))); b = np.pad(b, (0, total - len(b)))
        t = time.perf_counter(); req.infer([a[None], b[None]]); times.append(time.perf_counter() - t)
        y = req.get_output_tensor(0).data.reshape(-1)[-BLK:]
        out[s: s + BLK] = y[: len(out) - s]
    return out

import jiwer, openvino_genai as ov_genai
ovfetch.preload()
pipe = ov_genai.WhisperPipeline(os.path.expanduser("~/.local/share/vinowhisper/models/whisper-small.en-ov"), device="NPU", STATIC_PIPELINE=True)
cfg = pipe.get_generation_config(); cfg.max_new_tokens = 440
vad_path = vad.model_path(directory=B / "models/silero")
SETS = [("sim cold", "clips", "laptop", "cold"), ("real session", "clips_real", "laptop_clips_real", "session")]
for label, clips, out, mode in SETS:
    metas = [m for m in json.load(open(B / clips / "index.json")) if m["kind"] != "ne"]
    for variant in ["ggml v1.2", 1.0, 2.0, 4.0]:
        dt, fe, times, corrs = [], [], [], []
        for m in metas:
            d = B / clips / m["name"]
            mic, _ = sf.read(d / "mic.wav", dtype="float32"); ref, _ = sf.read(d / "ref.wav", dtype="float32")
            e = np.load(B / out / "wav" / f"lvqe1.4aec2.7k@{mode}" / (m["name"] + ".npy"))
            g = np.load(B / out / "wav" / f"lvqe1.2@{mode}" / (m["name"] + ".npy"))
            if variant == "ggml v1.2":
                decide = g
            else:
                decide = blockwise(variant, mic, ref, times)
                gs = g[256:]; n = min(len(gs), len(decide))  # ggml is one hop later
                corrs.append(np.corrcoef(decide[: n], gs[: n])[0, 1])
            fed = L.front_end(e, vad_path, decide)
            hyp = "" if fed is None else L.norm(str(pipe.generate(fed.tolist(), cfg)))
            if m["kind"] == "fe":
                fe.append(len(hyp.split()) / (m["seconds"] / 60))
            else:
                t = L.norm(m["near_text"]); o = jiwer.process_words(t, hyp or "<empty>")
                dt.append((o.wer * len(t.split()), o.deletions, len(t.split())))
        n = sum(x[2] for x in dt)
        extra = f"  corr w/ ggml {np.mean(corrs):.3f}  {np.mean(times)*1000:.1f} ms/call ({np.mean(times)/0.512:.3f} RT)" if times else ""
        name = variant if isinstance(variant, str) else f"ov ctx {variant}s"
        print(f"{label:13s} {name:11s} DT WER {sum(x[0] for x in dt)/n:6.1%}  lost {sum(x[1] for x in dt)/n:5.1%}  leak/min {np.mean(fe):6.1f}{extra}", flush=True)
