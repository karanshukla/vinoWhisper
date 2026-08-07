# vinoWhisper

NPU-accelerated local live captioning for Fedora/KDE, using OpenVINO GenAI's
`WhisperPipeline` on the Intel NPU. Named after
[vinoAuthFace](https://github.com/karanshukla/vinoAuthFace), same idea of
OpenVINO doing the NPU work, different feature.

Point it at whatever is playing and it captions in your terminal. Nothing
leaves the machine. Start it and it goes; there is nothing to interact with.

```
╭─ vinoWhisper NPU ────────────────────────────────────────────────────────────╮
│ ● live    ███───────────  -48dB  ×12   ⟳ 1.8s █▆▆▆▅▅  lag ~3.9s  ⏳7  341 words │
│ hearing… and the dugout emptied out behind him                                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

The transcript scrolls above that bar in your terminal's own scrollback, so it
is still there after you quit and your terminal's selection and search still
work on it. `hearing…` is the words heard once but still waiting on a second
cycle to agree, which is the two-cycle commit delay made visible rather than
felt as a freeze.

```
vinowhisper-caption                       # caption system audio
vinowhisper-caption --source mic          # caption yourself
vinowhisper-caption --debug               # per-cycle timings, levels, raw transcript
vinowhisper-caption --record ~/sess       # save the session for replay
vinowhisper-caption --plain > out.txt     # no status bar (implied when piping)

vinowhisper-doctor                        # check NPU, model, sink, mute, live levels
vinowhisper-replay ~/sess --restitch       # re-run the merge logic offline
vinowhisper-replay ~/sess --sweep 8,12,20  # measure what --window actually costs
```

Design doc and rationale (why this is built from scratch instead of reusing
`whisper-npu-server`, which turned out to be partly dead), plus the full
feasibility-spike writeup with benchmarks:
[wildcat-lake-linux/input/f5-voice-typing.md](https://github.com/karanshukla/wildcat-lake-linux/blob/main/input/f5-voice-typing.md).

**Status: the whole loop is built and has run against real content. Accuracy
and latency are the open problems, not whether it works at all.** The
transcription backend is confirmed genuinely on NPU (not a silent CPU
fallback), whisper-small.en benchmarks at ~1.19s per 30s window with first
streamed token at ~0.204s, and captions have been compared side by side
against YouTube's own generated captions as ground truth. The cleanup pass
documented below fixed the bugs that review turned up, but it has not been
re-run on the actual laptop yet.

## Layout

```
vinowhisper/
  config.py       paths, ports, window sizing, levels, timeouts
  audio.py        RingBuffer, RMS, gain normalization
  recorder.py     Recorder, continuous pw-record capture into the ring buffer
  client.py       TranscriptionClient, HTTP client for the server below
  server.py       local-only Flask server, socket-activated + self-idle-exit
  transcriber.py  WhisperTranscriber, wraps openvino_genai.WhisperPipeline (device="NPU")
  stitch.py       Stitcher, merges overlapping window transcripts into one
  caption.py      the caption loop and CLI (vinowhisper-caption)
scripts/
  convert_model.sh   optimum-cli export wrapper (whisper-small.en -> OpenVINO IR)
systemd/
  vinowhisper-server.socket    systemd owns the listening port, starts the service lazily
  vinowhisper-server.service   the transcription server, socket-activated (no [Install])
```

## Captions stop when you mute the system

This is the most-reported problem and it is not a bug in this code. A sink's
monitor carries what the sink is playing _after_ its own volume and mute are
applied, so muting the system means the capture reads genuine digital silence.
There is nothing to transcribe. The same mechanism is why captions got worse
as the volume slider went down, which is what the earlier threshold-lowering
attempts were chasing.

Two things help:

1. **Capture the application instead of the sink.** An app's own playback
   stream node also exposes monitor ports, and those sit upstream of the
   sink's mute:

   ```
   vinowhisper-caption --list-targets
   vinowhisper-caption --target 1043
   ```

   The app's own per-stream volume in the KDE mixer still applies, but the
   master mute no longer silences the capture.

2. **Check `monitor.channel-volumes` on the sink.** If it is `true`, the
   monitor is post-volume, which is consistent with what has been observed
   here. It defaults to `false` in PipeWire, so something in the local setup
   (plausibly the `effect_input.bass_eq` filter chain) is likely turning it
   on. Worth confirming on hardware:

   ```
   pw-cli enum-params $(pactl get-default-sink) Props
   ```

Quiet-but-not-silent audio is handled separately: every window is now boosted
toward a speech-like level (`config.TARGET_RMS`, up to 20x) before it reaches
the model, since Whisper's accuracy degrades on quiet input.

## Latency, and the one knob that matters

The caption loop is synchronous, so the hop between windows is just however
long the previous cycle took, and the commit policy needs two cycles to agree
before printing anything. Captions therefore trail the audio by roughly twice
the cycle time. Cycle time is the only real lever.

Whisper's encoder cost is fixed (it pads to 30s no matter what), but decoding
is autoregressive, one forward pass per token. A window packed with 29.5s of
dense speech emits roughly 2.5x the tokens of a 12s one and takes
correspondingly longer. That is why the default window is now 12s rather than
the full 29.5s short-form limit:

```
vinowhisper-caption --window 8      # snappier, less context, more wording drift
vinowhisper-caption --window 20     # steadier wording, noticeably laggier
```

`--debug` prints the numbers to tune against: window length, hop, RMS, gain
applied, time to first streamed piece, total cycle time, and how many words
each cycle confirmed versus held pending.

`vinowhisper-replay --sweep` measures the tradeoff on your own audio instead of
guessing at it. The est. lag column is `2 x mean`, which is the floor the
two-cycle commit policy imposes:

```
| window | decodes | mean | p90 | first piece | words/decode | est. lag |
```

## Debugging without the hardware in front of you

`--record DIR` writes `audio.wav` (16kHz mono, opens in Audacity) plus
`events.jsonl`, one JSON object per cycle with every number `--debug` prints
and the raw pre-stitch transcript. Roughly 2MB per minute.

That turns "it was laggy while I watched a video" into a fixture:

- `vinowhisper-replay DIR --restitch` feeds the recorded transcripts back
  through the stitcher with no NPU, no server and no audio. The model output
  is frozen, so any difference in what gets printed is your change and nothing
  else. It diffs against what the original run printed and points at the first
  divergence.
- `vinowhisper-replay DIR --sweep 8,12,16,20` needs the NPU and slices the
  recorded audio at a fixed hop, so every window size sees the same decodes
  over the same audio.

`vinowhisper-doctor` checks the environmental things that are currently
guesses: NPU enumeration, whether the model export has the
`decoder_with_past` submodel that `--disable-stateful` produces, server
reachability, default sink, current mute state, `monitor.channel-volumes`, and
a two-second live level probe on the sink monitor and on each playing app.
Run it once with audio playing normally and once with the system muted. If the
sink monitor drops to silence while an app stream stays audible, that is the
mute problem confirmed by measurement, and it says so.

## Why the captions reword themselves

Each cycle re-transcribes a window that mostly overlaps the last one, and
Whisper does not decode the same audio the same way twice. Real testing on
2026-08-03 caught it producing "Ex-sherzer", "you're told", and "you know
Dalton" for the same underlying audio across three consecutive cycles. That is
not paraphrasing near a boundary, the words genuinely are not the same until
the model has enough context to settle.

`stitch.py` handles this with a LocalAgreement-2 commit policy: a word only
prints once two consecutive cycles agree on it. That also means a hallucinated
guess on near-silence never reaches the screen, because the next cycle guesses
something else. The cost is the two-cycle latency described above.

The obvious remaining fix, priming each decode with the prior transcript via
`initial_prompt`, is hard-blocked on this device: `RuntimeError:
'initial_prompt' parameter is not supported on NPU device`. Some cross-window
wording drift is an accepted limitation until that changes.

## Design decision: scale-to-zero, not an always-on daemon

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

**Stopping it.** There's no daemon to manage day to day — the socket unit
holds the port with no process behind it until something connects, and the
service self-exits after `IDLE_TIMEOUT_S` regardless. Two commands cover the
rest:

```
systemctl --user stop vinowhisper-server.service      # drop the resident NPU process now, instead of waiting out the idle timeout
systemctl --user disable --now vinowhisper-server.socket  # full teardown: also stops systemd owning the port at all
```

The socket unit is what respawns the service, so disabling it (not just the
service) is the one to use before a reboot or when you're done with the tool
for a while — otherwise the next `vinowhisper-caption` run just spawns it
again on first connection.

## Setup

1. **`uv sync`.** That's the whole install. Two things it handles that a bare
   `pip install -e .` does not, both encoded in `pyproject.toml` rather than
   passed as flags: it holds Python to `<3.14` (3.14 made `functools.partial`
   a descriptor, which breaks `optimum`'s export code outright), and it routes
   the three `openvino*` packages to the nightly wheel index with prereleases
   allowed, because stable `openvino-genai` (2026.2.1 as of writing) cannot
   build the NPU static Whisper pipeline.
2. **Convert the model.** `./scripts/convert_model.sh` produces the
   whisper-small.en OpenVINO IR, including the `--disable-stateful` flag NPU
   requires.
3. **Install the systemd units.** Copy both files from `systemd/` into
   `~/.config/systemd/user/`, then
   `systemctl --user enable --now vinowhisper-server.socket`. Enable the
   **socket** unit, not the service. The service has no `[Install]` section on
   purpose, it is only ever meant to be started by the socket.
4. **Put the commands on PATH.** Optional but worth it, and the completion in
   step 5 depends on it:
   ```
   for c in caption server replay doctor; do
       ln -sf "$PWD/.venv/bin/vinowhisper-$c" ~/.local/bin/
   done
   ```
   Symlinks rather than `uv tool install .`, deliberately: `uv sync` installs
   the project editable with an absolute shebang, so these track your edits
   live. `uv tool install` would build an isolated snapshot and re-resolve the
   whole nightly OpenVINO stack into a second copy, which is several GB to get
   a stale build.
5. **Optional: bash completion.** Install snippet at the top of
   `scripts/vinowhisper-completion.bash`. `--target` completes against the
   applications actually playing audio right now, which is the one flag whose
   values you cannot guess. It completes `vinowhisper-caption`, not `uv run
   vinowhisper-caption` — in the latter the command word is `uv`, so uv's own
   completion owns the line. Hence step 4.
6. **Run it.** `vinowhisper-caption`, Ctrl+C to stop (or `uv run
   vinowhisper-caption` if you skipped step 4). Add `--source mic` to caption
   yourself instead of system audio.

## Pinning it on top

The status bar is Rich in an ordinary terminal, so keeping it above other
windows is a KWin job, not the app's. Add a window rule (System Settings >
Window Management > Window Rules) matching the terminal window, and set Keep
Above Other Windows to Force/Yes, plus Skip Taskbar and Skip Pager if you want
it out of the way. No titlebar and a small fixed size make it read like an
overlay rather than a terminal.

Binding the whole thing to the laptop's dictation key (which emits `Meta+H`,
currently pointed at Ghostty's `new-window`) is still unwired. Get the loop
behaving on hardware first.

See the design doc linked at the top for the benchmark tables, the three
export bugs hit getting to a working NPU pipeline, and the open items.
