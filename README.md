# vinoWhisper

NPU-accelerated local voice typing for Fedora/KDE, using OpenVINO GenAI's
`WhisperPipeline` on the Intel NPU. Named after
[vinoAuthFace](https://github.com/karanshukla/vinoAuthFace) — same idea,
OpenVINO doing the NPU work, different feature.

Design doc and rationale (why this is being built from scratch instead of
reusing `whisper-npu-server`, which turned out to be partly dead):
[wildcat-lake-linux/input/f5-voice-typing.md](https://github.com/karanshukla/wildcat-lake-linux/blob/main/input/f5-voice-typing.md).

**Status: scaffolding only, not functional yet.** Nothing has been run —
`openvino`/`openvino-genai`/`ydotool` aren't installed, no model has been
converted, and none of this has been tested end to end.

## Layout

```
vinowhisper/
  config.py       paths, ports, sample rate, idle timeout
  transcriber.py  WhisperTranscriber — wraps openvino_genai.WhisperPipeline (device="NPU")
  server.py       local-only Flask server, socket-activated + self-idle-exit (see below)
  recorder.py     Recorder — toggle start/stop of pw-record via a pidfile
  injector.py     TextInjector — ydotool-based text injection (KDE Wayland has no wtype support)
  toggle.py       entry point bound to the Meta+H KDE shortcut
scripts/
  convert_model.sh   optimum-cli export wrapper (whisper-small.en -> OpenVINO IR)
systemd/
  vinowhisper-server.socket    systemd owns the listening port, starts the service lazily
  vinowhisper-server.service   the actual transcription server, socket-activated (no [Install])
```

## Design decision: scale-to-zero, not an always-on daemon

This is deliberately built as a **socket-activated** service, not a plain
resident systemd service — the same lazy-load/idle-unload shape as
serverless/Lambda cold starts, just via systemd's native primitives instead
of a cloud provider's:

- `vinowhisper-server.socket` owns the listening port at boot. This costs
  nothing — no model loaded, no Python process running.
- Systemd only starts `vinowhisper-server.service` on the *first* connection
  to that socket. That's when the NPU model load (~10-30s) actually happens
  — equivalent to a Lambda cold start.
- The service tracks its own last-request time (`server.py`'s
  `_idle_watchdog`) and self-exits after `config.IDLE_TIMEOUT_S` (30 min
  default) of no dictation activity. The socket unit is untouched by this —
  the next press respawns the service and pays the cold-start cost again.

Why bother, on a 16GB machine, for a ~500MB model: the always-on version
holds that RAM resident indefinitely regardless of whether it's actually
used that hour. A relying on the kernel to swap it to zram under memory
pressure doesn't reliably help here either — 500MB rarely triggers enough
pressure on 16GB to get reclaimed, so it would just sit resident forever.
Scale-to-zero is the deterministic version of the same idea: it trades a
cold-start hit after idle periods for guaranteed reclaim, same trade-off as
any serverless architecture. Worth deciding later if 30 minutes is even the
right window, or if this is solving a problem too small to matter (~3% of
16GB) — noted here as a conscious choice, not a default that snuck in.

## Setup (once you're ready to actually run this)

1. `python -m venv .venv && .venv/bin/pip install -e .`
2. `./scripts/convert_model.sh` to produce the OpenVINO IR model.
3. Install `ydotool`, enable `ydotoold` as a user service.
4. Copy both `systemd/vinowhisper-server.socket` and
   `systemd/vinowhisper-server.service` into `~/.config/systemd/user/`, then
   `systemctl --user enable --now vinowhisper-server.socket` — enable the
   **socket** unit, not the service; the service has no `[Install]` section
   on purpose, since it's only ever meant to be started by the socket.
5. Repoint the existing `Meta+H` KDE global shortcut (currently Ghostty's
   `new-window`) to `vinowhisper-toggle`.

See the design doc linked above for the full verification plan and known
open items before doing any of this for real.
