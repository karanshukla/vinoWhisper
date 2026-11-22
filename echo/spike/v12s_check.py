import json, os, sys, time
from pathlib import Path
import numpy as np
B = Path(sys.argv[1]); what = sys.argv[2]; sys.argv = [sys.argv[0], str(B)]
sys.path.insert(0, os.path.dirname(__file__))
import soundfile as sf
from v12_stream_ov import Runner, HOP

if what == "decode":
    import laptop as L
    import jiwer, openvino_genai as ov_genai
    from vinowhisper import vad, ovfetch
    ovfetch.preload()
    pipe = ov_genai.WhisperPipeline(os.path.expanduser("~/.local/share/vinowhisper/models/whisper-small.en-ov"), device="NPU", STATIC_PIPELINE=True)
    cfg = pipe.get_generation_config(); cfg.max_new_tokens = 440
    vad_path = vad.model_path(directory=B / "models/silero")
    r = Runner(B / "ov_v12s/v12s_T1.xml")
    for label, clips, out, mode in [("sim cold", "clips", "laptop", "cold"), ("real session", "clips_real", "laptop_clips_real", "session")]:
        metas = json.load(open(B / clips / "index.json")); r.reset()
        dt, fe, scale = [], [], []
        for m in metas:
            mic, _ = sf.read(B / clips / m["name"] / "mic.wav", dtype="float32"); ref, _ = sf.read(B / clips / m["name"] / "ref.wav", dtype="float32")
            if mode == "cold":
                r.reset()
            n = len(mic) // 256 * 256
            g = 2.0 * np.concatenate([r(mic[s:s+256], ref[s:s+256]) for s in range(0, n, 256)])
            if m["kind"] == "ne":
                continue
            gg = np.load(B / out / "wav" / f"lvqe1.2@{mode}" / (m["name"] + ".npy"))[:n]
            scale.append(np.sqrt(np.mean(gg**2) / (np.mean(g**2) + 1e-20)))
            e = np.load(B / out / "wav" / f"lvqe1.4aec2.7k@{mode}" / (m["name"] + ".npy"))
            decide = np.zeros_like(e); decide[:n] = g
            fed = L.front_end(e, vad_path, decide)
            hyp = "" if fed is None else L.norm(str(pipe.generate(fed.tolist(), cfg)))
            if m["kind"] == "fe":
                fe.append(len(hyp.split()) / (m["seconds"] / 60))
            else:
                t = L.norm(m["near_text"]); o = jiwer.process_words(t, hyp or "<empty>")
                dt.append((o.wer * len(t.split()), o.deletions, len(t.split())))
        nn = sum(x[2] for x in dt)
        print(f"{label:13s} OV v1.2 gate  DT WER {sum(x[0] for x in dt)/nn:6.1%}  lost {sum(x[1] for x in dt)/nn:5.1%}  leak/min {np.mean(fe):6.1f}  ggml/ov level {np.mean(scale):.2f}", flush=True)
else:  # paced timing: hops T, seconds
    T, secs = int(sys.argv[3] if len(sys.argv) > 3 else os.environ["T"]), 45
    T = int(os.environ["T"])
    r = Runner(B / f"ov_v12s/v12s_T{T}.xml")
    clips = sorted((B / "clips_real").glob("dt_*"))
    mic = np.concatenate([sf.read(c / "mic.wav", dtype="float32")[0] for c in clips]); ref = np.concatenate([sf.read(c / "ref.wav", dtype="float32")[0] for c in clips])
    blk = T * HOP; per = blk / 16000; ts = []; late = 0
    t_next = time.perf_counter(); end = t_next + secs; i = 0
    while time.perf_counter() < end:
        j = (i * blk) % (len(mic) - blk)
        t = time.perf_counter(); r(mic[j:j+blk], ref[j:j+blk]); ts.append(time.perf_counter() - t)
        i += 1; t_next += per; d = t_next - time.perf_counter()
        if d > 0: time.sleep(d)
        else: late += 1
    ts = np.array(ts) * 1000
    print(json.dumps(dict(T=T, calls=i, late=late, per_hop_mean=float(ts.mean() / T), call_mean=float(ts.mean()), call_p99=float(np.percentile(ts, 99)), call_max=float(ts.max()), budget_ms=per * 1000)))
