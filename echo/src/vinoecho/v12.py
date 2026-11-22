"""LocalVQE v1.2 as an OpenVINO streaming step, derived from LocalVQE (Apache-2.0).
export/v12_stream.py builds the step from the PyTorch reference; this runs its IR."""

from pathlib import Path

import numpy as np

HOP = 256
# GGML's output is twice the PyTorch reference's (it skips the overlap-add divide), and the
# speech gate is tuned on GGML's level, so the IR's output is scaled to match.
GGML_LEVEL = 2.0


class Gate:
    def __init__(self, xml: Path, threads: int = 1) -> None:
        import openvino as ov

        compiled = ov.Core().compile_model(str(xml), "CPU", {"INFERENCE_NUM_THREADS": threads})
        self._request = compiled.create_infer_request()
        self._shapes = [tuple(i.get_partial_shape().to_shape()) for i in compiled.inputs]
        self.hops = self._shapes[0][1] // HOP
        self.reset()

    @property
    def block(self) -> int:
        return self.hops * HOP

    def reset(self) -> None:
        self._state = [np.zeros(shape, np.float32) for shape in self._shapes[2:]]

    def __call__(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        self._request.infer(
            [mic[None].astype(np.float32), ref[None].astype(np.float32), *self._state]
        )
        outputs = [
            self._request.get_output_tensor(i).data.copy() for i in range(len(self._shapes) - 1)
        ]
        self._state = outputs[1:]
        return outputs[0].reshape(-1) * GGML_LEVEL
