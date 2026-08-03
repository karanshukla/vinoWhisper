# vinoWhisper

NPU-accelerated local live captioning for Fedora/KDE, using OpenVINO GenAI's
`WhisperPipeline` on the Intel NPU. Named after
[vinoAuthFace](https://github.com/karanshukla/vinoAuthFace) — same idea,
OpenVINO doing the NPU work, different feature.

Originally scoped as toggle-mode voice typing (record, transcribe, inject
text via `ydotool`); pivoted 2026-08-03 to continuous live captioning
displayed on-screen instead — same NPU/Whisper backend, different consumer
of the output.

Design doc and rationale (why this is being built from scratch instead of
reusing `whisper-npu-server`, which turned out to be partly dead), plus the
full feasibility-spike writeup (bugs hit, benchmarks, streaming findings):
[wildcat-lake-linux/input/f5-voice-typing.md](https://github.com/karanshukla/wildcat-lake-linux/blob/main/input/f5-voice-typing.md).

**Status: transcription backend confirmed working on NPU (2026-08-03);
live-captioning implementation itself not built yet.** whisper-small.en
converted, benchmarked (~1.19s/30s window, ~0.204s to first streamed token),
and verified as genuinely running on NPU, not falling back to CPU. What's
below (`recorder.py`/`injector.py`/`server.py`) is still the old toggle-mode
design and needs a rewrite for continuous capture + overlay display — see
the design doc's "Planned architecture" section.

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

## Setup (transcription backend — confirmed working 2026-08-03)

1. Use Python 3.13, not 3.14 — 3.14 made `functools.partial` a descriptor,
   which breaks `optimum`'s export code outright (see CLAUDE.md/design doc
   for the root cause). `python3.13 -m venv .venv`.
2. Install with the nightly OpenVINO wheel index — stable `openvino-genai`
   (2026.2.1 as of writing) can't build the NPU static Whisper pipeline:
   ```
   .venv/bin/pip install --pre --extra-index-url https://storage.openvinotoolkit.org/simple/wheels/nightly -e .
   ```
3. `./scripts/convert_model.sh` to produce the whisper-small.en OpenVINO IR
   (already includes `--disable-stateful`, required for NPU).
4. From here down is **not yet built/verified** — the live-captioning
   consumer of the transcription backend above (continuous capture, overlay
   display, streaming server API) doesn't exist yet. What follows is the old
   toggle-mode setup, kept for reference until it's rewritten:
   - Install `ydotool`, enable `ydotoold` as a user service (moot once
     `injector.py` is replaced by an overlay).
   - Copy both `systemd/vinowhisper-server.socket` and
     `systemd/vinowhisper-server.service` into `~/.config/systemd/user/`,
     then `systemctl --user enable --now vinowhisper-server.socket` — enable
     the **socket** unit, not the service; the service has no `[Install]`
     section on purpose, since it's only ever meant to be started by the
     socket.
   - Repoint the existing `Meta+H` KDE global shortcut (currently Ghostty's
     `new-window`) to the eventual entry point.

See the design doc linked above for the full feasibility-spike writeup,
benchmark numbers, and known open items before doing any of this for real.
