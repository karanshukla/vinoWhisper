# Design decision: scale-to-zero, not an always-on daemon

Deliberately **socket-activated**, not a resident systemd service. Same
lazy-load/idle-unload shape as serverless cold starts, via systemd's own
primitives:

- `vinowhisper-server.socket` owns the listening port at boot. No model
  loaded, no Python process running.
- Systemd starts `vinowhisper-server.service` on the _first_ connection. That
  is when the NPU model load (~10-30s) happens.
- The service tracks its own last-request time and self-exits after
  `config.IDLE_TIMEOUT_S` (30 min). The socket unit is untouched, so the next
  caption session respawns it.

Why bother on a 16GB machine for a ~500MB model: the always-on version holds
that RAM resident regardless of use, and relying on the kernel to swap it to
zram does not reliably help, since 500MB rarely generates enough pressure on
16GB to get reclaimed. Scale-to-zero is the deterministic version of the same
idea. Whether 30 minutes is the right window, or whether this is solving a
problem too small to matter at ~3% of 16GB, is still open. It is a conscious
choice, not a default that snuck in.

`vinowhisper-caption` health-checks the server before starting the loop, so a
cold start shows up as an explicit "waiting for the transcription server"
line rather than as the captions appearing to be broken for 30 seconds.

**Stopping it.** There's no daemon to manage day to day. The socket unit
holds the port with no process behind it until something connects, and the
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
  a file.
- Calls into the pipeline are serialized by a lock. `WhisperPipeline` is not
  documented as thread-safe, and the NPU static pipeline holds one set of
  compiled requests. The server is threaded only so `/health` answers during
  a decode.
- The device is chosen before the model loads, so a fallback warning reaches
  the journal and `/health` even when loading then fails on a missing export.
- Started by hand rather than by the socket unit, it never idles out; the
  unload applies only to a socket-activated start.

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
