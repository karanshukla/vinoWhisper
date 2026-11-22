"""Export the v1.2 streaming step to OpenVINO IR (T hops per call) and a runner."""
import os, sys, time
from pathlib import Path
import numpy as np

HOP = 256

def export(bench, T):
    import torch, openvino as ov
    sys.path.insert(0, os.path.dirname(__file__))
    from v12_stream import Step, load_model
    step = Step(load_model(bench)); st = step.init_state()
    ex = (torch.zeros(1, T * HOP), torch.zeros(1, T * HOP), *st)
    with torch.no_grad():
        tr = torch.jit.trace(step, ex, check_trace=False)
    om = ov.convert_model(tr, example_input=ex, input=[ov.PartialShape(list(x.shape)) for x in ex])
    out = Path(bench) / "ov_v12s"; out.mkdir(exist_ok=True)
    ov.save_model(om, out / f"v12s_T{T}.xml", compress_to_fp16=False)
    return [x.numpy() for x in st]

class Runner:
    def __init__(self, xml, threads=1):
        import openvino as ov
        cm = ov.Core().compile_model(str(xml), "CPU", {"INFERENCE_NUM_THREADS": threads})
        self.req = cm.create_infer_request()
        self.shapes = [tuple(i.get_partial_shape().to_shape()) for i in cm.inputs]
        self.T = self.shapes[0][1] // HOP
        self.reset()
    def reset(self):
        self.state = [np.zeros(s, np.float32) for s in self.shapes[2:]]
    def __call__(self, mic, ref):  # mic, ref: T*HOP samples
        self.req.infer([mic[None].astype(np.float32), ref[None].astype(np.float32), *self.state])
        outs = [self.req.get_output_tensor(i).data.copy() for i in range(len(self.shapes) - 1)]
        self.state = outs[1:]
        return outs[0].reshape(-1)

if __name__ == "__main__":
    import soundfile as sf, json
    bench = sys.argv[1]; B = Path(bench)
    for T in (1, 2, 32):
        export(bench, T)
    clips = [m for m in json.load(open(B / "clips_real/index.json")) if m["kind"] != "ne"]
    for T in (1, 2, 32):
        r = Runner(B / "ov_v12s" / f"v12s_T{T}.xml")
        times, corrs = [], []
        for m in clips[:6]:
            mic, _ = sf.read(B / "clips_real" / m["name"] / "mic.wav", dtype="float32")
            ref, _ = sf.read(B / "clips_real" / m["name"] / "ref.wav", dtype="float32")
            g = np.load(B / "laptop_clips_real/wav/lvqe1.2@cold" / (m["name"] + ".npy"))
            r.reset(); blk = T * HOP; n = len(mic) // blk * blk; out = []
            for s in range(0, n, blk):
                t = time.perf_counter(); out.append(r(mic[s:s+blk], ref[s:s+blk])); times.append(time.perf_counter() - t)
            o = np.concatenate(out)
            corrs.append(np.corrcoef(o, g[:n])[0, 1])
        ms = np.array(times) * 1000 / T
        print(f"T={T:2d}: corr with ggml v1.2 {np.mean(corrs):.6f} (min {np.min(corrs):.6f})  per 16 ms hop: mean {ms.mean():.2f} ms, p99 {np.percentile(ms, 99):.2f} ms, per call max {np.max(times)*1000:.1f} ms", flush=True)
