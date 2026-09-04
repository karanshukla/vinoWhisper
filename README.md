# vinoWhisper

[![PyPI](https://img.shields.io/badge/PyPI-vinowhisper-blue?logo=pypi&logoColor=white)](https://pypi.org/project/vinowhisper/)
![PyPI - Version](https://img.shields.io/pypi/v/vinowhisper?label=latest%20version)
[![Python](https://img.shields.io/pypi/pyversions/vinowhisper)](https://pypi.org/project/vinowhisper/)
[![License](https://img.shields.io/badge/license-MIT-blue)](https://github.com/karanshukla/vinoWhisper/blob/main/LICENSE)
[![CI](https://github.com/karanshukla/vinoWhisper/actions/workflows/ci.yml/badge.svg)](https://github.com/karanshukla/vinoWhisper/actions/workflows/ci.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**Live captions for anything playing on your Linux laptop, running on the NPU
that came with it.** No cloud, no API key, no account, no audio leaving the
machine. Point it at whatever is playing and it captions in your terminal.
There is nothing to interact with: start it and it goes.

<img width="1237" height="530" alt="vinoWhisper captioning a video, with the status bar pinned at the bottom" src="https://github.com/user-attachments/assets/f263eabf-f1f4-4ab2-9b68-bc50eaf92ea0" />

```bash
pip install vinowhisper && vinowhisper-setup && vinowhisper-caption
```

## Why this exists

To be upfront about the bias: I did not build this because local speech-to-text
is hard to find. I built it because my laptop has an Intel NPU rated at 16 TOPS
that was doing **absolutely nothing**, and captioning a video turns out to be
the rare workload that suits it: continuous, latency-sensitive, and small
enough to fit. 16 TOPS is a coprocessor, not a GPU, and whisper-small.en is one
of the genuinely useful models that fits in it.

So the pitch is not "another Whisper wrapper." It is: the transcription runs on
a chip that is otherwise idle, so it costs you no CPU, no GPU, no fan, and no
network. `openvino-genai`'s `WhisperPipeline` with `device="NPU"` does the
work, the same idea as [vinoAuthFace](https://github.com/karanshukla/vinoAuthFace),
different feature.

Measured on this laptop (Wildcat Lake, stepping A0), whisper-small.en:

| | |
|---|---|
| Per 30s window, `generate()` | **1.19s** |
| First streamed token | **0.204s** |
| First readable sentence | **0.32s** |
| Idle cost after you stop | **zero, the server exits itself** |

## What makes it different from a shell script around Whisper

**Words are never rewritten once printed.** The stitcher runs LocalAgreement-2:
a word is committed only when two overlapping windows independently agree on
it. That is why the transcript can live in your terminal's own scrollback,
where it survives quitting and where your terminal's selection and search still
work on it. The `hearing…` line is the words heard once and still waiting on a
second opinion, so the two-cycle commit delay is _visible_ rather than felt as
a freeze.

**It scales to zero, the systemd way.** The NPU model load costs 10-30s, so
something has to hold it. A socket unit owns the port at boot with no process
running, systemd spawns the server on the first connection, and the server
exits itself after 30 minutes idle. Serverless, on your laptop, with no
framework.

**The model download is verified.** The export is hashed against pins in
`vinowhisper/model_digests.json` before anything loads it, and the check tells
a toolchain upgrade apart from bytes changing under a toolchain that did not.
Details in [SECURITY.md](https://github.com/karanshukla/vinoWhisper/blob/main/SECURITY.md).

**A missing NPU degrades instead of bricking.** Selection walks NPU, then GPU,
then CPU, and a fallback is never silent: it shows up in the server journal, in
`/health`, in `vinowhisper-doctor`, and on the status bar as a red border.

**It tells you how to fix it.** Nearly every error path here prints the command
that resolves it, in your distro's package names, for eight distro families.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/scripts/install.sh | bash
```

That installs [uv](https://docs.astral.sh/uv/), clones the repo, builds the
environment, and hands over to `vinowhisper-setup`, which is where every
machine-specific decision happens: your capture tool, your NPU driver, the
model export your device needs, and systemd units generated against paths that
actually exist. It prints every command before running it and asks first.

From a checkout, or to see what it would do without doing it:

```bash
git clone https://github.com/karanshukla/vinoWhisper && cd vinoWhisper
uv sync
uv run vinowhisper-setup --dry-run   # the whole plan, nothing changed
uv run vinowhisper-setup             # for real, one prompt per step
```

Or from PyPI, if you would rather wire up the machine yourself:

```bash
pip install vinowhisper   # needs Python 3.11-3.13
vinowhisper-setup         # still worth running: NPU driver, model export, units
```

`pip install` gets you the five commands and the Python dependencies. It cannot
get you an NPU driver, a model export or systemd units, which is what
`vinowhisper-setup` is for either way. See
[docs/install.md](https://github.com/karanshukla/vinoWhisper/blob/main/docs/install.md)
for the OpenVINO version floor and why this could not be a pip install until
2026-08-31.

## What you need

| | |
|---|---|
| **OS** | Linux. Developed on Fedora 45 / KDE Plasma 6 / Wayland |
| **Audio** | PipeWire (`pw-record`) or PulseAudio (`parec`), picked automatically |
| **Accelerator** | Intel NPU for the numbers above. GPU and CPU run, slower |
| **Python** | 3.11 to 3.13. [3.14 cannot export the model](https://github.com/karanshukla/vinoWhisper/blob/main/docs/install.md) |
| **Disk** | ~1.5GB for the model export |

The NPU needs a userspace driver half that no distro packages completely, and
`vinowhisper-doctor` will tell you exactly which half is missing.
[docs/hardware.md](https://github.com/karanshukla/vinoWhisper/blob/main/docs/hardware.md)
covers every way it fails to appear.

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

vinowhisper-doctor                        # devices, model, digests, audio, live levels
vinowhisper-doctor --json                 # the same, for a bug report
vinowhisper-doctor --no-probe             # skip the 2s-per-target level capture

vinowhisper-replay ~/sess --restitch      # re-run the merge logic offline
vinowhisper-replay ~/sess --sweep 8,12,20 # measure what --window actually costs
```

## Docs

| | |
|---|---|
| [Installing](https://github.com/karanshukla/vinoWhisper/blob/main/docs/install.md) | What the installer does, the OpenVINO version floor and why, digest pinning, pinning the window on top |
| [Hardware](https://github.com/karanshukla/vinoWhisper/blob/main/docs/hardware.md) | Device selection, the two model exports, and every way the NPU fails to appear |
| [Audio capture](https://github.com/karanshukla/vinoWhisper/blob/main/docs/audio.md) | PipeWire vs PulseAudio, distro coverage, and what actually silences a capture (it is not the mute button) |
| [Latency](https://github.com/karanshukla/vinoWhisper/blob/main/docs/latency.md) | Why captions trail the audio, the one knob that changes it, and why the wording drifts |
| [Debugging](https://github.com/karanshukla/vinoWhisper/blob/main/docs/debugging.md) | `--record`, offline replay, and what `vinowhisper-doctor` measures |
| [Architecture](https://github.com/karanshukla/vinoWhisper/blob/main/docs/architecture.md) | Socket activation and scale-to-zero, and how to stop it |

## Honest limits

Worth saying before you install it, because the numbers above are all from one
machine:

- **Every benchmark here is n=1**, on one laptop, with early-silicon NPU
  drivers. The GPU and CPU fallbacks have never run on hardware at all.
- **Captions trail the audio by roughly twice the cycle time.** That is
  inherent to a two-cycle commit policy, not a bug to be tuned away.
  [docs/latency.md](https://github.com/karanshukla/vinoWhisper/blob/main/docs/latency.md)
  explains the one knob that moves it.
- **Wording drifts between cycles**, because each window is re-decoded with
  more right-context than the last. The stitcher hides most of it and not all.
- **Package names for seven of the eight distro families are unverified.** If
  one is wrong for yours, that is expected, and it is the fastest thing in this
  repo to fix.
- **The current export toolchain produces a broken NPU model.** Measured
  2026-09-04: optimum-intel 2.1.0 / transformers 5.5.4, which is what a fresh
  install resolves to, exports a graph that compiles and then fails at
  `generate()` with `Port for tensor name cache_position was not found`. The
  digest check catches it and says so rather than letting it fail at the first
  transcription, but it is not fixed.
  [docs/install.md](https://github.com/karanshukla/vinoWhisper/blob/main/docs/install.md)
  has the control run.

## More

- Design doc, benchmarks, and the three export bugs hit getting to a working
  NPU pipeline:
  [wildcat-lake-linux/input/f5-voice-typing.md](https://github.com/karanshukla/wildcat-lake-linux/blob/main/input/f5-voice-typing.md)
- [CONTRIBUTING.md](https://github.com/karanshukla/vinoWhisper/blob/main/CONTRIBUTING.md),
  where the useful contributions are distro corrections and reports from
  hardware that isn't this laptop
- [SECURITY.md](https://github.com/karanshukla/vinoWhisper/blob/main/SECURITY.md),
  what stays on the machine and what the loopback server's trust boundary
  actually is
- [CHANGELOG.md](https://github.com/karanshukla/vinoWhisper/blob/main/CHANGELOG.md)

If you run this on hardware that isn't a Wildcat Lake laptop, I want the
report, working or not. That is the one thing I cannot test myself, and an
[issue](https://github.com/karanshukla/vinoWhisper/issues) with
`vinowhisper-doctor --json` pasted into it is worth more than any benchmark I
can run here.

MIT licensed.
