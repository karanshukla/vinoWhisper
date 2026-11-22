# Echo cancellation for two-lane captions (vinoEcho)

With speakers instead of headphones, the mic hears the other side of the call, so their words land in both the `you` and `them` lanes of `--source both` ([terminal.md](terminal.md)). vinoEcho (`echo/`, imported as `vinoecho`, issue #216) takes their voice out of the mic lane. It is a Python package inside this repo, vinoWhisper-only until it is worth publishing, and Apache-2.0 with LocalVQE's notice (the port is derived from their code).

## What runs

Two [LocalVQE](https://github.com/localai-org/LocalVQE) models, both on the CPU, on one thread of their own:

| Model | Job | Form |
|---|---|---|
| **v1.4 2.7K** | The audio Whisper hears. A Kalman filter with its own delay alignment and no mask, so your words stay. | numpy port of `daf_frontend.cpp`; weights are the 17 KB GGUF as `.npz`, shipped in the wheel |
| **v1.2** | The speech gate only. 2.7K leaves a faint residual of their speech while you are silent, and Silero passes it; gating on v1.2's output stops every leak. | A streaming step built from the PyTorch reference, exported to an OpenVINO IR (two 16 ms hops per call), hash-pinned in `vinowhisper/echo_model.json` |

`process(mic, ref) -> (cleaned, gate_signal)`. Silero and `SpeechGate` stay in vinowhisper. Output arrives in 512-sample (32 ms) chunks.

## Benchmark

31 calls built from LibriSpeech test-clean (16 double talk, 12 far end only, 3 near end only), scored through the caption front end. Spike on linuxlab's CPU 2026-11-21, laptop runs on the NPU the same day (issue #216):

| Method | Double-talk WER | Your words lost | Leaked words/min when you are silent |
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

Filters with a mask or suppressor cut 4 to 47% of your words. The linear filters keep them but leak when you are silent, hence gating on v1.2. The 21.1% used LocalVQE's batch call, which primes the echo delay from the whole clip. The streaming call the capture path needs finds it online over about 1 to 3 s, so a fresh context per clip scores 37.9%; held for a session it converges, which is why the state lasts the whole caption session and is never reset per window.

Real speaker echo on the laptop (65% volume, speaker to mic delay 91 to 97 ms, 16 double-talk clips, 2 live takes; one room, directional): no AEC 63.0% WER, streaming with a fresh context per clip 44.2%, **one session context 23.1% WER, 2.3% of your words lost, 0 leaks**. Both models, one thread, per 32 ms chunk next to Whisper on the NPU: 9.9 ms mean, 16.3 ms p99.

Checked 2026-11-22 on linuxlab (CPU only, nothing else decoding): the numpy port matches LocalVQE's own `daf_cli` output on speech to correlation 0.999999999 (`tests/test_echo.py`, fixture `tests/fixtures/echo_call.npz`, LibriSpeech CC BY 4.0). One thread ran 6 s of audio in 4.7 s here (about 25 ms per 32 ms chunk, against the laptop's 9.9 ms): this VM's two shared vCPUs are not representative, and with Whisper decoding as well it falls behind.

## When it runs

`--echo-cancel auto|on|off` (default `auto`; the tray passes the `echo_cancel` setting from `gui.json` as this flag, so a change applies from the next session). `auto` turns it **off** when the default sink is Bluetooth (`device.api = bluez5`, speakers included), USB (`device.bus = usb`) or the jack's headphone or headset port (the active port's type, read from `pactl --format=json list sinks`). Built-in speakers and HDMI stay on. A filter-chain or loopback sink is followed through PipeWire's links to the device it plays into (this laptop's default sink is `effect_input.speaker_eq_v2`), up to five hops (`echo_policy.py`).

The decision is made at session start and again every 3 s, so moving to earphones mid-call switches it off and back. `vinowhisper-caption --echo-status` prints what Auto would do now as JSON, which is what the settings window shows under "Echo cancellation". `Ready` carries `echo_mode`, `echo_active` and `echo_reason`; an `EchoStatus` event follows it and repeats on each change.

With no echo in the mic (a sink that is not audible in the room, or headphones misread as speakers) the filter left a voice within 0.05 dB and lost no words with an active reference: harmless apart from the CPU.

## Warm start

The 2.7K filter state (not the audio history) is saved at session end, and when the output changes, under `$XDG_STATE_HOME/vinowhisper/echo/`, keyed by (sink, port, mic source), and loaded at the next start. Cold, suppression climbs to 6 to 7 dB by about 4 s; with the previous take's state carried over it reached 6 to 8 dB within 0.5 to 1 s (spike, 2026-11-21). The delay locks 3.5 s in and is never re-estimated, so a later *increase* inside the 1.024 s filter span is re-learned, but a *decrease* puts echo before the first tap until a reset, hence a different key (and a fresh filter) when the sink or its port changes. Bluetooth and HDMI latency fit the search range from the design (GCC-PHAT searches up to 1.024 s); neither was measured.

## Safety nets

- **Text dedup** (`echo_text.py`): a `you` line of at least three words that fuzzy-matches what `them` said in the last 20 s is dropped, kept in the event as `Cycle.dropped` so a replay still accounts for it.
- **Headphones hint** (`echo_meter.py`): when over the last 10 s their voice is audibly in the mic while the v1.2 gate says you are silent, for at least half of 5 s or more of far-end speech, a `Hint` event says so, at most once per 5 minutes. The thresholds are guesses, not measured: tune them on the laptop.

## The gate model

`vinowhisper/echo_model.json` pins `v12s_T2.xml` and `.bin` by sha256 (hashed 2026-11-22 from the export linuxlab ran) at a commit of [`arankays/localvqe-v1.2-ov`](https://huggingface.co/arankays/localvqe-v1.2-ov) on Hugging Face, with LocalVQE's Apache-2.0 LICENSE and a model card crediting its commit. The files served from that revision hash to the pinned values (checked 2026-11-22). They download on first use, like the speech detector's; if that fails the session runs with echo cancellation off and says why. `echo/export/` rebuilds them (`export_v12.py`; needs torch, einops, pyyaml, a LocalVQE checkout and the v1.2 checkpoint; never shipped).

## Not here

Ruled out: linking `liblocalvqe.so` (a C++ build in the Flatpak, a third language), WebRTC AEC3, DTLN-aec, fdaf-aec, pyaec, NKF (no licence), PipeWire's `module-echo-cancel` (it changes the user's audio graph) and a hand-written energy gate in place of v1.2. Mic-only captions and dictation hear what is playing too; both could take the monitor as a reference only, which is not in #216. Open: HDMI on real hardware and a real call of several minutes.
