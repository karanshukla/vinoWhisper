# Architecture

vinoWhisper splits audio capture/UI from model inference using an on-demand local server that starts automatically and exits when idle.

## On-demand server (scale-to-zero)

Nothing runs in the background until a client needs inference:

1. Any client (captions, dictation, file transcription, or `vinowhisper-doctor`) connects to `$XDG_RUNTIME_DIR/vinowhisper/server.sock`.
2. If no server is listening, `vinowhisper/launch.py` spawns `vinowhisper-server` detached in its own session, acquires a lock file beside the socket so concurrent clients start only one instance, waits for the socket to accept connections, and retries. This is when the NPU model load (~10–30s cold) occurs.
3. The server tracks the timestamp of its last request and exits automatically after `config.IDLE_TIMEOUT_S` (5 minutes).

`vinowhisper-caption` runs a `/health` check before entering the caption loop so a cold start displays an explicit "waiting for the transcription server" message rather than appearing hung.

### Startup overhead and history

Measured 2026-11-14 on a cloud Xeon with a stubbed model:

- `vinowhisper-server` takes 0.25–0.30s from `exec` until the socket accepts (Python importing Flask and NumPy). `launch.ensure` probes the socket every 2 ms and returns in ~0.25s; once running, client requests take a single `connect()`.
- `launch.py` imports `subprocess`, `fcntl`, and related modules inside its functions, adding ~0.2 ms to CLI startup.
- `launch.server_main` handles `vinowhisper-server --stop` before importing the server module (~0.1s with a running server vs. 0.35s when importing the full server), keeping language switches responsive in the tray.

Before 2026-11-14, a systemd user socket unit managed the socket and started the server. Client-side activation removed the systemd dependency and made Flatpak packaging possible (Flatpak cannot export systemd units). `vinowhisper-setup`, upgrades, and `vinowhisper-server --stop` clean up old systemd units automatically, while the server still accepts sockets passed by an existing unit until removed. Without systemd, `Restart=on-failure` is replaced by starting a fresh server on the next client request (`failures.py` still prevents repeated retries on a broken device).

- **Device config:** Stored in `~/.config/vinowhisper/server.json` (written by `vinowhisper-setup --device`; overridden by `--device` on `vinowhisper-server`).
- **Logs:** Written to `~/.local/state/vinowhisper/server.log` and rotated to `server.log.old` on startup once past 1 MB.

### Why a 5-minute idle timeout

Measured 2026-10-06 on the multilingual server: the resident server uses 1.17–1.24 GB of RAM, of which 1.06 GB is file-backed compiled NPU model blobs mapped from `~/.cache/ze_intel_npu_cache` and only 100–170 MB is anonymous memory. Because swap and zram only reclaim anonymous pages (zram stayed empty) and ~500 MB–1.2 GB rarely triggers reclaim pressure on a 16 GB machine, unloading the process deterministically after 5 idle minutes is more reliable than keeping an always-on daemon.

With a warm NPU blob cache, restarting a stopped server answered `/health` with the model loaded in 0.69s (single measurement on the Wildcat Lake laptop, to `/health` rather than first decode). The 40–70s cold compile occurs only on the first run or after an OpenVINO or driver update invalidates the cache key.

### Stopping the server manually

To unload the model immediately without waiting for `IDLE_TIMEOUT_S`:

```bash
vinowhisper-server --stop
```

This sends `POST /quit` over the Unix socket and waits for the process to exit (aborting any in-flight decode).

## The server itself

- **Audio endpoint:** `/transcribe` accepts raw little-endian float32 PCM (16 kHz mono, under 30s) with no WAV header, matching the client's rolling buffer. Payloads larger than `MAX_WINDOW_S` plus 4 KB are rejected with HTTP 413 before reading.
- **Process hardening:** On startup, the server sets umask `0077` and `PR_SET_NO_NEW_PRIVS` (matching the former systemd unit's `UMask=0077` and `NoNewPrivileges=yes`). The former unit's seccomp directives (`RestrictAddressFamilies`, `RestrictNamespaces`, `LockPersonality`) were dropped when the unit was removed on 2026-11-14. Note that `MemoryDenyWriteExecute` breaks OpenVINO's JIT-compiled CPU plugin, and `PrivateDevices` hides `/dev/accel` and `/dev/dri`, preventing NPU and GPU detection.
- **Concurrency:** Pipeline calls are serialized behind a lock because `WhisperPipeline` is not documented as thread-safe and the NPU static pipeline holds a single set of compiled requests. The HTTP server is threaded only so `/health` responds during an active decode.
- **Early device selection:** The target device is selected before model loading so fallback warnings appear in logs and `/health` even if loading later fails due to a missing export.
- **Unix socket transport:** The server binds `$XDG_RUNTIME_DIR/vinowhisper/server.sock` (`0600` inside a `0700` directory), removes stale sockets left by dead servers, and exits with an error if `XDG_RUNTIME_DIR` is unset or if another server is already listening. (Until 0.6.x it used `127.0.0.1:8099`, which lacked per-user access control.) `client.py` mounts a custom `requests` adapter for Unix sockets. You can query the server manually with:

  ```bash
  curl --unix-socket "$XDG_RUNTIME_DIR/vinowhisper/server.sock" http://localhost/health
  ```

## Why OpenVINO GenAI instead of ONNX Runtime

Evaluated 2026-09-28 (when sibling tool `ovfetch`, built for ONNX Runtime workloads like Gaze, noted ONNX Runtime as an option). Switching would add maintenance without improving performance:

- **Complete Whisper loop:** `WhisperPipeline` handles mel feature extraction, encoder execution, autoregressive decoding with KV caching, tokenization, token streaming, and `max_new_tokens`. ONNX Runtime executes graphs only, requiring vinoWhisper to reimplement the loop.
- **Static NPU shapes:** `STATIC_PIPELINE=True` with `--disable-stateful` is Intel's supported NPU path for Whisper; using ONNX Runtime's OpenVINO execution provider would reach the same compiler while moving static-pipeline management into this repository.
- **OpenVINO version floor:** Intel's `onnxruntime-openvino` bundled OpenVINO 2025.4.1 as of 2026-09-28, whereas vinoWhisper requires OpenVINO 2026.4.1+.
- **Identical backend:** Both invoke the same OpenVINO NPU plugin and compiler, offering no speed advantage.

ONNX Runtime would only be considered for non-Intel hardware, where `whisper.cpp` over Vulkan is currently the preferred candidate (see [hardware.md](hardware.md)).

## Power

Measured 2026-09-12 on the Wildcat Lake laptop over 15–30s intervals with no audio playing:

| Component | Overlay visible (silence) | Overlay hidden |
|---|---|---|
| NPU | suspended | suspended |
| Speaker sink | running (held by capture) | suspended |
| `vinowhisper-tray` | 3.7 wakeups/s | 0 |
| `vinowhisper-caption` + `pw-record` | 2.2 + 51.5 wakeups/s | not running |
| `vinowhisper-server` | 0 | 0 |

- **Idle NPU power:** Holding a loaded model costs RAM, not NPU power. The kernel suspends the NPU 100 ms after the last inference (`/sys/class/accel/accel0/device/power`), and the caption loop skips inference below the silence gate.
- **Visible overlay cost:** An active sink-monitor capture keeps the audio hardware awake even in silence, and `pw-record` wakes at PipeWire's graph clock (quantum 1024 at 48 kHz, ~47 times/s). Hiding the overlay stops capture and allows the speaker sink to suspend within seconds.
- **Hidden overlay cost:** Nothing wakes except the server's idle watchdog once every 30s until exit. (Earlier versions woke more frequently due to a 1s GUI counter timer, `serve_forever`'s 0.5s shutdown poll, and redrawing on every silence event; all three were fixed after these measurements.)
