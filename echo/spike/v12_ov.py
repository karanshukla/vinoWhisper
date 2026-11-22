"""v1.2 through OpenVINO: export the PyTorch reference at a fixed length
(context + block), then run it block by block with left context."""
import os, sys, time
from pathlib import Path
import numpy as np

B = Path(sys.argv[1]); SRC = B / "src/LocalVQE/pytorch"; IR = B / "ov_v12"; FS = 16000

def export(total):
    import torch, yaml, openvino as ov
    sys.path.insert(0, str(SRC))
    from localvqe.model import LocalVQE
    cfg = yaml.safe_load(open(SRC / "configs/default.yaml"))
    state = torch.load(B / "models/localvqe-v1.2-1.3M.pt", map_location="cpu", weights_only=False)
    mcfg = dict(cfg["model"])
    for k, v in (state.get("model_config") or {}).items():
        if k in ("mic_channels", "far_channels", "bottleneck_hidden", "kernel_size"):
            mcfg[k] = v
    model = LocalVQE(**mcfg, n_freqs=cfg["audio"]["n_freqs"])
    model.load_state_dict(state.get("model_state_dict", state.get("model")))
    model.align.fold_temperature(); model.eval()

    class Wav(torch.nn.Module):
        def __init__(s, m): super().__init__(); s.m = m
        def forward(s, mic, ref):
            out = s.m(mic, ref)
            if isinstance(out, tuple): out = out[0]
            return s.m.decoder(out, length=mic.shape[-1])

    w = Wav(model)
    ex = (torch.zeros(1, total), torch.zeros(1, total))
    with torch.no_grad():
        traced = torch.jit.trace(w, ex, check_trace=False)
    om = ov.convert_model(traced, example_input=ex, input=[ov.PartialShape([1, total]), ov.PartialShape([1, total])])
    IR.mkdir(exist_ok=True)
    ov.save_model(om, IR / f"v12_{total}.xml", compress_to_fp16=False)
    # torch reference on a random-ish input, for an export check
    x = torch.from_numpy(np.sin(np.arange(total) * 0.01).astype(np.float32))[None] * 0.1
    with torch.no_grad():
        return w(x, x * 0.5).numpy().reshape(-1), x.numpy().reshape(-1)

if __name__ == "__main__":
    import openvino as ov
    for ctx_s in (1.0, 2.0, 4.0):
        total = int((ctx_s + 0.512) * FS)
        t = time.perf_counter(); ref_out, x = export(total)
        core = ov.Core()
        cm = core.compile_model(str(IR / f"v12_{total}.xml"), "CPU", {"INFERENCE_NUM_THREADS": 1})
        o = cm([x[None], (x * 0.5)[None]])[0].reshape(-1)
        print(f"ctx {ctx_s}s: export {time.perf_counter()-t:.1f}s, ov vs torch max|err| {np.abs(o-ref_out).max():.2e}", flush=True)
