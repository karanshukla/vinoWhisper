# CLAUDE.md

Guidance for Claude Code picking this project back up in a future session.

## What this is

NPU-accelerated local **live captioning** for a Fedora/KDE Plasma 6 (Wayland)
laptop with an Intel NPU (Wildcat Lake/Panther Lake, 16 TOPS). Uses OpenVINO
GenAI's `WhisperPipeline` running on-device (`device="NPU"`), triggered by
the laptop's dictation key (physically emits `Meta+H`). Originally scoped as
toggle-mode voice typing (record, transcribe, inject text via `ydotool`);
pivoted 2026-08-03 to continuous live captioning displayed on-screen instead
— same transcription backend and hardware story, different consumer of the
output. See Status below for exactly what's confirmed vs. still unbuilt.

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

**Transcription backend confirmed working end to end (2026-08-03). Live
captioning consumer of that backend not built yet.** What's actually been
verified, standalone, isolated from any KDE/hotkey wiring:
- `openvino`/`openvino-genai` installed and working — NPU (`Intel(R) AI
  Boost`) confirmed enumerating and doing real inference, not a silent CPU
  fallback (cross-checked: same model loaded with `device="CPU"` fails
  outright, since the export is NPU-static-pipeline-specific).
- whisper-small.en converted to OpenVINO IR and benchmarked on NPU: ~1.19s
  steady-state per 30s-window `generate()` call, correct transcription.
  base.en/tiny.en are faster (2.6x/3.8x) but both introduce real
  transcription errors — small.en is a settled choice, not a default.
- Token-streaming callback tested: ~0.204s to first token, full first
  sentence readable by ~0.32s. This is the number to design the
  live-captioning UX around, not the 1.19s full-call time.
- Three real bugs hit and fixed along the way (Python 3.14 incompatible with
  `optimum`'s export code, NPU needs `--disable-stateful` export, stable
  `openvino_genai` can't parse the export graph and needs a nightly build).
  Full root-cause writeups in the design doc, summaries in Known gotchas
  below.

What's still **not** built or verified:
- No code in `vinowhisper/` has been run as an actual live-captioning
  consumer (continuous capture, overlay display, streaming server API) —
  the verification above tested the transcription layer directly via
  throwaway benchmark scripts, not through `recorder.py`/`server.py`/
  `injector.py`, which still reflect the old toggle-mode design and need a
  fresh architecture pass (see Architecture below).
- `ydotool` not installed/tested — moot for captioning anyway, since output
  is now a display overlay, not text injection. Keep only if ydotool ends up
  needed for some other interaction.
- The KDE `Meta+H` shortcut still points at Ghostty's `new-window`, not this.

Do not assume the toggle-mode architecture below still applies uncritically
— it's what's in the repo today, not the target design. Test bottom-up per
the verification order below before wiring any hotkey.

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
start, press again to stop+transcribe+inject) was the original design to get
something working first.

**Superseded by the live-captioning pivot (2026-08-03).** The file layout
above (`recorder.py`'s toggle pidfile, `injector.py`'s `ydotool type`,
`server.py`'s one-shot `POST /transcribe`) is still what's in the repo today,
but is the *old* toggle-mode design, not the target architecture. Still
toggle-triggered (same `Meta+H` press to start/stop a session), but the
session itself needs to become: continuous chunked capture instead of
one-shot WAV, an on-screen caption overlay instead of `ydotool` text
injection, and a streaming-aware server API instead of one-shot POST/response
— since Whisper's fixed 30s-window architecture means captioning needs a
sliding window with overlap/dedup, not a single record-then-transcribe call.
See the design doc's "Planned architecture (live captioning, not yet built)"
section for the concrete shape. This hasn't been implemented yet — treat the
current `vinowhisper/*.py` contents as due for a rewrite, not as working
reference code.

## Known gotchas to check before/while implementing

- **NPU static-pipeline requirement — confirmed, three real bugs found
  getting there (2026-08-03).** Full root-cause writeups in the design doc
  (`wildcat-lake-linux/input/f5-voice-typing.md`'s "Feasibility spike"
  section); summary:
  1. **Python 3.14 is unusable for model export.** It made `functools.partial`
     a descriptor, breaking `optimum`'s `NORMALIZED_CONFIG_CLASS =
     SomeConfig.with_args(...)` class-attribute idiom (`self` gets
     auto-bound as an extra positional arg). Version-independent root cause,
     not a package-pairing issue — use Python 3.13 (`pyproject.toml` now
     caps `requires-python` at `<3.14`).
  2. **NPU needs `--disable-stateful` on export** for the separate
     `decoder_with_past` KV-cache submodel the static pipeline requires
     (`scripts/convert_model.sh` does this now). Matches
     [openvinotoolkit/openvino.genai#1728](https://github.com/openvinotoolkit/openvino.genai/issues/1728).
  3. **Stable `openvino_genai` (2026.2.1) can't parse the resulting graph** —
     its NPU pattern-matcher doesn't recognize the current export's SDPA
     attention-mask node shape. Needs the nightly wheel index
     (`https://storage.openvinotoolkit.org/simple/wheels/nightly`, `--pre`)
     until a stable release catches up — `pyproject.toml` pins this and
     documents the install command. This is an ongoing dependency risk, not
     a one-time fix; re-check on each new stable OpenVINO release.

  `transcriber.py`'s `WhisperPipeline(...)` call now passes
  `STATIC_PIPELINE=True` for NPU. Confirmed correct transcription end to end
  through the actual `WhisperTranscriber` class, not just a throwaway script.
- **Model size is settled: whisper-small.en, not swappable without a real
  accuracy cost.** Benchmarked base.en (2.6x faster) and tiny.en (3.8x
  faster) against small.en on this NPU — both introduce real transcription
  errors (garbled words, dropped/mis-heard terms). INT8 quantization on
  small.en is free (zero observed accuracy loss) but only buys ~10%, since
  the bottleneck is fixed compute over Whisper's 30s window, not weight
  memory bandwidth. Full benchmark table in the design doc.
- **Perceived latency ≠ total call latency.** A single `generate()` call
  takes ~1.19s (fixed cost of Whisper's 30s-window encoder pass, doesn't
  shrink with less actual speech in the window). But its `streamer`
  callback delivers first text at ~0.204s and a full sentence by ~0.32s —
  design the live-captioning UX around the streaming number, not the total
  call time. One caveat: the streamer callback only supports audio under
  30s; trimming a clip to fit that caused a hallucinated repeated phrase at
  the very end in testing — normal Whisper behavior on audio cut
  mid-thought, worth expecting if a sliding-window implementation trims
  near a 30s boundary.
- **Known upstream NPU rough edges** (as of 2026-07-30, not re-verified
  against the current nightly build or under sustained/repeated use): open
  `openvino.genai` GitHub issues report a Whisper-turbo model hanging on
  NPU and unclean pipeline shutdown on NPU. Not hit either yet in short
  one-shot benchmark testing — worth watching once live captioning holds
  the pipeline resident and calls it continuously over a real session, a
  meaningfully different usage pattern than tested so far.
- **Early-silicon NPU driver risk**: this specific laptop (Wildcat Lake,
  stepping A0) has a history of NPU driver/tooling rough edges — see the
  AuthFace NPU work in the `wildcat-lake-linux` repo
  (`face-unlock-authface/npu-openvino-backend.md`) for what that looked like
  there (hand-patched driver, missing compiler libs in Fedora's package).
  Worth checking whether the same OpenVINO/driver stack version used there
  is what gets used here too, for consistency.
- **`ydotool`/uinput permissions — likely moot now.** Live captioning
  displays text on an overlay, it doesn't inject it at the cursor, so
  `injector.py`'s `ydotool type` approach is being replaced, not fixed. Keep
  this note only in case some other interaction ends up needing text
  injection later; otherwise this whole concern goes away with the pivot.
  (Original note, still true if ever needed: `/dev/uinput` exists and the
  user is already in the `input` group, but this was never actually
  confirmed sufficient — the device has a `+` ACL flag suggesting something
  else may already be granting access.)
- **`recorder.py`'s stop() timing — applies to whatever replaces it.**
  `pw-record` needs a moment to flush and close the WAV header after
  SIGINT; the toggle-mode code polled `/proc/<pid>` before reading the file
  back, untested against a real recording. Whatever continuous-capture
  mechanism replaces this for live captioning will have its own version of
  "did the audio actually finish writing" to get right — don't assume a
  rolling-buffer approach sidesteps this class of bug for free.
- **Socket-activation fd handoff, untested**: `server.py`'s
  `_systemd_socket_fd()` + `run_simple(..., fd=fd)` assumes werkzeug's
  `fd=` kwarg behaves as documented for wrapping a systemd-passed socket.
  Verify this actually works against the installed werkzeug version before
  trusting it — if it doesn't, the fallback is binding normally and losing
  the idle-unload behavior, not a hard failure, but confirm rather than
  assume.

## Verification order (don't skip ahead)

Steps 1 and 5 below are **done** (2026-08-03, via throwaway benchmark
scripts, not yet through the real `vinowhisper/*.py` modules apart from
`transcriber.py`). Steps 2-4 describe the old toggle-mode design and need
re-scoping once the live-captioning architecture (recorder/overlay/server
rewrite) actually exists — keeping them here as the discipline to repeat
("standalone correctness, then integration, then hotkey last"), not as
literal remaining steps.

1. ~~Convert the model, test `WhisperTranscriber` standalone against a
   pre-recorded test WAV — confirm correct transcription and measure
   latency before anything else.~~ Done: ~1.19s/30s-window, correct
   transcription, streaming confirmed at ~0.204s first-token.
2. Test whatever overlay/display mechanism replaces `ydotool` alone,
   independent of transcription — same reasoning as before (isolate
   display plumbing from model correctness), different mechanism.
3. Wire continuous capture + streaming transcription + overlay together,
   test the full loop manually before touching any KDE shortcut.
4. Only then repoint the KDE `Meta+H` shortcut from Ghostty to whatever the
   new entry point is (successor to `vinowhisper-toggle`).
5. ~~Confirm the NPU is actually doing inference, not silently falling back
   to CPU.~~ Done: same non-stateful export fails outright on `device="CPU"`
   (`beam_idx` port error), so NPU succeeding is a real, distinct code path,
   not a fallback. Worth re-confirming once live captioning holds the
   pipeline resident under sustained use, a different usage pattern than
   the one-shot benchmark tested.

## Conventions

- No test suite yet — this is a personal single-machine tool, not a
  distributed package. Keep it that way unless it grows a reason not to.
- Keep `wildcat-lake-linux/input/f5-voice-typing.md` in sync with real
  decisions made here (model size actually settled on, whether toggle mode
  survived contact with reality, etc.) — that repo is the durable
  investigation record, this repo is just the code.
