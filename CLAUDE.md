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
| Model download had no integrity check | Fixed 2026-09-04 (issue #9): sha256 pins on the exported IR, checked by the wizard, the convert script and the doctor. Found the transformers 5.4.0 export break below on the way. |
| Nothing works while muted | **Misdiagnosed.** Measured 2026-08-07: muted, with audio playing, the sink monitor reads 0.08578 against the app's 0.08781. Mute does not silence it. The likely real cause is muting the *app* rather than the system, which nothing can capture around. See the gotcha below. |

Not built: the KDE `Meta+H` shortcut still points at Ghostty's `new-window`.

**2026-08-13: distribution and the "other" aspects.** The tool now assumes
less about the machine it runs on: automatic NPU>GPU>CPU selection with loud
warnings anywhere below NPU, a PulseAudio capture fallback, a distro table
covering eight package families, `vinowhisper-setup` (guided install,
generated systemd units — the checked-in unit used to hardcode
`~/Development/vinoWhisper`), `scripts/install.sh`, a test suite, and CI.
**None of the new hardware paths have run on hardware**: the GPU and CPU
fallbacks, the stateful export, and every non-Fedora package name are
unverified. Treat them as best-effort until someone reports otherwise.

## The UI, and why it is Rich and not Textual

Scope, set explicitly by the user 2026-08-06: turn on and go. No complex
interactions, no app. Setup can be a wizard or scripts later if it needs any.
Do not grow this into a full TUI application without being asked.

Rich, not Textual, and the reason is structural rather than taste:

- **The transcript is append-only.** LocalAgreement-2 means a word is never
  revised once printed, so it belongs in the terminal's own scrollback, where
  it survives quitting and where native selection and search still work. Only
  the status line needs to redraw, which is exactly `rich.live.Live`.
- **Textual owns the event loop and (by default) the alternate screen.** That
  would put the transcript in an in-app widget: gone on quit, not pipeable,
  and scrolling/selection reimplemented rather than inherited.
- **There is nothing to interact with**, so Textual's widgets, focus and input
  handling would all go unused.

Reconsider only if it grows real interaction (picking a `--target` from the UI,
a settings pane, searchable history). Because renderers are just event
consumers, that switch is a swap, not a rewrite.

### How the UI is wired

- **The loop emits events, it does not print.** `caption.caption_events` is a
  generator of `events.Ready` / `Cycle` / `Silence` / `Stopped`. `ui.RichRenderer`
  is one consumer, `caption.TerminalRenderer` another, `session.SessionWriter`
  a third. Do not add prints to the loop; add fields to the events.
- **Every stat on the bar comes off an event.** `Cycle` carries index,
  captured_s, window_s, hop_s, rms, gain, first_piece_s, total_s, the raw
  transcript, and confirmed/pending word lists. `Silence` carries elapsed
  time, rms, and the sink's mute state.
- **Pending words are shown dimmed, on purpose.** They are the visible cost of
  the two-cycle commit policy. Surfacing them turns "the captions are frozen"
  into "it is still deciding" without printing anything that might be wrong.
  This is the cheapest available fix for *perceived* lag.
- **Punctuation is not inferred, it is Whisper's.** whisper-small.en emits
  punctuation and capitalization already, so confirmed words arrive with it
  attached. Paragraphs *are* inferred, from signals already on the event
  stream: a `Silence` past `_PARAGRAPH_SILENCE_S` is a real pause, and past
  `_PARAGRAPH_MIN_WORDS` a sentence end breaks so continuous speech doesn't run
  one paragraph forever. Every break is scheduled and only applied when the
  next word actually arrives — scrollback is append-only, so a session must
  never end on a stray blank line.
- **The gutter is a hanging indent, and it is width-gated.** `[MM:SS]` on the
  first line of each paragraph, blank on continuations, elapsed rather than
  wall clock so it agrees with the status bar's clock. Suppressed entirely
  below 60 columns, where those 8 columns are worth more as text.
- **Partial lines cannot go to scrollback.** Live redraws its region directly
  below whatever was last printed and expects to start at column 0, so the
  line being built lives inside the Live region and only moves up once full.
  `ui.RichRenderer._add_words` does its own word wrapping so no word is ever
  split by the terminal.
- **`Live.update()` needs `refresh=True`.** Without it the new renderable is
  only picked up on the auto-refresh thread's next tick, so any redraw in
  between (notably the one Live does when the transcript scrolls) paints stale
  numbers. This was a real bug, caught in rendering tests, not theory.
- **Pinning is a KWin window rule**, not an app concern. Do not go looking for
  a layer-shell surface unless this becomes a real GUI overlay.
- **`--debug` and `--plain` fall back to `TerminalRenderer`**, and so does a
  non-TTY stdout, so piping to a file still works.

## Architecture

```
vinowhisper/
  config.py       paths, ports, window sizing, levels, timeouts, per-device model dirs
  audio.py        RingBuffer (fixed capacity, thread-safe), rms(), normalize()
  capture.py      which audio backend exists (pw-record/parec), argv, node enumeration
  recorder.py     Recorder, the capture subprocess feeding the ring buffer
  devices.py      OpenVINO device inventory, NPU>GPU>CPU selection, kernel-side preflight
  distro.py       /etc/os-release -> package names and install commands, per family
  client.py       TranscriptionClient, streaming HTTP client
  server.py       Flask, loopback-only (127.0.0.1:8099), socket-activated + self-idle-exit
  transcriber.py  WhisperTranscriber, wraps WhisperPipeline, serialized by a lock
  stitch.py       Stitcher, LocalAgreement-2 merge of overlapping transcripts
  integrity.py    sha256 pins for the model export, and what a mismatch means
  events.py       what the loop emits instead of printing
  caption.py      caption_events() + TerminalRenderer + CLI (vinowhisper-caption)
  ui.py           RichRenderer, the pinned status bar
  session.py      --record writer, and reading a session back
  replay.py       vinowhisper-replay, --restitch (offline) and --sweep (needs NPU)
  doctor.py       vinowhisper-doctor, environment checks + --json
  wizard.py       vinowhisper-setup, the guided install
tests/            pytest; no NPU, no audio server, no OpenVINO (see Conventions)
docs/             install, hardware, audio, latency, debugging, architecture
scripts/          install.sh (bootstrap), convert_model.sh (both exports),
                  update_digests.py (re-pin an export), completion
vinowhisper/model_digests.json   generated pins; do not hand-edit the hashes
.github/          CI, release, dependency canary, Bandit, templates
```

**The README is an index and a pitch, not the documentation.** Cut from 365
lines to 121 on 2026-08-31; the long-form sections live in `docs/` and the
README links to them. When something here changes, the prose to update is
almost always a file under `docs/`, not the README.

Rewritten 2026-09-04, at the user's request, to read as a project someone might
choose rather than as a table of contents: a one-line hook, the "why the NPU"
angle stated as a bias, a numbers table, five differentiators, a requirements
table, and an **Honest limits** section. It grew to ~200 lines doing that,
which is deliberate and not the 365-line regression. The index rule still
holds: nothing in it explains anything `docs/` explains, every long-form claim
links out, and **Honest limits** is load-bearing, since every benchmark in it
is n=1 on one laptop and it says so.

**It ships on PyPI now, so "is this file here?" has two answers.** From
2026-08-31, `pip install vinowhisper` works: the nightly pin was the only thing
preventing it (see the OpenVINO gotcha below), and its removal made the whole
dependency set resolve from PyPI. The consequence to keep in mind when writing
any error message: **a wheel install has no `scripts/`, no `docs/`, no
`systemd/` and no repo at all.** Seven messages named
`./scripts/convert_model.sh` and were wrong for every pip user until
`config.export_command()` was added; `wizard.install_completion()` had already
got this right ("completion script not found (installed from a wheel?)"), which
is the pattern to copy. Anything reached via `_repo_root()` needs the same
treatment. Releases upload through Trusted Publishing from `release.yml`, which
names the `pypi` environment, so renaming that file or that environment breaks
publishing until PyPI's publisher config is changed to match.

**Two device paths, two model exports, and this is the trap.** NPU needs the
`--disable-stateful` export; that same export cannot run on CPU at all
(`beam_idx` port error). So the CPU/GPU fallback needs a *second* export in a
second directory — `config.model_dir(kind)` decides which, and
`transcriber._check_export()` fails with the command to run rather than dying
inside the pipeline constructor. Device selection is `devices.select()`:
NPU > GPU > CPU for "auto", an explicit `--device` is refused rather than
downgraded, and anything below NPU carries warnings that surface in the server
journal, `/health`, the `Ready` event, the status bar (red border + `⚠`) and
the doctor.

**The NPU has a userspace half that device enumeration does not test.**
`devices.npu_preflight()` answers the kernel-side question; `npu_userspace()`
answers the other one, and the doctor runs it even when the NPU enumerated
fine. Two silent failures live there: no distro packages
`libopenvino_intel_npu_compiler.so` at all (Fedora's `intel-npu-driver` rpm
ships level-zero and stops), and a `dnf reinstall` of that rpm rewrites
`libze_intel_npu.so.1` back to the packaged backend, which still enumerates
`Intel(R) AI Boost` and then fails inside `compile_model()`. Both are read
straight off the libraries, which embed their own provenance
(`npu-linux-driver-ci-1.35.0.…` and the OpenVINO release the compiler was built
from). The 547KB `_loader` carries the same version as the 127MB compiler, so
that is the one scanned. This machine's hand-installed 1.35.0 libraries are
untracked by rpm, which is exactly why the reinstall case is worth checking
rather than assuming.

**Capture has two backends.** `capture.py` prefers `pw-record` and falls back
to `parec`; `record_argv()` is a pure function of (source, target, backend) so
the flags are testable with no audio server present. Per-application `--target`
is PipeWire-only and says so on PulseAudio rather than capturing the wrong
thing. `pactl` is used where present regardless of backend (pipewire-pulse
provides it), and PipeWire's `default.audio.sink` metadata is the fallback when
it isn't.

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

## Bug found 2026-08-07

**Punctuation flips broke the stitch anchor, same as capitalization once did.**
`_candidate_tail` normalized case but not punctuation, and Whisper re-decodes
the identical audio with drifting punctuation as the window boundary moves
through a sentence ("baseball." / "baseball," / "baseball"). Each flip breaks
the anchor mid-overlap, which is enough to fall below `_MIN_MATCH_WORDS` and
reprint an already-shown word. Demonstrated directly: for one drifted overlap
the old comparison returned `['baseball.', 'For', 'the', 'first', 'month,']`
against a correct `['For', 'the', 'first', 'month,']`. Fixed with `_norm`,
which strips punctuation for *comparison only*, never before printing. Note
`push` commits `candidate`'s words rather than `_pending`'s: when two decodes
agree modulo punctuation, the later saw more right-context.

## Bug found 2026-09-01

**A short boundary overlap below `_MIN_MATCH_WORDS` reprinted already-shown
words, worsening cycle over cycle.** Reported from a real ~4-minute session:
growing duplicate blocks like "do things" / "do things that make you" printed
as separate lines. Root cause: when wording drift left only a 1-2 word
overlap between the confirmed tail and the new decode (below the 3-word
anchor floor), `_candidate_tail` had no internal match to fall back on and
returned the whole cycle as "new," reprinting the words it shared with what
was already on screen. That floor exists to avoid locking onto an unrelated
*internal* occurrence of a common phrase (see the 2026-08-06 review), but a
match *at the boundary* has no such ambiguity — `curr` always starts inside a
window that overlaps what's confirmed, so a boundary match is trustworthy even
below the floor. Fixed with `_strip_confirmed_prefix`, a word-by-word boundary
check that runs only when the internal `SequenceMatcher` search comes back
empty. Reproduced and verified against a stubbed stitcher, not yet re-run on
hardware.

## transformers 5.4.0 breaks the NPU export, bisected 2026-09-04

**Export with `transformers<5.4`.** Found while generating digest pins for
issue #9, which needed a fresh export to pin, which is the only reason anyone
re-exported. `pip install optimum[openvino]` resolves transformers 5.5.4 today,
so a clean install produces a model that loads and then cannot run.

`WhisperPipeline(..., STATIC_PIPELINE=True)` builds, then `generate()` raises
`Port for tensor name cache_position was not found`
(`infer_request.cpp:191`). Bisected in a clean venv per version, with
optimum-intel 2.1.0, optimum 2.3.0, openvino 2026.3.1, openvino-genai 2026.3.1.0
and torch 2.13.0 all held fixed:

| transformers | `generate()` |
|---|---|
| 5.0.0, 5.2.0, 5.3.0 | ok, 0.71-0.96s |
| 5.4.0, 5.5.4 | fails |

Two controls rule out the rest of the stack: optimum-intel 2.1.0 with
transformers 5.0.0 works (so the optimum pair is fine), and the full 2026-08-03
package set re-run under openvino 2026.3.1 works (so the runtime is fine).

**The mechanism is a tensor name, which is why it is so easy to break.**
`cache_position` appears exactly once in each 5.3.0 decoder graph, on the output
port of `__module.model.model.decoder/aten::arange/Range`, and zero times in
the 5.4.0 graphs. It is neither a model input nor a model output in either
export, and the input/output signatures are otherwise identical, so the static
pipeline is resolving an *internal traced tensor* by name and 5.4.0 stopped
emitting that name. Patching `num_hidden_layers` back into 5.4.0's config.json
(the other visible config change) does not help, which is how that hypothesis
was ruled out.

Not fixed here: `pyproject.toml` does not pin `transformers<5.4`. It would
hold a package back for everyone, and it is a dependency-policy call rather
than a bug fix. The verified-working combination is optimum-intel 2.1.0 +
optimum 2.3.0 + transformers 5.3.0, so the pin is narrow if it is wanted.
Meanwhile the `known_bad` entry in `model_digests.json` carries a floor at
transformers 5.4.0, so `vinowhisper-setup` fails that step rather than handing
over a model that dies at the first transcription.

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
  3. **Stable `openvino_genai` 2026.2.1 could not parse the resulting graph,
     and 2026.3.1 can. Resolved 2026-08-31, the nightly pin is gone.** 2026.2.1's
     NPU pattern-matcher did not recognize the export's SDPA attention-mask node
     shape, which is why 2026-08-03 through 2026-08-31 pinned nightly wheels.
     Re-measured on hardware against the same export: stable 2026.3.1 builds the
     static pipeline in 2.0s and decodes, as do nightly 2026.4.0.dev20260805
     (the version that had been pinned) and 2026.5.0.dev20260831. The floor is
     now `openvino>=2026.3.1` from PyPI, and `[tool.uv.index]`,
     `[tool.uv.sources]` and `prerelease = "allow"` are all deleted.
  4. **`STATIC_PIPELINE=True` is what selects the NPU code path, and omitting
     it fails misleadingly.** Without it the NPU goes down the generic stateful
     path and raises `Stateful models without 'beam_idx' input are not
     supported in StatefulToStateless transformation`, which reads as a broken
     export and is not one. Cost an hour on 2026-08-31 while version-testing:
     three OpenVINO releases all "failed" identically until the control run
     showed the probe, not the wheels, was wrong.
- **Two generation-config levers look like they would help and do not.** Both
  confirmed dead 2026-08-03. `no_repeat_ngram_size` is a no-op for Whisper:
  `pipeline_static.cpp`, `whisper.cpp`, and `logit_processor.cpp(.hpp)` have
  zero references to "ngram" or "repeat", so the field exists on
  `WhisperGenerationConfig` but Whisper's decode loop never reads it.
  `initial_prompt` is hard-blocked on NPU with an explicit `RuntimeError` at
  `pipeline_static.cpp:1147`. Runaway repeats are cleaned up client-side in
  `stitch.collapse_repeats`, at character level rather than by word, because
  the degenerate case has no whitespace to split on ("youyouyouyou...").
- **The character-level collapse was never blind to whitespace, it is blind to
  normalization. Measured 2026-09-04.** The obvious reading of the above — that
  a space-separated repeat needs a word-level pass — is wrong: `"you "` is a
  repeating *character* unit exactly as `"you"` is, so `collapse_repeats`
  already folds `"you you you you"` and `"do things do things do things do
  things"`. What it cannot see is a loop whose reps differ in punctuation or
  capitalization (`"do things. do things, Do things"`), which is not an exotic
  input here — drifting punctuation on re-decoded audio is the documented
  cause of both the 2026-08-07 and 2026-09-01 anchor bugs. `collapse_word_repeats`
  closes that by comparing through `_norm`, the same normalization the anchor
  uses. It collapses *adjacent* runs only: a word recurring three times
  anywhere in a cycle is ordinary speech, and with an append-only transcript a
  false positive deletes a real word with no way to restore it. Both passes run
  on the raw cycle in `push`, not on the words about to be committed — cleaning
  only the commit would leave `_confirmed` holding a collapsed run while the
  next cycle's `curr` still holds the full one, which is the anchor mismatch
  that causes reprints in the first place.
- **The export is bit-reproducible on a fixed toolchain, and not across one.
  Measured 2026-09-04.** Two independent `optimum-cli export openvino` runs of
  whisper-small.en on the same machine and toolchain produced all 16 files
  byte-identical, which is the fact the whole digest-pinning design rests on;
  without it every user would get a mismatch and learn to ignore it. Across
  toolchains, the 2026.3.1/2.1.0/5.5.4 export differed from the
  2026.2.1/2.0.0/5.0.0 one in 9 of 16 files, including both decoder `.bin`
  weights. `openvino_encoder_model.bin` was identical across both, as were the
  tokenizer `.bin` files and three of the JSON configs. So a pin outlives about
  one toolchain, `integrity.py` reads the versions out of the export's own
  `rt_info` block to tell drift from tampering, and `scripts/update_digests.py`
  exists so re-pinning is a command rather than a paste.
- **Model size is settled: whisper-small.en.** base.en (2.6x faster) and
  tiny.en (3.8x faster) both introduce real transcription errors. INT8 on
  small.en is free accuracy-wise but only buys ~10%, since the bottleneck is
  fixed compute over the 30s window, not weight memory bandwidth.
- **Capturing system audio needs `stream.capture.sink = true`.** Plain
  `pw-record` auto-connects to the default source (the mic), and
  `--target <sink>.monitor` alone is silently overridden back to the mic by
  WirePlumber's default policy for Capture-role streams.
- **The sink monitor is pre-volume AND pre-mute. Measured 2026-08-07, and this
  repo previously claimed the opposite in seven places, including two messages
  printed to the user.** Three `vinowhisper-doctor` runs, each comparing the
  default sink's monitor against a playing Chrome stream as a control: 100%
  volume, 0.01419 vs 0.02040 (0.70); 20% volume, 0.05739 vs 0.06610 (0.87);
  **muted, 0.08578 vs 0.08781 (0.98)**. The ratio is the measurement, since
  the content differed between runs, and it moves with neither the slider nor
  the mute button. `monitor.channel-volumes` is unset and PipeWire defaults it
  to `false`, so the prop and the levels agree on the volume half.

  This invalidates the "most-reported problem" and every mitigation built on
  it. What is actually capable of silencing the capture: muting the *app*
  (the app then writes silence into its own stream and no tap exists upstream
  of that, so `--target` cannot help either), targeting
  `effect_output.bass_eq`, or nothing playing.

  Method note worth keeping: the first muted run looked like proof that mute
  kills the monitor, reading 0.00000 everywhere. Nothing was playing. Without
  an audible app stream as a control, a muted run measures nothing at all.
- **`effect_output.bass_eq` is the trap in `--list-targets`.** It is the
  obvious-looking pick and the worst one: it sits on the output side of the EQ
  chain, downstream of the volume control, and measured 0.00034 at 20% volume
  against the sink monitor's 0.05739. It is the one node here that really is
  post-volume. Aim `--target` at an actual application.
- **Known upstream NPU rough edges** (as of 2026-07-30, not re-verified
  against 2026.3.1 stable): open `openvino.genai` issues report a
  Whisper-turbo model hanging on NPU and unclean pipeline shutdown on NPU.
  Neither hit yet, but live captioning holds the pipeline resident and calls
  it continuously, which is a meaningfully different usage pattern from
  one-shot benchmarking.
- **Early-silicon NPU driver risk.** This laptop (Wildcat Lake, stepping A0)
  has a history of NPU driver/tooling rough edges. See the AuthFace NPU work
  in `wildcat-lake-linux` (`face-unlock-authface/npu-openvino-backend.md`) for
  what that looked like there: hand-patched driver, missing compiler libs in
  Fedora's package.
- **Socket-activation fd handoff: tested 2026-08-06, was broken, now fixed.**
  `server.py` called `run_simple(..., fd=fd)`, but `werkzeug.serving.run_simple`
  never had an `fd` kwarg at all (checked against werkzeug 3.1.8's actual
  signature) — every socket-activated start hit `TypeError:
  run_simple() got an unexpected keyword argument 'fd'` and systemd's
  restart limit killed the unit (`service-start-limit-hit`). `run_simple` is
  a thin CLI wrapper; `make_server()` is what it calls internally and does
  accept `fd=`. Server now calls `make_server(...).serve_forever()` directly.
  Confirmed end to end: `vinowhisper-server.socket` → cold NPU spawn on first
  connection → `/health` → live captions.

## Remaining questions

Ordered by what would most change the design.

1. **What is the real per-cycle time at a 12s window on dense speech?** Record
   a session, then `vinowhisper-replay --sweep 8,12,16,20`. If 12s is still
   multiple seconds, the next lever is trimming confirmed audio out of the
   buffer rather than shrinking the window further. That needs
   `return_timestamps`, and nobody has checked whether the NPU static pipeline
   supports it.
2. **Does the pipeline stay healthy under sustained continuous use?** Every
   number so far comes from one-shot benchmarks. A long `--record` session is
   the cheapest way to find out, since it leaves evidence either way.
3. **Does the status bar read well on a real pinned window?** It has been
   verified by rendering to a fixed-width buffer, never on a physical
   terminal. Column budgets at narrow widths are the likely rough edge.
4. **Does the CPU/GPU fallback actually run?** The stateful export has never
   been produced, let alone loaded. The plumbing is there and the failure
   modes are handled; whether `WhisperPipeline` on CPU with that export is
   usable at 12s windows is unknown. Cheapest check:
   `./scripts/convert_model.sh --variant stateful` then
   `vinowhisper-server --device CPU`.
5. **Are the non-Fedora package names right?** Eight families in `distro.py`,
   one of them confirmed by use. Each wrong name is a one-line fix and there is
   an issue template pointed at exactly this.
6. **Does the PulseAudio backend capture anything?** `parec` argv is unit-
   tested; it has never run against a PulseAudio server.
7. **Should `pyproject.toml` pin `transformers<5.4`?** Answered as far as the
   measurement goes: it is transformers, the pin is narrow, and
   optimum-intel 2.1.0 + optimum 2.3.0 + transformers 5.3.0 is verified working
   on the NPU. What is left is the policy call, holding a package back for
   everyone against shipping a tool whose first export fails. Only the export
   needs the old transformers; the runtime does not use it at all, which is an
   argument for a separate export extra rather than a hard dependency pin.
8. **Does the same break exist on the stateful (CPU/GPU) path?** The failure is
   in the NPU static pipeline specifically, and nobody has produced a stateful
   export at all, so it is unknown whether transformers 5.4.0 matters there.

## Conventions

- **There is a test suite now** (2026-08-13), reversing the earlier "keep this
  test-free" convention. The reason for the reversal: the tool stopped being
  single-machine, and CI has no NPU, no audio server and no OpenVINO. So the
  hard rule is that constraint, not the tests — **nothing under `tests/` may
  import `openvino`, `vinowhisper.transcriber` or `vinowhisper.server`**, and
  anything that would shell out is monkeypatched at the `capture` boundary.
  The old throwaway verification scripts from the 2026-08-06/08-07 reviews are
  in there as fixtures now, which is where they should have been.
- **A test named `test_characterization_*` pins a known oddity on purpose, and
  flipping one is a deliberate act, not a green-to-red accident.** This is a
  rule, not a style preference. Several behaviours here are correct as written
  and look like bugs to anyone reading them cold — `Live.update()` needing
  `refresh=True`, `stitch.push` committing `candidate`'s words rather than
  `_pending`'s, `_norm` stripping punctuation for comparison and never before
  printing, `--target` being refused on PulseAudio rather than reinterpreted,
  the sink monitor being pre-volume and pre-mute. Each was defended by a
  comment, and a comment does not fail when someone "fixes" the behaviour it
  describes.

  So: the docstring starts with `characterization:` and states the oddity, why
  it is right, and what breaks if it is "corrected". If one of these goes red,
  the question is not "which assertion do I update" — it is whether the
  behaviour was supposed to change at all. Same idea as "measured claims carry
  a date", applied to behaviour instead of prose: the sink-monitor reversal is
  the argument for both, and the dates are what made it recoverable.

  Audited 2026-09-04 while adopting this: four of those five had no test at
  all, only a comment. Adding one is cheap; adding it after someone has
  already deleted the behaviour is not.
- **Level assertions go through `tests/pcm.py`, not through `np.full`.** The
  audio layer is where the thresholds live (`SILENCE_RMS_THRESHOLD`,
  `TARGET_RMS`, `MAX_GAIN`), and on a constant array rms and amplitude are the
  same number, so `rms(np.full(100, 0.5)) == 0.5` asserts nothing. A sine
  separates them (rms = amplitude / sqrt(2)) and is what makes those tests
  mean something. Noise comes from a hand-rolled LCG rather than
  `numpy.random`, whose stream is versioned — seeding it pins a numpy release
  instead of a waveform. `pcm.chunks` reads its chunk size off
  `Recorder._READ_CHUNK_SAMPLES` on purpose: both 2026-08-06 buffer bugs were
  about chunk *arrival pattern*, so a fixture chunking at some other size
  tests a program that does not exist.
- `uv run poe check` is the whole gate (ruff, ruff format, mypy, pytest). CI
  installs `--group dev` only, never `uv sync`: a full resolve pulls ~400MB of
  OpenVINO that no test may import anyway. (Until 2026-08-31 there was a
  sharper reason, nightly wheels aging out of the index; that is gone with the
  nightly pin.) The full resolve is still checked weekly by `deps-canary.yml`.
- A failure should name its fix. Nearly every error path here prints the
  command that resolves it, and on anything environmental it prints the
  command *for the local distro* (`distro.remediation`). An exception that only
  says what went wrong is half-finished.
- Measured claims carry a date. This file and the README both previously
  asserted the opposite of the truth about the sink monitor in seven places;
  the dates are what made that recoverable.
- Keep `wildcat-lake-linux/input/f5-voice-typing.md` in sync with real
  decisions made here. That repo is the durable investigation record, this
  repo is just the code.
