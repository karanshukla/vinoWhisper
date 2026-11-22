"""Per-hop LocalVQE cost while Whisper decodes on the NPU, in separate processes.
usage: contention.py BENCH_DIR hops SECONDS        (paced, one hop per 16 ms, both models per hop)
       contention.py BENCH_DIR decode SECONDS      (loop NPU decodes on a 12 s window)
Prints JSON."""
import json, os, sys, time
from pathlib import Path
import numpy as np
B, what, secs = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
sys.argv = [sys.argv[0], str(B)] + (["--threads", os.environ["LVQE_THREADS"]] if os.environ.get("LVQE_THREADS") else [])
sys.path.insert(0, os.path.dirname(__file__))
import laptop as L  # noqa
import soundfile as sf

clips = sorted((B / "clips_real").glob("dt_*"))
mic = np.concatenate([sf.read(c / "mic.wav", dtype="float32")[0] for c in clips])
ref = np.concatenate([sf.read(c / "ref.wav", dtype="float32")[0] for c in clips])

if what == "hops":
    F = L.F; c2, c27 = L.ctx("lvqe1.2", "s"), L.ctx("lvqe1.4aec2.7k", "s")
    o = np.zeros(256, np.float32); a, b, both, late = [], [], [], 0
    t_next = time.perf_counter(); end = t_next + secs; i = 0
    while time.perf_counter() < end:
        j = (i * 256) % (len(mic) - 256); m = mic[j:j+256]; r = ref[j:j+256]
        t0 = time.perf_counter()
        L.lib.localvqe_process_frame_f32(c2, m.ctypes.data_as(F), r.ctypes.data_as(F), 256, o.ctypes.data_as(F))
        t1 = time.perf_counter()
        L.lib.localvqe_process_frame_f32(c27, m.ctypes.data_as(F), r.ctypes.data_as(F), 256, o.ctypes.data_as(F))
        t2 = time.perf_counter()
        a.append((t1 - t0) * 1e3); b.append((t2 - t1) * 1e3); both.append((t2 - t0) * 1e3)
        i += 1; t_next += 0.016
        d = t_next - time.perf_counter()
        if d > 0:
            time.sleep(d)
        else:
            late += 1
    st = lambda v: dict(mean=float(np.mean(v)), p50=float(np.median(v)), p90=float(np.percentile(v, 90)),
                        p99=float(np.percentile(v, 99)), max=float(np.max(v)))
    print(json.dumps(dict(hops=i, late=late, v12=st(a), k27=st(b), both=st(both))))
else:
    import openvino_genai as ov_genai
    from vinowhisper import ovfetch
    ovfetch.preload()
    pipe = ov_genai.WhisperPipeline(os.path.expanduser("~/.local/share/vinowhisper/models/whisper-small.en-ov"), device="NPU", STATIC_PIPELINE=True)
    cfg = pipe.get_generation_config(); cfg.max_new_tokens = 160
    w = mic[: 12 * 16000]; w = (w / (np.sqrt(np.mean(w**2)) + 1e-9) * 0.05).astype(np.float32).tolist()
    pipe.generate(w, cfg)
    print("ready", file=sys.stderr, flush=True)
    ts = []; end = time.perf_counter() + secs
    while time.perf_counter() < end:
        t = time.perf_counter(); pipe.generate(w, cfg); ts.append(time.perf_counter() - t)
    print(json.dumps(dict(n=len(ts), mean=float(np.mean(ts)), p90=float(np.percentile(ts, 90)), max=float(np.max(ts)))))
