# vinoWhisper

[![CI](https://github.com/karanshukla/vinoWhisper/actions/workflows/ci.yml/badge.svg)](https://github.com/karanshukla/vinoWhisper/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue)](https://github.com/karanshukla/vinoWhisper/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-blue)](https://github.com/karanshukla/vinoWhisper/blob/main/pyproject.toml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

NPU-accelerated local live captioning for Linux, using OpenVINO GenAI's
`WhisperPipeline` on an Intel NPU. Named after
[vinoAuthFace](https://github.com/karanshukla/vinoAuthFace), same idea of
OpenVINO doing the NPU work, different feature.

Point it at whatever is playing and it captions in your terminal. Nothing
leaves the machine. Start it and it goes; there is nothing to interact with.

<img width="1237" height="530" alt="image" src="https://github.com/user-attachments/assets/f263eabf-f1f4-4ab2-9b68-bc50eaf92ea0" />

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

Or from PyPI, if you would rather wire up the machine yourself:

```bash
pip install vinowhisper   # needs Python 3.11-3.13
vinowhisper-setup         # still worth running: NPU driver, model export, units
```

`pip install` gets you the five commands and the Python dependencies. It cannot
get you an NPU driver, a model export or systemd units, which is what
`vinowhisper-setup` is for either way. See
[docs/install.md](https://github.com/karanshukla/vinoWhisper/blob/main/docs/install.md) for the OpenVINO version
floor and why this could not be a pip install until 2026-08-31.

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

## Hardware

The NPU is the point. Everything below it exists so a broken driver degrades
the tool instead of bricking it.

| Device | Selected | Model export | What you get |
|---|---|---|---|
| **NPU** (`Intel(R) AI Boost`) | first | `--disable-stateful` | ~1.19s per 30s window, measured 2026-08-03 |
| **GPU** (Arc / Xe) | second | stateful | Untested here. Works in principle; watch the lag figure |
| **CPU** | last resort | stateful | Runs. Competes with everything else on the machine, and lags |

Selection is automatic and a fallback is never silent: it shows up in the
server journal, in `/health`, in `vinowhisper-doctor`, and on the status bar as
a red border. The two model exports are not interchangeable, and the NPU needs
a userspace driver half that no distro packages completely.
[docs/hardware.md](https://github.com/karanshukla/vinoWhisper/blob/main/docs/hardware.md) covers all of it, including what to do
when the NPU does not show up.

Audio capture works on PipeWire (`pw-record`) or PulseAudio (`parec`), picked
automatically, and package names for eight distro families live in one table in
[`vinowhisper/distro.py`](https://github.com/karanshukla/vinoWhisper/blob/main/vinowhisper/distro.py). **If a name is wrong for your
distro, that is expected, and it is the fastest thing here to fix.**

## Docs

| | |
|---|---|
| [Installing](https://github.com/karanshukla/vinoWhisper/blob/main/docs/install.md) | What the installer does, the OpenVINO version floor and why, pinning the window on top |
| [Hardware](https://github.com/karanshukla/vinoWhisper/blob/main/docs/hardware.md) | Device selection, the two model exports, and every way the NPU fails to appear |
| [Audio capture](https://github.com/karanshukla/vinoWhisper/blob/main/docs/audio.md) | PipeWire vs PulseAudio, distro coverage, and what actually silences a capture (it is not the mute button) |
| [Latency](https://github.com/karanshukla/vinoWhisper/blob/main/docs/latency.md) | Why captions trail the audio, the one knob that changes it, and why the wording drifts |
| [Debugging](https://github.com/karanshukla/vinoWhisper/blob/main/docs/debugging.md) | `--record`, offline replay, and what `vinowhisper-doctor` measures |
| [Architecture](https://github.com/karanshukla/vinoWhisper/blob/main/docs/architecture.md) | Socket activation and scale-to-zero, and how to stop it |

## More

- Design doc, benchmarks, and the three export bugs hit getting to a working
  NPU pipeline:
  [wildcat-lake-linux/input/f5-voice-typing.md](https://github.com/karanshukla/wildcat-lake-linux/blob/main/input/f5-voice-typing.md)
- [CONTRIBUTING.md](https://github.com/karanshukla/vinoWhisper/blob/main/CONTRIBUTING.md), where the useful contributions are distro
  corrections and reports from hardware that isn't this laptop
- [SECURITY.md](https://github.com/karanshukla/vinoWhisper/blob/main/SECURITY.md), what stays on the machine and what the loopback
  server's trust boundary actually is
- [CHANGELOG.md](https://github.com/karanshukla/vinoWhisper/blob/main/CHANGELOG.md)

MIT licensed.
