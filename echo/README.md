# vinoEcho

Echo cancellation for vinoWhisper's two-lane captions ([#216](https://github.com/karanshukla/vinoWhisper/issues/216)). Currently a Python package internal to vinoWhisper.

```python
from vinoecho import Echo

cleaned, gate = echo.process(mic, ref)  # 16 kHz float32, any length
```

- `cleaned` is the microphone audio with far-end echo removed by LocalVQE's 2.7K Kalman filter (which handles delay alignment without a suppression mask, preserving near-end speech). This stream is passed to Whisper.
- `gate` is LocalVQE v1.2's output, used only for the speech detector. The 2.7K filter leaves a faint residual of far-end speech while you are silent, whereas v1.2 suppresses it.
- Output is returned in 512-sample (32 ms) chunks. Each call returns all complete chunks and buffers any remaining samples.
- `state()` and `restore()` save and restore the filter state across sessions for a warm start.

The 2.7K port is in `daf.py` (NumPy; weights converted from the 17 KB GGUF into `.npz`). The v1.2 model is an OpenVINO IR built by `export/` (requiring `torch` and `einops`, not shipped in the package), hosted and hash-pinned by vinoWhisper. Licensed under Apache-2.0 with LocalVQE's notice (separate from vinoWhisper's MIT license).
