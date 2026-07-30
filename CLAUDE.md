# CLAUDE.md

Guidance for Claude Code picking this project back up in a future session.

## What this is

NPU-accelerated local voice typing for a Fedora/KDE Plasma 6 (Wayland) laptop
with an Intel NPU (Wildcat Lake/Panther Lake, 16 TOPS). Uses OpenVINO GenAI's
`WhisperPipeline` running on-device (`device="NPU"`), triggered by the
laptop's dictation key (physically emits `Meta+H`), transcribed text injected
at the cursor via `ydotool`.

Named after [vinoAuthFace](https://github.com/karanshukla/vinoAuthFace)
(same author, same "OpenVINO doing the NPU work" theme, different feature —
that one's face-unlock, this one's speech-to-text).

**Canonical design doc, read this first:**
[wildcat-lake-linux/input/f5-voice-typing.md](https://github.com/karanshukla/wildcat-lake-linux/blob/main/input/f5-voice-typing.md)
in the sibling `~/Development/wildcat-lake-linux` repo. That doc has the full
rationale, the live-system facts that were confirmed before writing any code
here, and the verification plan. This CLAUDE.md is a working-context summary,
not a replacement for it — if the two ever disagree, the design doc is the
one that's been kept current with actual investigation, update it too when
things here diverge from it.

## Status

**Scaffolding only. Nothing has been run.** File structure and class shapes
exist; none of the following has happened yet:
- `openvino` / `openvino-genai` / `ydotool` are not installed.
- No Whisper model has been converted to OpenVINO IR.
- No code in `vinowhisper/` has been executed even once.
- The KDE `Meta+H` shortcut still points at Ghostty's `new-window`, not this.

Do not assume anything here works. Test bottom-up per the verification order
below, don't wire the hotkey first.

## Why this exists / why not just use an existing project

The obvious existing project, `whisper-npu-server`, is a dead end: the
GitHub repo pointed to as "the maintained fork" (`mecattaf/whisper-npu-server`)
404s — it doesn't exist. Only a container image and some HF model repos are
reachable via a third fork's README, and **none of that ecosystem (original
or forks) documents the actual text-injection mechanism** — it was always a
private, unpublished wrapper script on the original author's machine, and
it's all sway/niri-native anyway (no KDE/Plasma integration exists anywhere
in that lineage). So the "persistent local server" idea is worth keeping,
but everything else here is a from-scratch build, not a port.

## Architecture

```
vinowhisper/
  config.py       paths, ports, sample rate, idle timeout — single source of truth for both server and client
  transcriber.py  WhisperTranscriber: wraps openvino_genai.WhisperPipeline(model_dir, device="NPU")
  server.py       Flask app, loopback-only (127.0.0.1:8099), socket-activated + self-idle-exit
  recorder.py     Recorder: toggle start/stop of `pw-record`, state tracked via a pidfile
  injector.py     TextInjector: shells out to `ydotool type`
  toggle.py       entry point bound to the Meta+H KDE shortcut — ties recorder+server+injector together
scripts/convert_model.sh   optimum-cli export wrapper (whisper-small.en -> OpenVINO IR)
systemd/vinowhisper-server.socket    systemd owns the listening port, starts the service lazily
systemd/vinowhisper-server.service   socket-activated (no [Install]), self-exits after idle
```

**Two-process design (server + toggle client), not one script.** The reason:
NPU model load takes ~10-30s, and `toggle.py` runs as a short-lived process
per keypress, so something has to hold the loaded model across presses. The
server is loopback HTTP for simplicity, not for any security reason —
nothing here is exposed to the network or to any other user. A Unix domain
socket would be a marginally "more contained" swap (filesystem-permission-
gated instead of port-based) but isn't a meaningful security improvement at
this trust boundary; not worth doing unless it starts to itch.

**Socket-activated with self-idle-exit, not an always-on daemon.** Deliberate
choice, discussed explicitly with the user and worth preserving the reasoning:
`vinowhisper-server.socket` owns the port at boot (no model loaded, no
process running); systemd spawns `vinowhisper-server.service` lazily on the
first connection (that's when the NPU load cost happens); `server.py`'s
`_idle_watchdog` self-exits the process after `config.IDLE_TIMEOUT_S` (30
min) of no requests, and the next dictation press respawns it via the socket.
This is the systemd-native equivalent of a serverless/Lambda scale-to-zero
pattern — the user explicitly framed it that way and wanted it named as such
in the docs, since they're coming from web-arch background, not kernel/Linux
systems background. The alternative they initially proposed (evict to zram)
doesn't actually apply here: zram-backed swap is kernel memory-pressure
driven, not idle-time driven, and a ~500MB process on a 16GB machine likely
never generates enough pressure to get swapped out passively — hence
choosing deterministic self-exit over hoping the kernel reclaims it.

**Toggle mode, not hold-to-record.** KDE global shortcuts fire on key press,
not press-hold-release, so true hold-to-record would mean bypassing KDE
shortcuts and reading raw evdev events directly instead. Toggle (press to
start, press again to stop+transcribe+inject) was chosen to get something
working first. Revisit only if the UX genuinely bothers him.

## Known gotchas to check before/while implementing

- **NPU static-pipeline requirement**: OpenVINO GenAI's NPU device path
  needs a static-shape pipeline. `transcriber.py`'s `WhisperPipeline(...)`
  call may need an explicit property for this — confirm the exact kwarg
  name against whatever `openvino_genai` version actually gets installed,
  the API may have moved since this was last checked (2026-07-30).
- **Known upstream NPU rough edges** (as of the same date): open
  `openvino.genai` GitHub issues report a Whisper-turbo model hanging on
  NPU and unclean pipeline shutdown on NPU. Don't be surprised if
  `whisper-small.en` (the default here) needs to be swapped for something
  else, or if clean process exit needs extra handling.
- **Early-silicon NPU driver risk**: this specific laptop (Wildcat Lake,
  stepping A0) has a history of NPU driver/tooling rough edges — see the
  AuthFace NPU work in the `wildcat-lake-linux` repo
  (`face-unlock-authface/npu-openvino-backend.md`) for what that looked like
  there (hand-patched driver, missing compiler libs in Fedora's package).
  Worth checking whether the same OpenVINO/driver stack version used there
  is what gets used here too, for consistency.
- **`ydotool`/uinput permissions**: `/dev/uinput` exists and the user is
  already in the `input` group, but this was never actually confirmed
  sufficient — the device has a `+` ACL flag suggesting something else may
  already be granting access. Check `getfacl /dev/uinput` and just try
  `ydotool type` early rather than assuming.
- **`recorder.py`'s stop() timing**: `pw-record` needs a moment to flush and
  close the WAV header after SIGINT; the code polls `/proc/<pid>` before
  reading the file back, but this hasn't been tested against a real
  recording yet — verify the WAV isn't truncated/corrupt on first real test.
- **Socket-activation fd handoff, untested**: `server.py`'s
  `_systemd_socket_fd()` + `run_simple(..., fd=fd)` assumes werkzeug's
  `fd=` kwarg behaves as documented for wrapping a systemd-passed socket.
  Verify this actually works against the installed werkzeug version before
  trusting it — if it doesn't, the fallback is binding normally and losing
  the idle-unload behavior, not a hard failure, but confirm rather than
  assume.

## Verification order (don't skip ahead)

1. Convert the model (`scripts/convert_model.sh`), then test
   `WhisperTranscriber` standalone against a pre-recorded test WAV — confirm
   correct transcription and measure latency before anything else.
2. Test `ydotool type` alone (e.g. typing into Kate) to confirm uinput
   permissions work end to end, independent of transcription.
3. Wire `toggle.py` + `server.py` together and test the full
   record-stop-transcribe-inject loop manually (run `vinowhisper-toggle`
   from a terminal twice), before touching any KDE shortcut.
4. Only then repoint the KDE `Meta+H` shortcut from Ghostty to
   `vinowhisper-toggle`.
5. Confirm the NPU is actually doing inference, not silently falling back
   to CPU — reuse whatever NPU-utilization check worked for AuthFace.

## Conventions

- No test suite yet — this is a personal single-machine tool, not a
  distributed package. Keep it that way unless it grows a reason not to.
- Keep `wildcat-lake-linux/input/f5-voice-typing.md` in sync with real
  decisions made here (model size actually settled on, whether toggle mode
  survived contact with reality, etc.) — that repo is the durable
  investigation record, this repo is just the code.
