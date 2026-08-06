# CLAUDE.md

Guidance for Claude Code picking this project back up in a future session.

## What this is

NPU-accelerated local **live captioning** for a Fedora/KDE Plasma 6 (Wayland)
laptop with an Intel NPU (Wildcat Lake/Panther Lake, 16 TOPS). Uses OpenVINO
GenAI's `WhisperPipeline` running on-device (`device="NPU"`). Captures system
audio (or the mic) continuously, transcribes overlapping windows, and prints
captions to the terminal.

Originally scoped as toggle-mode voice typing (record, transcribe, inject text
via `ydotool`), pivoted 2026-08-03 to live captioning. The toggle-mode code is
gone. Anything still describing `injector.py` or `toggle.py` is stale.

**Canonical design doc, read this first:**
[wildcat-lake-linux/input/f5-voice-typing.md](https://github.com/karanshukla/wildcat-lake-linux/blob/main/input/f5-voice-typing.md)
in the sibling `~/Development/wildcat-lake-linux` repo. That doc has the full
rationale, the live-system facts confirmed before any code was written here,
and the benchmark tables. This file is a working-context summary, not a
replacement. If the two disagree, the design doc is the one kept current with
actual investigation. Update it when things here diverge.

## Status

The full loop is built and has run against real content, compared side by side
against YouTube's own generated captions as ground truth.

Confirmed working:
- NPU (`Intel(R) AI Boost`) doing real inference, not a silent CPU fallback.
  Cross-checked: the same non-stateful export fails outright on `device="CPU"`
  with a `beam_idx` port error, so NPU succeeding is a distinct code path.
- whisper-small.en on NPU at ~1.19s per 30s-window `generate()` call, correct
  transcription, first streamed token at ~0.204s and a readable first sentence
  by ~0.32s.
- Continuous capture off a sink monitor, cross-window stitching, and captions
  on screen.

Reported problems, and where each one stands after the 2026-08-06 review:

| Problem | Status |
|---|---|
| Laggy captions | Root-caused to window size driving decode length. Default window cut 29.5s to 12s, plus a minimum-hop guard. Not yet measured on hardware. |
| Incorrect captions | Two real stitching bugs fixed (see Bugs found below). Wording drift across cycles is inherent and only partly fixable. |
| Nothing works while muted | Not a bug in this code. A sink monitor is post-mute, so the samples really are zero. Mitigation is `--target` onto an app's own playback stream. Unverified on hardware. |

Not built: the TUI (captions go to stdout today), and the KDE `Meta+H`
shortcut still points at Ghostty's `new-window`.

## Where this is heading

A small TUI you pin on top of other windows, showing captions plus live
monitoring indicators. Two things follow from that, and both are already
accounted for:

- **The loop emits events, it does not print.** `caption.caption_events` is a
  generator of `events.Ready` / `Cycle` / `Silence` / `Stopped`.
  `TerminalRenderer` is one consumer, `session.SessionWriter` is another, and
  the TUI is a third. Do not add prints to the loop; add fields to the events.
- **Every stat worth showing is already on the events.** `Cycle` carries
  index, captured_s, window_s, hop_s, rms, gain, first_piece_s, total_s, the
  raw transcript, and confirmed/pending word lists. `Silence` carries elapsed
  time, rms, and the sink's mute state. That covers a level meter, a
  cycle-time readout, a pending-words indicator (the visible cost of
  LocalAgreement-2), and a "no signal, and here is why" state.

Practical notes for whoever builds it:

- **Pinning is a KWin job, not an app job.** A terminal running the TUI gets
  pinned with a KWin window rule (Keep Above, Skip Taskbar, no titlebar), same
  as any other window. Do not go looking for a layer-shell surface unless the
  TUI turns into a real GUI overlay.
- **The loop is synchronous and blocks for seconds at a time.** A TUI needs
  its own thread or an async wrapper, or the UI freezes for a whole decode.
  Run `caption_events` on a worker thread and hand events to the UI through a
  queue.
- **`Silence` fires roughly twice a second** so an indicator can count up
  live. `TerminalRenderer` throttles it to one message per silent stretch; a
  TUI should not.

## Architecture

```
vinowhisper/
  config.py       paths, ports, window sizing, levels, timeouts
  audio.py        RingBuffer (fixed capacity, thread-safe), rms(), normalize()
  recorder.py     Recorder, pw-record subprocess feeding the ring buffer
  client.py       TranscriptionClient, streaming HTTP client
  server.py       Flask, loopback-only (127.0.0.1:8099), socket-activated + self-idle-exit
  transcriber.py  WhisperTranscriber, wraps WhisperPipeline, serialized by a lock
  stitch.py       Stitcher, LocalAgreement-2 merge of overlapping transcripts
  events.py       what the loop emits instead of printing
  caption.py      caption_events() + TerminalRenderer + CLI (vinowhisper-caption)
  session.py      --record writer, and reading a session back
  replay.py       vinowhisper-replay, --restitch (offline) and --sweep (needs NPU)
  doctor.py       vinowhisper-doctor, environment and live level checks
```

**Two processes, not one script.** NPU model load takes 10-30s, so something
has to hold the loaded model across sessions. Loopback HTTP for simplicity,
not for security. Nothing here is exposed to the network or to another user. A
Unix domain socket would be a marginally more contained swap but is not a
meaningful improvement at this trust boundary.

**Socket-activated with self-idle-exit, not an always-on daemon.** Deliberate,
discussed explicitly with the user, worth preserving the reasoning: the socket
unit owns the port at boot with no process running, systemd spawns the service
on first connection (that is when the NPU load cost happens), and the service
self-exits after `IDLE_TIMEOUT_S` of no requests. This is the systemd-native
equivalent of serverless scale-to-zero, and the user explicitly wanted it
named as such since they come from a web-arch background. The alternative they
initially proposed (evict to zram) does not apply: zram-backed swap is kernel
memory-pressure driven, not idle-time driven, and ~500MB on a 16GB machine
likely never generates enough pressure to be reclaimed passively.

**The loop is synchronous and self-pacing.** No hop timer, never more than one
request in flight, so the hop is whatever the last cycle took. Combined with
LocalAgreement-2 needing two cycles to agree, captions trail the audio by
roughly 2x cycle time. This is the single most important fact for reasoning
about latency here.

## Bugs found in the 2026-08-06 review

All fixed, all verified against a stubbed pipeline. None had been caught on
hardware because each needs a specific input to show up.

1. **`UnicodeDecodeError` on any multi-byte character.** The client read the
   response with `iter_content(chunk_size=1)` and called `.decode("utf-8")`
   per chunk, so every multi-byte character was split into single bytes by
   construction. Whisper emits curly quotes, em dashes, and ellipses
   routinely. Reproduced against a real socket, then fixed with an incremental
   decoder. This crashed the caption session outright, it did not degrade.
2. **A one-word spurious match could reprint the entire transcript.**
   `_new_candidate_tail` picked the furthest-reaching match block and _then_
   checked its size, so a size-1 block near the end won the reach comparison,
   failed the size check, and fell through to "treat everything as new." Now
   filters blocks by size before taking the furthest reach.
3. **A dead `pw-record` was invisible.** The read loop just ended, the buffer
   froze, and the caption loop happily re-transcribed the same stale window
   forever. `Recorder.check_alive()` now raises with the exit status and argv.
4. **Busy-spin on an empty buffer.** `if samples.size == 0: continue` burned a
   core at startup until the first audio arrived.
5. **~38MB/s of pointless memcpy.** The buffer did
   `np.concatenate([buf, chunk])[-cap:]` on every 100ms read, copying the full
   1.9MB window twice per chunk, competing with the NPU for memory bandwidth.
   Replaced with a preallocated ring buffer.
6. **The idle watchdog could `os._exit(0)` mid-transcription.** Now tracks
   in-flight requests.
7. Smaller ones: unbounded growth of the confirmed-word list, a read-only
   array handed to the pipeline from `np.frombuffer`, no request timeouts, no
   validation of body length or window duration, and `soundfile` still listed
   as a dependency with nothing importing it.

## Known gotchas

- **NPU static-pipeline requirement, three real bugs found getting there.**
  Full root-cause writeups in the design doc. Summary:
  1. **Python 3.14 is unusable for model export.** It made `functools.partial`
     a descriptor, breaking `optimum`'s
     `NORMALIZED_CONFIG_CLASS = SomeConfig.with_args(...)` class-attribute
     idiom (`self` gets auto-bound as an extra positional arg).
     Version-independent root cause, not a package-pairing issue.
     `pyproject.toml` caps `requires-python` at `<3.14`.
  2. **NPU needs `--disable-stateful` on export** for the separate
     `decoder_with_past` KV-cache submodel the static pipeline requires.
     Matches [openvinotoolkit/openvino.genai#1728](https://github.com/openvinotoolkit/openvino.genai/issues/1728).
  3. **Stable `openvino_genai` (2026.2.1) cannot parse the resulting graph.**
     Its NPU pattern-matcher does not recognize the current export's SDPA
     attention-mask node shape. Needs the nightly wheel index until a stable
     release catches up. This is an ongoing dependency risk, re-check on each
     new stable OpenVINO release.
- **Two generation-config levers look like they would help and do not.** Both
  confirmed dead 2026-08-03. `no_repeat_ngram_size` is a no-op for Whisper:
  `pipeline_static.cpp`, `whisper.cpp`, and `logit_processor.cpp(.hpp)` have
  zero references to "ngram" or "repeat", so the field exists on
  `WhisperGenerationConfig` but Whisper's decode loop never reads it.
  `initial_prompt` is hard-blocked on NPU with an explicit `RuntimeError` at
  `pipeline_static.cpp:1147`. Runaway repeats are cleaned up client-side in
  `stitch.collapse_repeats`, at character level rather than by word, because
  the degenerate case has no whitespace to split on ("youyouyouyou...").
- **Model size is settled: whisper-small.en.** base.en (2.6x faster) and
  tiny.en (3.8x faster) both introduce real transcription errors. INT8 on
  small.en is free accuracy-wise but only buys ~10%, since the bottleneck is
  fixed compute over the 30s window, not weight memory bandwidth.
- **Capturing system audio needs `stream.capture.sink = true`.** Plain
  `pw-record` auto-connects to the default source (the mic), and
  `--target <sink>.monitor` alone is silently overridden back to the mic by
  WirePlumber's default policy for Capture-role streams.
- **Sink monitors appear to be post-volume on this machine.** That is what
  makes mute fatal and quiet audio bad. PipeWire's default for
  `monitor.channel-volumes` is `false`, so something local is likely turning
  it on, plausibly the `effect_input.bass_eq` filter chain. Verify with
  `pw-cli enum-params $(pactl get-default-sink) Props` before doing anything
  more elaborate.
- **Known upstream NPU rough edges** (as of 2026-07-30, not re-verified
  against the current nightly): open `openvino.genai` issues report a
  Whisper-turbo model hanging on NPU and unclean pipeline shutdown on NPU.
  Neither hit yet, but live captioning holds the pipeline resident and calls
  it continuously, which is a meaningfully different usage pattern from
  one-shot benchmarking.
- **Early-silicon NPU driver risk.** This laptop (Wildcat Lake, stepping A0)
  has a history of NPU driver/tooling rough edges. See the AuthFace NPU work
  in `wildcat-lake-linux` (`face-unlock-authface/npu-openvino-backend.md`) for
  what that looked like there: hand-patched driver, missing compiler libs in
  Fedora's package.
- **Socket-activation fd handoff, still untested.** `server.py`'s
  `_systemd_socket_fd()` plus `run_simple(..., fd=fd)` assumes werkzeug's
  `fd=` kwarg wraps a systemd-passed socket as documented. If it does not, the
  fallback is binding normally and losing idle-unload, not a hard failure.
  Confirm rather than assume.

## Remaining questions

Ordered by what would most change the design.

1. **Is the sink monitor actually post-mute, or is something else silencing
   it?** Everything in the mute mitigation rests on this. `vinowhisper-doctor`
   answers it: run it once with audio playing and once muted, and compare the
   sink monitor's level against a playing app's.
2. **What is the real per-cycle time at a 12s window on dense speech?** Record
   a session, then `vinowhisper-replay --sweep 8,12,16,20`. If 12s is still
   multiple seconds, the next lever is trimming confirmed audio out of the
   buffer rather than shrinking the window further. That needs
   `return_timestamps`, and nobody has checked whether the NPU static pipeline
   supports it.
3. **Does the pipeline stay healthy under sustained continuous use?** Every
   number so far comes from one-shot benchmarks. A long `--record` session is
   the cheapest way to find out, since it leaves evidence either way.
4. **Which TUI library?** Textual is the obvious pick and would be the first
   runtime dependency that is not already needed for the model. A plain ANSI
   status bar over the existing renderer would cost nothing and might be
   enough. Worth deciding before writing UI code.

## Conventions

- No test suite. This is a personal single-machine tool, not a distributed
  package. Keep it that way unless it grows a reason not to. Throwaway
  verification scripts against a stubbed pipeline are fine and were used for
  the 2026-08-06 review, they just do not live in the repo.
- Keep `wildcat-lake-linux/input/f5-voice-typing.md` in sync with real
  decisions made here. That repo is the durable investigation record, this
  repo is just the code.
