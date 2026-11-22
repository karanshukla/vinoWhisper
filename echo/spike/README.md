# vinoEcho spike (#216)

Prototype code from the 2026-11-21 laptop runs for [#216](https://github.com/karanshukla/vinoWhisper/issues/216). Not part of the package; vinoEcho proper goes in `echo/src/vinoecho/`. Numbers are in the issue and its comment.

`daf.py` and `v12_stream.py` are derived from [LocalVQE](https://github.com/localai-org/LocalVQE) at `7f61cd05095d` (Apache-2.0, `LICENSE-LocalVQE`). Everything else here is vinoWhisper's MIT.

## The parts vinoEcho starts from

| File | What it is |
|---|---|
| `daf.py` | numpy port of LocalVQE's 2.7K front end (`ggml/daf_frontend.cpp`). Matches `liblocalvqe` to correlation 0.99998-1.0. |
| `v12_stream.py` | v1.2 as a streaming step (T hops + explicit state), built from the PyTorch reference's layers. Equals the whole-clip forward to 9e-7. Loads the checkpoint with `weights_only=True`. |
| `v12_stream_ov.py` | Exports the step to OpenVINO IR (`jit.trace`, `check_trace=False`, static shapes) and a `Runner` that carries the state. Multiply its output by 2 to match GGML's level. |

## Bench harness

Paths are hardcoded to `BENCH_DIR`. linuxlab has a copy at `~/aecbench` with the real recordings (`clips_real`, `real_raw`, `real_log.json`), the streaming IRs (`ov_v12s`), the laptop results (`laptop-results`) and these scripts (`spike`); no NPU there, so use CPU for Whisper. Scripts that import vinowhisper expect it at `~/Development/vinoWhisper` (linuxlab: `~/workspace/vinoWhisper`). Layout: `clips/` from linuxlab's `~/aecbench`, `models/` (the two GGUFs, `localvqe-v1.2-1.3M.pt`, Silero), `src/LocalVQE` built with `cmake -DLOCALVQE_BUILD_SHARED=ON`.

| File | Measures |
|---|---|
| `laptop.py` | LocalVQE through `liblocalvqe.so` (cold, batch, session), `SpeechGate` + `normalize`, Whisper on the NPU |
| `record.py` | Real speaker echo takes: plays each far end, captures mic + monitor with `capture.record_argv` |
| `live.py`, `score_live.py` | Reading passages over playback, and scoring them |
| `gate_sweep.py` | The energy-ratio gate that failed |
| `v12_ov.py`, `ov_gate.py` | v1.2 as a whole-clip IR run per block with left context (superseded by the streaming step) |
| `v12s_check.py`, `v12s_timing.sh` | Streaming IR: gate results vs GGML, paced timing next to Whisper |
| `contention*.{py,sh}`, `combined.{py,sh}` | Per-hop cost alone and next to NPU decoding |
| `noecho.py` | Active reference, no echo in the mic |
| `session.py` | A 250 s two-lane call with read passages; built, never run |
| `smoke.py` | `BENCH_DIR OUT.npy SPIKE_DIR`: both ports on 5 s of a real clip. Bit-identical on the laptop and linuxlab (2026-11-21). Reads the 2.7K weights from `localvqe-v1.4-aec-2.7K-f32.npz` (the GGUF's 22 tensors), so it needs no `gguf` package |
