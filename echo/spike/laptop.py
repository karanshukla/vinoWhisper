"""#216 on the laptop: LocalVQE GGML through its streaming C API (one 256-sample
hop per call, as the capture path would), vinoWhisper's SpeechGate + normalize,
Whisper small.en on the NPU. Same clips and scoring as linuxlab's gated.py.

usage: laptop.py BENCH_DIR [--threads N]
"""
import ctypes, json, os, re, sys, time
from pathlib import Path
import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.expanduser("~/Development/vinoWhisper"))
from vinowhisper import audio, caption, config, vad, ovfetch  # noqa: E402

B = Path(sys.argv[1])
CLIPS = sys.argv[sys.argv.index("--clips") + 1] if "--clips" in sys.argv else "clips"
THREADS = int(sys.argv[sys.argv.index("--threads") + 1]) if "--threads" in sys.argv else 0
LIB = B / "src/LocalVQE/ggml/build/bin/liblocalvqe.so"
MODELS = {"lvqe1.2": B / "models/localvqe-v1.2-1.3M-f32.gguf",
          "lvqe1.4aec2.7k": B / "models/localvqe-v1.4-aec-2.7K-f32.gguf"}
OUT = B / ("laptop" if CLIPS == "clips" else "laptop_" + CLIPS); CACHE = OUT / "wav"
NUM = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
       "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten"}

def norm(t):
    t = re.sub(r"[^a-z0-9' ]", " ", t.lower().replace("-", " "))
    return " ".join(w.strip("'") for w in (NUM.get(w, w) for w in t.split()) if w.strip("'"))

lib = ctypes.CDLL(str(LIB))
lib.localvqe_options_new.restype = ctypes.c_void_p
lib.localvqe_new_with_options.restype = ctypes.c_void_p
lib.localvqe_new_with_options.argtypes = [ctypes.c_void_p]
lib.localvqe_options_set_model_path.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lib.localvqe_options_set_threads.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.localvqe_options_free.argtypes = [ctypes.c_void_p]
lib.localvqe_reset.argtypes = [ctypes.c_void_p]
lib.localvqe_free.argtypes = [ctypes.c_void_p]
F = ctypes.POINTER(ctypes.c_float)
lib.localvqe_process_frame_f32.argtypes = [ctypes.c_void_p, F, F, ctypes.c_int, F]
_ctx = {}

def ctx(name, tag=""):
    key = (name, tag)
    if key not in _ctx:
        o = lib.localvqe_options_new()
        lib.localvqe_options_set_model_path(o, str(MODELS[name]).encode())
        if THREADS:
            lib.localvqe_options_set_threads(o, THREADS)
        c = lib.localvqe_new_with_options(o)
        lib.localvqe_options_free(o)
        assert c, name
        _ctx[key] = c
    return _ctx[key]

lib.localvqe_process_f32.argtypes = [ctypes.c_void_p, F, F, ctypes.c_int, F]
MODES = sys.argv[sys.argv.index("--modes") + 1].split(",") if "--modes" in sys.argv else ["cold", "batch"]

def batch(name, mic, ref):
    c = ctx(name, "batch"); out = np.zeros(len(mic), np.float32)
    rc = lib.localvqe_process_f32(c, np.ascontiguousarray(mic, np.float32).ctypes.data_as(F),
                                  np.ascontiguousarray(ref, np.float32).ctypes.data_as(F), len(mic), out.ctypes.data_as(F))
    assert rc == 0, rc
    return out

def stream(name, mic, ref, hops_ms, reset=True):
    c = ctx(name, "cold" if reset else "session")
    if reset:
        lib.localvqe_reset(c)
    n = len(mic) // 256 * 256
    out = np.zeros(len(mic), np.float32)
    mic = np.ascontiguousarray(mic, np.float32); ref = np.ascontiguousarray(ref, np.float32)
    buf = np.zeros(256, np.float32)
    for i in range(0, n, 256):
        m, r = mic[i:i+256], ref[i:i+256]
        t = time.perf_counter()
        rc = lib.localvqe_process_frame_f32(c, m.ctypes.data_as(F), r.ctypes.data_as(F), 256, buf.ctypes.data_as(F))
        hops_ms.append((time.perf_counter() - t) * 1000)
        assert rc == 0, rc
        out[i:i+256] = buf
    return out

def front_end(samples, vad_path, decide=None):
    decide = samples if decide is None else decide
    gate = caption.SpeechGate(vad.SpeechDetector(vad_path))
    for i in range(0, len(decide), 512):
        gate.tap(decide[i:i+512])
    g = gate.apply(samples)
    return None if g is None else audio.normalize(g, config.TARGET_RMS, config.MAX_GAIN)[0]

def main():
    import jiwer, openvino_genai as ov_genai
    OUT.mkdir(exist_ok=True)
    metas = json.load(open(B / CLIPS / "index.json"))
    vad_path = vad.model_path(directory=B / "models/silero")
    hops = {k: [] for k in MODELS}
    for m in metas:  # pass 1: AEC only, NPU idle
        d = B / CLIPS / m["name"]
        mic, _ = sf.read(d / "mic.wav", dtype="float32"); ref, _ = sf.read(d / "ref.wav", dtype="float32")
        for k in MODELS:
            for mode in MODES:
                p = CACHE / f"{k}@{mode}" / (m["name"] + ".npy")
                p.parent.mkdir(parents=True, exist_ok=True)
                if mode == "batch":
                    o = batch(k, mic, ref)
                else:  # session: one context, never reset, clips back to back (needs one fixed echo path)
                    o = stream(k, mic, ref, hops[k] if mode == "cold" else [], reset=(mode == "cold"))
                np.save(p, o)
    timing = {k: dict(n=len(v), mean=float(np.mean(v)), p50=float(np.median(v)), p90=float(np.percentile(v, 90)),
                      p99=float(np.percentile(v, 99)), max=float(np.max(v)), rtf=float(np.mean(v)) / 16.0) for k, v in hops.items()}
    print(json.dumps(timing, indent=1), flush=True)

    ovfetch.preload()
    model = config.model_dir("NPU") if hasattr(config, "model_dir") else None
    model = Path(os.path.expanduser("~/.local/share/vinowhisper/models/whisper-small.en-ov"))
    t = time.perf_counter()
    pipe = ov_genai.WhisperPipeline(str(model), device="NPU", STATIC_PIPELINE=True)
    print(f"NPU load {time.perf_counter()-t:.1f}s", flush=True)
    cfg = pipe.get_generation_config(); cfg.max_new_tokens = 440
    methods = ["oracle", "none"] + [f"{x}@{mode}" for mode in MODES for x in ("lvqe1.2", "lvqe1.4aec2.7k")] \
        + [f"lvqe1.2@{mode}+lvqe1.4aec2.7k@{mode}" for mode in MODES]
    res = {}; dec = []
    for meth in methods:
        for m in metas:
            d = B / CLIPS / m["name"]
            decide = None
            if meth == "oracle":
                out, _ = sf.read(d / "clean.wav", dtype="float32")
            elif meth == "none":
                out, _ = sf.read(d / "mic.wav", dtype="float32")
            elif "+" in meth:
                a, b = meth.split("+")
                decide = np.load(CACHE / a / (m["name"] + ".npy")); out = np.load(CACHE / b / (m["name"] + ".npy"))
            else:
                out = np.load(CACHE / meth / (m["name"] + ".npy"))
            fed = front_end(np.asarray(out, np.float32), vad_path, decide)
            t = time.perf_counter()
            text = "" if fed is None else str(pipe.generate(fed.tolist(), cfg)).strip()
            if fed is not None:
                dec.append(time.perf_counter() - t)
            hyp = norm(text)
            r = dict(method=meth, clip=m["name"], kind=m["kind"], cond=m["cond"], gated_out=fed is None, hyp=hyp,
                     hyp_words=len(hyp.split()))
            if m["kind"] == "fe":
                r["leak_per_min"] = r["hyp_words"] / (m["seconds"] / 60)
            else:
                ref_t = norm(m["near_text"]); o = jiwer.process_words(ref_t, hyp or "<empty>"); n = len(ref_t.split())
                r.update(wer=o.wer, dele=o.deletions / n, ins=o.insertions / n, ref_words=n)
            res[f"{meth}|{m['name']}"] = r
    json.dump(dict(timing=timing, results=res, npu_decode_mean=float(np.mean(dec))), open(OUT / "results.json", "w"), indent=1)
    print(f"NPU decode mean {np.mean(dec):.3f}s over {len(dec)}")
    for meth in methods:
        rs = [r for r in res.values() if r["method"] == meth]
        dt = [r for r in rs if r["kind"] == "dt"]; fe = [r for r in rs if r["kind"] == "fe"]
        n = sum(r["ref_words"] for r in dt)
        f = lambda k: sum(r[k] * r["ref_words"] for r in dt) / n
        print(f"| {meth} | {f('wer'):.1%} | {f('dele'):.1%} | {f('ins'):.1%} | {np.mean([r['leak_per_min'] for r in fe]):.1f} |")

if __name__ == "__main__":
    main()
