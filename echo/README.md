# vinoEcho

Echo cancellation for vinoWhisper's two-lane captions ([#216](https://github.com/karanshukla/vinoWhisper/issues/216)). Python only, vinoWhisper-only until it's worth publishing.

```python
from vinoecho import Echo

cleaned, gate = echo.process(mic, ref)  # 16 kHz float32, any length
```

- `cleaned` is the mic with their voice taken out by LocalVQE's 2.7K filter: a Kalman filter with its own delay alignment and no mask, so your words stay. This is what Whisper hears.
- `gate` is LocalVQE v1.2's output, for the speech detector only. The 2.7K filter leaves a faint residual of their speech while you're silent; v1.2 does not.
- Output comes in 512-sample (32 ms) chunks. A call returns what it could complete and holds the rest.
- `state()` and `restore()` carry the filter between sessions (a warm start).

The 2.7K port is `daf.py` (numpy; weights are the 17 KB GGUF as `.npz`). v1.2 is an OpenVINO IR built by `export/` (torch and einops; not shipped), hosted and hash-pinned by vinoWhisper. Apache-2.0 with LocalVQE's notice, not vinoWhisper's MIT.
