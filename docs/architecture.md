# Design decision: scale-to-zero, not an always-on daemon

Deliberately **started on demand**, not a resident service. Same
lazy-load/idle-unload shape as serverless cold starts, with no service manager:

- Nothing runs until something needs the server. No model loaded, no Python
  process.
- Every client (captions, dictation, a file import, the doctor) connects to
  `$XDG_RUNTIME_DIR/vinowhisper/server.sock`. When nobody is listening, the
  client starts `vinowhisper-server` itself (`vinowhisper/launch.py`), detached
  and in its own session, waits for the socket and connects again. That is when
  the NPU model load (~10-30s) happens. A lock beside the socket keeps two
  clients arriving together to one server.
- The server tracks its own last-request time and exits after
  `config.IDLE_TIMEOUT_S` (5 min). The next client starts it again.

Until 2026-11-14 a systemd socket unit held the socket and started the server.
That tied it to systemd and cannot work in a Flatpak, which exports no units, so
the clients do what the socket unit did. `vinowhisper-setup`, an upgrade and
`vinowhisper-server --stop` remove the old units; until then a leftover socket
unit keeps working, since the server still takes a socket systemd passes it.
What systemd gave and this does not: `Restart=on-failure` (a crashed server is
started by the next request instead, and `failures.py` still keeps a broken
device from being retried forever) and the journal (below).

The device, which the unit's `ExecStart` carried, is in
`~/.config/vinowhisper/server.json` (`vinowhisper-setup --device` writes it; a
`--device` on the server overrides it). The server's output goes to
`~/.local/state/vinowhisper/server.log`, moved to `server.log.old` past 1 MB
when a server starts.

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

**Stopping it.** There's no daemon to manage day to day: the server exits
after `IDLE_TIMEOUT_S` on its own. To drop the resident NPU process now:

```bash
vinowhisper-server --stop
```

That posts `/quit` to the socket and waits for the server to go. A decode in
flight is cut off, as `systemctl stop` did. Nothing is left behind to tear
down: with no server and no client, nothing is running.

## The server itself

- `/transcribe` takes raw little-endian float32 PCM, 16kHz mono and under 30s,
  with no WAV container, because the client is a rolling buffer that never has
  a file. A body longer than one `MAX_WINDOW_S` window (plus 4KB) is refused
  with a 413 before it is read.
- It sets a `0077` umask and `PR_SET_NO_NEW_PRIVS` on itself when it starts,
  which is what the old unit's `UMask=0077` and `NoNewPrivileges=yes` did. The
  unit's seccomp-backed settings (`RestrictAddressFamilies`,
  `RestrictNamespaces`, `LockPersonality` and the like) went with the unit on
  2026-11-14 and nothing replaces them. For the record of what could not be
  added even then: `MemoryDenyWriteExecute` breaks OpenVINO's CPU plugin, which
  JIT-compiles its kernels, and `PrivateDevices` hides `/dev/accel` and
  `/dev/dri`, so the NPU and GPU vanish and "auto" lands on the CPU.
- Calls into the pipeline are serialized by a lock. `WhisperPipeline` is not
  documented as thread-safe, and the NPU static pipeline holds one set of
  compiled requests. The server is threaded only so `/health` answers during
  a decode.
- The device is chosen before the model loads, so a fallback warning reaches
  the log and `/health` even when loading then fails on a missing export.
- It binds the socket itself (`0600`, in a `0700` directory), replacing one
  left by a server that has exited, refuses to start without
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
  2026-09-28, and this project's floor is 2026.4.1.
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
| `vinowhisper-tray` | 3.7 wakeups/s | 0 |
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
