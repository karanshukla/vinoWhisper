"""Export v1.2's streaming step to OpenVINO IR: python export_v12.py LOCALVQE_CHECKOUT CHECKPOINT.pt OUT_DIR.
Needs torch, einops, pyyaml and openvino. Writes v12s_T2.xml/.bin (two hops per call, fp32)."""

import sys
from pathlib import Path

import openvino as ov
import torch
from v12_stream import HOP, Step, load_model

T = 2


def main(checkout: Path, checkpoint: Path, out: Path) -> None:
    step = Step(load_model(checkout, checkpoint))
    state = step.init_state()
    example = (torch.zeros(1, T * HOP), torch.zeros(1, T * HOP), *state)
    with torch.no_grad():
        traced = torch.jit.trace(step, example, check_trace=False)
    model = ov.convert_model(
        traced, example_input=example, input=[ov.PartialShape(list(x.shape)) for x in example]
    )
    out.mkdir(parents=True, exist_ok=True)
    ov.save_model(model, out / f"v12s_T{T}.xml", compress_to_fp16=False)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
