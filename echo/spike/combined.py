"""Full Python path per 512-sample chunk: numpy 2.7K (what Whisper hears) + OpenVINO v1.2 (gate signal)."""
import json, os, sys, time
from pathlib import Path
import numpy as np
B = Path(sys.argv[1]); secs = float(sys.argv[2])
sys.path.insert(0, os.path.dirname(__file__))
import soundfile as sf
t0 = time.perf_counter()
import daf
from v12_stream_ov import Runner
t_imp = time.perf_counter()
f = daf.DAF(daf.load_weights(B / "models/localvqe-v1.4-aec-2.7K-f32.gguf"))
t_daf = time.perf_counter()
r = Runner(B / "ov_v12s/v12s_T2.xml")
t_ov = time.perf_counter()
clips = sorted((B / "clips_real").glob("dt_*"))
mic = np.concatenate([sf.read(c / "mic.wav", dtype="float32")[0] for c in clips])
ref = np.concatenate([sf.read(c / "ref.wav", dtype="float32")[0] for c in clips])
a, b, both = [], [], []; late = 0; i = 0
t_next = time.perf_counter(); end = t_next + secs
while time.perf_counter() < end:
    j = (i * 512) % (len(mic) - 512); m, rr = mic[j:j+512], ref[j:j+512]
    s0 = time.perf_counter(); e = f.process(m, rr); s1 = time.perf_counter(); g = 2.0 * r(m, rr); s2 = time.perf_counter()
    a.append((s1 - s0) * 1e3); b.append((s2 - s1) * 1e3); both.append((s2 - s0) * 1e3)
    i += 1; t_next += 0.032; d = t_next - time.perf_counter()
    if d > 0: time.sleep(d)
    else: late += 1
st = lambda v: dict(mean=round(float(np.mean(v)), 2), p99=round(float(np.percentile(v, 99)), 2), max=round(float(np.max(v)), 2))
print(json.dumps(dict(chunks=i, late=late, budget_ms=32, k27_numpy=st(a), v12_ov=st(b), both=st(both),
                      startup_s=dict(imports=round(t_imp - t0, 2), daf_load=round(t_daf - t_imp, 2), ov_compile=round(t_ov - t_daf, 2)))))
