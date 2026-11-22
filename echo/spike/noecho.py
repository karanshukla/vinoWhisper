"""Reference active, no echo in the mic: your quiet live takes + an unrelated far-end ref."""
import json, os, sys
from pathlib import Path
import numpy as np
B = Path(sys.argv[1]); sys.argv = [sys.argv[0], str(B)]
sys.path.insert(0, os.path.dirname(__file__))
import soundfile as sf
import laptop as L, daf
from v12_stream_ov import Runner
import jiwer, openvino_genai as ov_genai
from vinowhisper import vad, ovfetch
ovfetch.preload()
pipe = ov_genai.WhisperPipeline(os.path.expanduser("~/.local/share/vinowhisper/models/whisper-small.en-ov"), device="NPU", STATIC_PIPELINE=True)
cfg = pipe.get_generation_config(); cfg.max_new_tokens = 440
vad_path = vad.model_path(directory=B / "models/silero")
P = json.load(open(B / "live/passages.json"))
w = daf.load_weights(B / "models/localvqe-v1.4-aec-2.7K-f32.gguf")
r = Runner(B / "ov_v12s/v12s_T1.xml")
far = sf.read(B / "live/far0.wav", dtype="float32")[0]
for take in ("A_quiet", "B_quiet"):
    mic = np.load(B / "live" / f"{take}_mic.npy"); n = min(len(mic), len(far)) // 256 * 256
    mic = mic[:n]; ref = far[:n] * 0.5
    f = daf.DAF(w); r.reset()
    e = np.concatenate([f.process(mic[i:i+256], ref[i:i+256]) for i in range(0, n, 256)])
    g = 2.0 * np.concatenate([r(mic[i:i+256], ref[i:i+256]) for i in range(0, n, 256)])
    g = np.concatenate([g[256:], np.zeros(256, np.float32)])  # v1.2 is one hop late
    truth = L.norm(P[take[0]])
    for name, x, decide in (("raw mic", mic, None), ("2.7K", e, None), ("v1.2 gate + 2.7K", e, g)):
        fed = L.front_end(x, vad_path, decide)
        hyp = "" if fed is None else L.norm(str(pipe.generate(fed.tolist(), cfg)))
        # score only the passage: drop words before its first word / after its last, as the takes include chatter
        o = jiwer.process_words(truth, hyp or "<empty>")
        print(f"{take} {name:17s} WER {o.wer:6.1%} del {o.deletions/len(truth.split()):5.1%} | e/mic {10*np.log10(np.mean(x**2)/np.mean(mic**2)):5.2f} dB | {hyp[:110]}", flush=True)
