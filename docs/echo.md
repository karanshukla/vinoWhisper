# Echo cancellation for two-lane captions (vinoEcho)

When using speakers instead of headphones, the microphone picks up the remote side of the call, causing remote speech to appear in both the `you` and `them` lanes of `--source both` ([terminal.md](terminal.md)). vinoEcho (`echo/`, imported as `vinoecho`, issue #216) removes far-end speaker echo from the microphone lane. It is currently an internal Python package inside this repository, licensed under Apache-2.0 with LocalVQE's notice (as a port derived from LocalVQE).

## What runs

Two [LocalVQE](https://github.com/localai-org/LocalVQE) models run on the CPU on a dedicated thread:

| Model | Role | Implementation |
|---|---|---|
| **v1.4 2.7K** | Produces the cleaned audio passed to Whisper. Uses a Kalman filter with built-in delay alignment and no suppression mask, preserving near-end speech. | NumPy port of `daf_frontend.cpp`; weights converted from the 17 KB GGUF into `.npz` and shipped in the wheel |
| **v1.2** | Drives the speech gate only. The 2.7K filter leaves a faint residual of far-end speech while you are silent that Silero still passes; gating on v1.2 eliminates those leaks. | Streaming step built from the PyTorch reference, exported to an OpenVINO IR (two 16 ms hops per call), hash-pinned in `vinowhisper/echo_model.json` |

`process(mic, ref) -> (cleaned, gate_signal)` outputs 512-sample (32 ms) chunks. Silero VAD and `SpeechGate` remain in `vinowhisper`.

## Benchmark

Evaluated on 31 synthetic calls built from LibriSpeech test-clean (16 double-talk, 12 far-end only, 3 near-end only), scored through the caption front end. CPU spike measured on `linuxlab` on 2026-11-21; laptop NPU runs measured the same day (issue #216):

| Method | Double-talk WER | Your words lost | Leaked words/min when silent |
|---|--:|--:|--:|
| oracle (no echo) | 4.9% | 0.3% | 0 |
| no AEC | 74.0% | 10.1% | 170.6 |
| **v1.2 gate + 2.7K** | **21.1%** | **1.4%** | **0** |
| 2.7K alone | 21.7% | 1.4% | 57.8 |
| v1.2 alone | 33.2% | 5.8% | 0 |
| NKF (no licence) | 15.3% | 0.0% | 52.9 |
| DTLN-aec 256 | 37.9% | 3.8% | 11.7 |
| sonora AEC3 linear | 41.9% | 11.0% | 96.1 |
| WebRTC AEC3 | 69.9% | 47.1% | 7.1 |

Filters that use a suppression mask cut 4% to 47% of near-end words. Linear filters preserve near-end words but leak far-end speech during silence, which is why v1.2 is used as the speech gate. The 21.1% result used LocalVQE's batch mode, which estimates echo delay across the entire clip. The streaming path used for live capture estimates delay online over roughly 1 to 3 seconds, scoring 37.9% with a fresh context per clip. Keeping filter state across an entire caption session allows it to converge, so the state is never reset between decode windows.

Real speaker echo on the laptop (65% volume, 91 to 97 ms speaker-to-mic delay, 16 double-talk clips, 2 live takes; single room, directional): no AEC scored 63.0% WER, streaming with a fresh context per clip scored 44.2%, and **a single session context achieved 23.1% WER, 2.3% near-end words lost, and 0 leaks**. Running both models on one thread alongside Whisper on the NPU took 9.9 ms mean (16.3 ms p99) per 32 ms chunk.

Verified 2026-11-22 on `linuxlab` (CPU only, no concurrent decoding): the NumPy port matches LocalVQE's `daf_cli` output on speech with 0.999999999 correlation (`tests/test_echo.py`, fixture `tests/fixtures/echo_call.npz`, LibriSpeech CC BY 4.0). Processing 6 seconds of audio on one thread took 4.7 seconds on this VM (~25 ms per 32 ms chunk vs. 9.9 ms on the laptop) because its two shared vCPUs are not representative and fall behind when Whisper is also decoding.

## When it runs

Controlled by `--echo-cancel auto|on|off` (default `auto`; the tray passes the `echo_cancel` setting from `gui.json` as this flag, taking effect on the next session). `auto` disables echo cancellation when the default sink is Bluetooth (`device.api = bluez5`, including Bluetooth speakers), USB (`device.bus = usb`), or a headphone/headset jack port (read from `pactl --format=json list sinks`). Built-in speakers and HDMI remain enabled. Filter-chain or loopback sinks are followed through PipeWire links to the underlying output device (up to five hops in `echo_policy.py`; for example, this laptop's default sink is `effect_input.speaker_eq_v2`).

The output device is checked at session start and every 3 seconds thereafter, so switching to headphones mid-call toggles echo cancellation automatically. `vinowhisper-caption --echo-status` prints the current `auto` decision as JSON. `Ready` includes `echo_mode`, `echo_active`, and `echo_reason`; an `EchoStatus` event follows and repeats whenever the state changes.

When no echo is present in the microphone (for example, an inaudible sink or headphones misclassified as speakers), the filter altered voice levels by less than 0.05 dB and lost no words with an active reference, costing only CPU time.

## Warm start

The 2.7K filter state (excluding audio history) is saved under `$XDG_STATE_HOME/vinowhisper/echo/` at session end and whenever the output device changes, keyed by `(sink, port, mic source)`, and restored on the next start. From a cold start, suppression reaches 6 to 7 dB after roughly 4 seconds; carrying state over from a prior take reached 6 to 8 dB within 0.5 to 1 second (spike, 2026-11-21). Echo delay locks at 3.5 seconds and is not re-estimated: a subsequent *increase* within the 1.024 s filter span can be re-learned, whereas a *decrease* shifts echo before the first tap until reset. Keying by sink and port ensures a fresh filter whenever the audio path changes. Bluetooth and HDMI latencies fall within GCC-PHAT's 1.024 s search range by design, though neither has been measured directly.

## Safety nets

- **Text deduplication** (`echo_text.py`): Drops any `you` line of at least three words that fuzzy-matches what `them` said in the last 20 seconds, recording it in `Cycle.dropped` so replays remain complete.
- **Headphones hint** (`echo_meter.py`): Emits a `Hint` event (at most once every 5 minutes) when remote speech is audibly present in the microphone while the v1.2 gate indicates you are silent for at least half of 5 or more seconds of far-end speech within the last 10 seconds. These thresholds are initial estimates and need tuning on hardware.

## The gate model

`vinowhisper/echo_model.json` pins `v12s_T2.xml` and `v12s_T2.bin` by SHA-256 (hashed 2026-11-22 from the `linuxlab` export) at a specific commit of [`arankays/localvqe-v1.2-ov`](https://huggingface.co/arankays/localvqe-v1.2-ov) on Hugging Face, including LocalVQE's Apache-2.0 LICENSE and a model card crediting its commit. Served files match the pinned hashes (verified 2026-11-22). The model downloads on first use alongside the speech detector; if the download fails, the session continues with echo cancellation disabled and reports the error. `echo/export/export_v12.py` rebuilds the IR (requires `torch`, `einops`, `pyyaml`, a LocalVQE checkout, and the v1.2 checkpoint; not shipped).

## Not here

Alternatives ruled out: linking `liblocalvqe.so` (would require a C++ build in the Flatpak), WebRTC AEC3, DTLN-aec, fdaf-aec, pyaec, NKF (no license), PipeWire's `module-echo-cancel` (modifies the user's audio graph), and a hand-written energy gate instead of v1.2. Mic-only captions and dictation can also pick up speaker playback and could eventually use the sink monitor as a reference, but that is outside the scope of #216. Still untested: HDMI output on physical hardware and multi-minute real calls.
