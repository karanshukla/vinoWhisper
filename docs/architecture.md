# Design decision: scale-to-zero, not an always-on daemon

Deliberately **socket-activated**, not a resident systemd service. Same
lazy-load/idle-unload shape as serverless cold starts, via systemd's own
primitives:

- `vinowhisper-server.socket` owns the listening socket at boot,
  `$XDG_RUNTIME_DIR/vinowhisper/server.sock`, readable by your user only. No
  model loaded, no Python process running.
- Systemd starts `vinowhisper-server.service` on the _first_ connection. That
  is when the NPU model load (~10-30s) happens.
- The service tracks its own last-request time and self-exits after
  `config.IDLE_TIMEOUT_S` (5 min). The socket unit is untouched, so the next
  caption session respawns it.

Why bother on a 16GB machine for a ~500MB model: the always-on version holds
that RAM resident regardless of use, and relying on the kernel to swap it to
zram does not reliably help, since 500MB rarely generates enough pressure on
16GB to get reclaimed. Scale-to-zero is the deterministic version of the same
idea. It is a conscious choice, not a default that snuck in.

**Measured 2026-10-06, which is why the window is 5 minutes and not 30.** The
loaded multilingual server is 1.17-1.24GB resident, and 1.06GB of that is
file-backed: the NPU driver's compiled model blobs, mapped from
`~/.cache/ze_intel_npu_cache`. Only 100-170MB is anonymous memory. So swap and
zram cannot help (they hold anonymous pages; zram was on and held nothing), and
the blobs are clean cache the kernel can drop anyway. What helps is unloading.
With the blob cache warm, a stopped server answered its first `/health` with
the model loaded in 0.69s (once, on this laptop, to `/health` and not to the
first decoded caption). The 40-70s cold load is the first compile, or the first
one after an OpenVINO or driver upgrade changes the cache key.

`vinowhisper-caption` health-checks the server before starting the loop, so a
cold start shows up as an explicit "waiting for the transcription server"
line rather than as the captions appearing to be broken for 30 seconds.

**Stopping it.** There's no daemon to manage day to day. The socket unit
holds the socket with no process behind it until something connects, and the
service self-exits after `IDLE_TIMEOUT_S` regardless. Two commands cover the
rest:

```bash
systemctl --user stop vinowhisper-server.service          # drop the resident NPU process now
systemctl --user disable --now vinowhisper-server.socket  # full teardown
```

The socket unit is what respawns the service, so disabling it (not just the
service) is the one to use before a reboot or when you're done with the tool
for a while, otherwise the next `vinowhisper-caption` run just spawns it
again on first connection.

## The server itself

- `/transcribe` takes raw little-endian float32 PCM, 16kHz mono and under 30s,
  with no WAV container, because the client is a rolling buffer that never has
  a file. A body longer than one `MAX_WINDOW_S` window (plus 4KB) is refused
  with a 413 before it is read.
- The service unit carries only the hardening a *user* unit can apply without
  user namespaces: the seccomp-backed settings, `NoNewPrivileges` and
  `UMask=0077`. `systemd-analyze security --offline` scores it 7.8, down from
  9.6 (2026-10-02, systemd 255). The next steps are deliberately not taken:
  `MemoryDenyWriteExecute` breaks OpenVINO's CPU plugin, which JIT-compiles
  its kernels; `PrivateDevices` hides `/dev/accel` and `/dev/dri`, so the NPU
  and GPU vanish and "auto" lands on the CPU; `ProtectClock`, `PrivateTmp`,
  `ProtectHome` and the rest of the namespace settings turn on `PrivateUsers`
  in a user unit, which needs unprivileged user namespaces. A CPU
  `compile_model` ran under the added set on 2026-10-02; the NPU and GPU have
  not.
- Calls into the pipeline are serialized by a lock. `WhisperPipeline` is not
  documented as thread-safe, and the NPU static pipeline holds one set of
  compiled requests. The server is threaded only so `/health` answers during
  a decode.
- The device is chosen before the model loads, so a fallback warning reaches
  the journal and `/health` even when loading then fails on a missing export.
- Started by hand rather than by the socket unit, it never idles out; the
  unload applies only to a socket-activated start. It binds the same path
  itself (`0600`, in a `0700` directory), refuses to start without
  `XDG_RUNTIME_DIR`, and refuses if a server is already listening there.
- It is a Unix socket rather than `127.0.0.1:8099` (the port until 0.6.x)
  because a localhost port belongs to no user: any local account could
  connect to it, or bind it first while it was free. `requests` cannot dial a
  Unix socket, so `client.py` mounts a small adapter that sends every request
  in its session there. To poke it by hand:
  `curl --unix-socket "$XDG_RUNTIME_DIR/vinowhisper/server.sock" http://localhost/health`.

## Why OpenVINO GenAI, not ONNX Runtime

Never weighed until 2026-09-28, when a sibling tool (ovfetch, built for Gaze,
which does run ONNX Runtime) claimed vinoWhisper needed it. It does not, and
switching would cost more than it buys:

- Whisper is an encoder, an autoregressive decoder with a KV cache, mel
  features and a tokenizer. `WhisperPipeline` is all of it, including token
  streaming and `max_new_tokens`; ONNX Runtime runs graphs, and the loop
  would be ours.
- The NPU wants static shapes. `STATIC_PIPELINE=True` with the
  `--disable-stateful` export is Intel's NPU path for Whisper; ONNX Runtime's
  OpenVINO execution provider reaches the same compiler with that work moved
  into this repo.
- Intel's `onnxruntime-openvino` bundles its own OpenVINO, 2025.4.1 as of
  2026-09-28, and this project's floor is 2026.3.1.
- Both end in the same NPU plugin and compiler, so there is no speed to gain.

ONNX Runtime would be a candidate only as the non-Intel backend, where
whisper.cpp over Vulkan is the current pick (see `docs/hardware.md`).

## Power

Measured 2026-09-12 on the Wildcat Lake laptop, per process over 15 to 30s,
with nothing being said:

| | Overlay visible, silence | Overlay hidden |
|---|---|---|
| NPU | suspended | suspended |
| Speaker sink | running, held by the capture | suspended |
| `vinowhisper-gui` | 3.7 wakeups/s | 0 |
| `vinowhisper-caption` + `pw-record` | 2.2 + 51.5 wakeups/s | not running |
| `vinowhisper-server` | 0 | 0 |

- **A loaded model does not hold the NPU awake.** The kernel parks it 100ms
  after the last inference (`/sys/class/accel/accel0/device/power`), and the
  caption loop skips inference below the silence gate, so a resident server
  costs RAM, not NPU power.
- **The cost of a visible overlay is the capture.** A stream on the sink
  monitor keeps the audio hardware running even when nothing plays, and
  `pw-record` wakes at PipeWire's graph clock (quantum 1024 at 48kHz, about 47
  times a second), which it does not set. Hiding stops the capture, and the
  sink suspends within seconds.
- **Hidden, nothing wakes** apart from the server's idle watchdog, once every
  30s until it exits. Before this was measured, the GUI ticked every second
  for a counter it was not showing, the server woke twice a second for
  `serve_forever`'s shutdown poll, and every silence event redrew the overlay.
