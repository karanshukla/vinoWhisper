"""Score the live takes: raw mic vs LocalVQE (cold per take, and one session context
over the takes in order), through SpeechGate + normalize + Whisper on the NPU."""
import json, os, sys
from pathlib import Path
import numpy as np
sys.argv = [sys.argv[0], sys.argv[1]]
sys.path.insert(0, os.path.dirname(__file__))
import laptop as L  # noqa
from vinowhisper import vad, ovfetch  # noqa

B = Path(sys.argv[1]); D = B / "live"; FS = 16000
P = json.load(open(D / "passages.json"))
order = ["A_quiet", "A_echo", "B_quiet", "B_echo"]
lanes = {}
for t in order:
    mic, mon = np.load(D / f"{t}_mic.npy"), np.load(D / f"{t}_mon.npy")
    k = int(round(float((D / f"{t}_skew.txt").read_text()) * FS))
    mon = mon[k:] if k > 0 else mon; mic = mic if k > 0 else mic[-k:]
    n = min(len(mic), len(mon)); lanes[t] = (mic[:n].copy(), mon[:n].copy())
    rm = lambda x: 20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12)
    print(f"{t}: {n/FS:.1f}s mic {rm(mic):.1f} dBFS mon {rm(mon):.1f} dBFS", flush=True)
outs = {}
for t in order:
    mic, ref = lanes[t]
    for m in L.MODELS:
        outs[(t, m, "cold")] = L.stream(m, mic, ref, [], reset=True)
        outs[(t, m, "session")] = L.stream(m, mic, ref, [], reset=False)
import jiwer, openvino_genai as ov_genai
ovfetch.preload()
pipe = ov_genai.WhisperPipeline(os.path.expanduser("~/.local/share/vinowhisper/models/whisper-small.en-ov"), device="NPU", STATIC_PIPELINE=True)
cfg = pipe.get_generation_config(); cfg.max_new_tokens = 440
vad_path = vad.model_path(directory=B / "models/silero")
rows = []
for t in order:
    truth = L.norm(P[t[0]]); n = len(truth.split())
    variants = {"none": (lanes[t][0], None)}
    for mode in ("cold", "session"):
        variants[f"2.7k@{mode}"] = (outs[(t, "lvqe1.4aec2.7k", mode)], None)
        variants[f"v1.2@{mode}"] = (outs[(t, "lvqe1.2", mode)], None)
        variants[f"hybrid@{mode}"] = (outs[(t, "lvqe1.4aec2.7k", mode)], outs[(t, "lvqe1.2", mode)])
    for name, (x, decide) in variants.items():
        fed = L.front_end(np.asarray(x, np.float32), vad_path, decide)
        hyp = "" if fed is None else L.norm(str(pipe.generate(fed.tolist(), cfg)))
        o = jiwer.process_words(truth, hyp or "<empty>")
        rows.append(dict(take=t, variant=name, wer=o.wer, dele=o.deletions / n, ins=o.insertions / n, sub=o.substitutions / n, hyp=hyp))
        print(f"{t:8s} {name:15s} WER {o.wer:6.1%} del {o.deletions/n:5.1%} ins {o.insertions/n:5.1%} | {hyp}", flush=True)
json.dump(rows, open(D / "scores.json", "w"), indent=1)
