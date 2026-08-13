# vinoWhisper

[![CI](https://github.com/karanshukla/vinoWhisper/actions/workflows/ci.yml/badge.svg)](https://github.com/karanshukla/vinoWhisper/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-blue)](pyproject.toml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

NPU-accelerated local live captioning for Linux, using OpenVINO GenAI's
`WhisperPipeline` on an Intel NPU. Named after
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

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/scripts/install.sh | bash
```

That installs [uv](https://docs.astral.sh/uv/), clones the repo, builds the
environment, and hands over to `vinowhisper-setup`, which is where every
machine-specific decision happens: your capture tool, your NPU driver, the
model export your device needs, and systemd units generated against the paths
that actually exist. It prints every command before running it and asks first.

From a checkout, or to see what it would do without doing it:

```bash
git clone https://github.com/karanshukla/vinoWhisper && cd vinoWhisper
uv sync
uv run vinowhisper-setup --dry-run   # the whole plan, nothing changed
uv run vinowhisper-setup             # for real, one prompt per step
```

**Why not `pip install vinowhisper`.** It would resolve `openvino` from PyPI,
where the builds that can construct the NPU static Whisper pipeline do not
exist yet — the pin lives in `[tool.uv.sources]`, which pip does not read. An
install that succeeds and then cannot load a model is worse than no install, so
that path is deliberately not offered. See [the nightly note](#openvino-nightly-and-why)
below.

## Commands

```
vinowhisper-caption                       # caption system audio
vinowhisper-caption --source mic          # caption yourself
vinowhisper-caption --list-targets        # capture one app instead of the whole sink
vinowhisper-caption --debug               # per-cycle timings, levels, raw transcript
vinowhisper-caption --record ~/sess       # save the session for replay
vinowhisper-caption --plain > out.txt     # no status bar (implied when piping)

vinowhisper-setup                         # guided install; re-runnable, idempotent
vinowhisper-setup --dry-run               # print the plan, change nothing
vinowhisper-setup --print-units           # the systemd units it would generate

vinowhisper-doctor                        # devices, model, audio, live levels
vinowhisper-doctor --json                 # the same, for a bug report
vinowhisper-doctor --no-probe             # skip the 2s-per-target level capture

vinowhisper-replay ~/sess --restitch      # re-run the merge logic offline
vinowhisper-replay ~/sess --sweep 8,12,20 # measure what --window actually costs
```

## Hardware, and what happens when you don't have it

The NPU is the point. Everything below it exists so a broken driver degrades
the tool instead of bricking it — the latency design assumes NPU-class cycle
times, and the two-cycle commit policy doubles any regression.

| Device | Selected | Model export | What you get |
|---|---|---|---|
| **NPU** (`Intel(R) AI Boost`) | first | `--disable-stateful` | ~1.19s per 30s window, measured 2026-08-03 |
| **GPU** (Arc / Xe) | second | stateful | Untested here. Works in principle; watch the lag figure |
| **CPU** | last resort | stateful | Runs. Competes with everything else on the machine, and lags |

Selection is automatic (`--device auto`). An *explicit* `--device NPU` that
isn't available is refused rather than quietly downgraded, because someone who
typed it wants to know it didn't happen.

A fallback is never silent. It appears in the server's journal, in `/health`,
in `vinowhisper-doctor`, and on the status bar as a red border and a `⚠` line:

```
╭─ vinoWhisper CPU ────────────────────────────────────────────────────────────╮
│ ● live    ███───────────  -48dB  ×12   ⟳ 6.4s ███▇█▇  lag ~12.8s  ⏳9  88 words │
│ ⚠ Running on the CPU. Every cycle now competes with everything else on the... │
╰──────────────────────────────────────────────────────────────────────────────╯
```

**The two model exports are not interchangeable.** The NPU needs
`--disable-stateful`, which produces the separate `decoder_with_past` KV-cache
submodel its static pipeline requires. That same export cannot run on CPU at
all — it fails on a `beam_idx` port error, which is incidentally how the NPU
was confirmed to be doing real work rather than silently falling back. So the
CPU/GPU path needs a second export in a second directory, and the wizard, the
doctor and the server all check which one you have against the device you got.

```bash
./scripts/convert_model.sh --variant npu        # ~/.local/share/vinowhisper/models/whisper-small.en-ov
./scripts/convert_model.sh --variant stateful   # ...-ov-stateful
./scripts/convert_model.sh --variant both
```

### When the NPU doesn't show up

`vinowhisper-doctor` walks it in fix-first order rather than reporting "no NPU"
and stopping: the `/dev/accel/accel0` node (does the in-tree `intel_vpu` driver
have the device?), its permissions (are you in the `render` group?), the
`intel_vpu` module, and then the userspace driver package for your distro, with
[Intel's release page](https://github.com/intel/linux-npu-driver/releases) as
the authoritative fallback. Those have different fixes and are indistinguishable
from OpenVINO's device list alone.

## Distro support

Audio capture works two ways, picked automatically:

- **PipeWire** (`pw-record`) — preferred, and the only backend that can tap an
  individual application's playback stream, which is what `--target` is for.
- **PulseAudio** (`parec`) — the fallback. Everything works except
  per-application capture; `--list-targets` there lists monitor sources and
  says why.

`pactl` is used for the default sink and mute state where present (on PipeWire
too, via `pipewire-pulse`); without it, PipeWire's own `default.audio.sink`
metadata answers the same question.

Package names per family live in one table in
[`vinowhisper/distro.py`](vinowhisper/distro.py), so `vinowhisper-doctor` and
the wizard both speak your distro:

| Family | Covered | Confidence |
|---|---|---|
| Fedora / RHEL / derivatives | ✓ | Built and run here |
| Debian / Ubuntu / Pop / Mint | ✓ | From the package index, not from use |
| Arch / CachyOS / EndeavourOS / Manjaro | ✓ | Same |
| openSUSE / SLES | ✓ | Same |
| Void, Gentoo, Alpine | ✓ | Same |
| NixOS | ✓ | Configuration advice, not `nix-env` lines — imperative installs there do not persist |
| Anything else | degrades | Generic advice, and it says so |

**If a package name is wrong for your distro, that is expected, and it is the
fastest thing here to fix** — see [CONTRIBUTING.md](CONTRIBUTING.md) or the
distro-support issue template.

## "Captions stop when you mute the system"

They don't, and this section used to say the opposite at length. Measured with
`vinowhisper-doctor` on 2026-08-07, always against a Chrome stream playing the
same audio as a control:

| Sink state | Default sink monitor | Chrome stream | Monitor / app |
|---|---|---|---|
| 100% volume | 0.01419 | 0.02040 | 0.70 |
| 20% volume | 0.05739 | 0.06610 | 0.87 |
| **Muted** | **0.08578** | **0.08781** | **0.98** |

The ratio is the measurement, not the absolute levels, since the content
differed between runs. It doesn't move with the volume slider and it doesn't
move on mute. **The sink monitor is both pre-volume and pre-mute here.**
`pw-dump` agrees on the volume half: `monitor.channel-volumes` is unset, and
PipeWire defaults it to `false`.

So the original diagnosis was wrong, and so was every mitigation built on it.
Lowering the volume costs nothing. Muting the system costs nothing. The
earlier threshold-lowering work was chasing a mechanism that isn't there.

### What actually silences the capture

1. **Muting the application rather than the system.** YouTube's own mute
   button, or Chrome's slider in the KDE mixer. The app then writes silence
   into its own PipeWire stream, and there is no tap upstream of that.
   `--target` does not help, because the stream it would capture is the
   silence. Note the node stays listed in `--list-targets` the whole time,
   because that lists nodes that exist, not nodes carrying signal. This is the
   likeliest explanation for the original report.
2. **`--target effect_output.bass_eq`.** It shows up in `--list-targets` as
   the most obvious-looking choice and is the worst one: it sits on the
   *output* side of the EQ chain, downstream of both volume and mute. It
   measured 0.00034 at 20% volume and 0.00000 while muted, in the same runs
   where the sink monitor read 0.05739 and 0.08578. It is the one node here
   that genuinely is post-everything. Aim at a real application instead.
3. **Nothing playing.** `vinowhisper-doctor` says so explicitly when every
   target reads zero.

Quiet-but-not-silent audio is handled separately: every window is boosted
toward a speech-like level (`config.TARGET_RMS`, up to 20x) before it reaches
the model, since Whisper's accuracy degrades on quiet input. Ordinary web
video lands around 0.014 rms, so this earns its place on source material
alone, independent of the volume question above.

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

`vinowhisper-doctor` checks the environmental things: OpenVINO's device list,
which device would be selected, whether each model export matches the device
that needs it, server reachability, the capture backend, default sink, mute
state, `monitor.channel-volumes`, and a two-second live level probe on the sink
monitor and on each playing app. Run it once with audio playing normally and
once with the system muted. If the sink monitor drops to silence while an app
stream stays audible, that is the mute problem confirmed by measurement, and it
says so.

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

```bash
systemctl --user stop vinowhisper-server.service          # drop the resident NPU process now
systemctl --user disable --now vinowhisper-server.socket  # full teardown
```

The socket unit is what respawns the service, so disabling it (not just the
service) is the one to use before a reboot or when you're done with the tool
for a while — otherwise the next `vinowhisper-caption` run just spawns it
again on first connection.

## OpenVINO nightly, and why

Three things `uv sync` handles that a bare `pip install -e .` does not, all
encoded in `pyproject.toml` rather than passed as flags:

1. **Python is held below 3.14.** 3.14 made `functools.partial` a descriptor,
   which breaks `optimum`'s `NORMALIZED_CONFIG_CLASS = SomeConfig.with_args(...)`
   class-attribute idiom outright. Version-independent root cause, confirmed
   2026-08-03 across every optimum/transformers pairing tried.
2. **The three `openvino*` packages route to the nightly wheel index**, with
   prereleases allowed. Stable `openvino-genai` (2026.2.1 as of writing) cannot
   build the NPU static Whisper pipeline: its pattern-matcher does not recognise
   the current export's SDPA attention-mask node shape.
3. **The nightly index is `explicit = true`**, so it adds to PyPI rather than
   replacing it.

This is the project's standing dependency risk, and it is watched rather than
assumed: `.github/workflows/deps-canary.yml` runs the full resolve weekly, off
the pull-request path, precisely because nightly builds get pruned upstream on
their own schedule. The outcome to hope for is that a stable OpenVINO release
catches up and the whole nightly pin can be deleted.

## Pinning it on top

The status bar is Rich in an ordinary terminal, so keeping it above other
windows is a window-manager job, not the app's. On KWin: System Settings >
Window Management > Window Rules, match the terminal window, set Keep Above
Other Windows to Force/Yes, plus Skip Taskbar and Skip Pager if you want it out
of the way. No titlebar and a small fixed size make it read like an overlay
rather than a terminal.

## More

- Design doc, benchmarks, and the three export bugs hit getting to a working
  NPU pipeline:
  [wildcat-lake-linux/input/f5-voice-typing.md](https://github.com/karanshukla/wildcat-lake-linux/blob/main/input/f5-voice-typing.md)
- [CONTRIBUTING.md](CONTRIBUTING.md) — the useful contributions are distro
  corrections and reports from hardware that isn't this laptop
- [SECURITY.md](SECURITY.md) — what stays on the machine, and what the
  loopback server's trust boundary actually is
- [CHANGELOG.md](CHANGELOG.md)

MIT licensed.
