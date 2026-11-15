<p align="center">
  <img src="https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/docs/assets/vinowhisper.svg" width="112" alt="The vinoWhisper mark: a dark caption box with green voice bars turning into lines of text">
</p>

# vinoWhisper

[![PyPI](https://img.shields.io/pypi/v/vinowhisper?logo=pypi&logoColor=white)](https://pypi.org/project/vinowhisper/)
[![Python](https://img.shields.io/pypi/pyversions/vinowhisper)](https://pypi.org/project/vinowhisper/)
[![License](https://img.shields.io/badge/license-MIT-blue)](https://github.com/karanshukla/vinoWhisper/blob/main/LICENSE)
[![CI](https://github.com/karanshukla/vinoWhisper/actions/workflows/ci.yml/badge.svg)](https://github.com/karanshukla/vinoWhisper/actions/workflows/ci.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**Live captions for anything playing on your Linux laptop, running on the NPU
that came with it.** No cloud, no API key, no account, no audio leaving the
machine. Point it at whatever is playing and it captions in your terminal, or
in a floating box above everything else on screen. The same box does
dictation too: hold a key, talk, and let go to type what you said. There is
nothing to configure: start it and it goes.

<img width="1237" height="530" alt="vinoWhisper captioning a video, with the status bar pinned at the bottom" src="https://github.com/user-attachments/assets/f263eabf-f1f4-4ab2-9b68-bc50eaf92ea0" />

```bash
pip install vinowhisper && vinowhisper-setup && vinowhisper-caption
```

## Why this exists

To be upfront about the bias: I did not build this because local speech-to-text
is hard to find. I built it because my laptop has an Intel NPU rated at 16 TOPS
that was doing **absolutely nothing**, and captioning a video is the rare
workload that suits it: continuous, latency-sensitive, and small enough to fit.

So the pitch is not "another Whisper wrapper." The transcription runs on a chip
that is otherwise idle, so it costs you no CPU, no GPU, no fan, and no network.
`openvino-genai`'s `WhisperPipeline` with `device="NPU"` does the work, the same
idea as [vinoAuthFace](https://github.com/karanshukla/vinoAuthFace), different
feature.

Measured on this laptop (Wildcat Lake, stepping A0), whisper-small.en on the
NPU, 2026-09-12:

| | |
|---|---|
| Per 12s window (the default), `generate()` | **0.70s** mean, 0.85s p90 |
| Captions behind the audio | **~1.5s** at best |
| First streamed token | **0.204s** |
| Idle cost after you stop | **zero, the server exits itself** |

The Intel iGPU does the same window in 0.95s and the CPU in 2.30s, so both
fallbacks are usable, just laggier.

## What makes it different from a shell script around Whisper

- **Words are never rewritten once printed.** A word is committed only when two
  overlapping windows agree on it (LocalAgreement-2), so the transcript lives in
  your terminal's own scrollback. The dimmed `hearing…` line is what is still
  waiting on a second opinion. [Latency](https://docs.vinowhisper.com/latency/)
- **It scales to zero.** The NPU model load costs 10-30s, so a server holds it.
  The first client starts that server on a private Unix socket and it exits
  itself after 5 minutes idle. [Architecture](https://docs.vinowhisper.com/architecture/)
- **The model download is verified.** The Hugging Face source is pinned to a
  commit and sha256-checked per file, and the export is hashed against
  `vinowhisper/model_digests.json` before anything loads it.
  [SECURITY.md](https://github.com/karanshukla/vinoWhisper/blob/main/SECURITY.md)
- **A missing NPU degrades instead of bricking.** Selection walks NPU, GPU, CPU,
  and a fallback is never silent: server log, `/health`, `vinowhisper-doctor`,
  and a red border on the status bar. [Hardware](https://docs.vinowhisper.com/hardware/)
- **It tells you how to fix it.** Nearly every error prints the command that
  resolves it, in your distro's package names.
- **The overlay is optional, and native.** `vinowhisper-tray` is a 6.3MB Rust
  binary that floats a caption box above every window, fullscreen video
  included. It links nothing but libc and adds nothing to the Python install.
- **Dictation, in the same overlay.** Hold Meta+H, talk, let go, and it types
  what you said into the focused window. Optionally a small model on the NPU
  tidies it first, and only by deleting ("um", stammers, "scratch that"). It
  never adds or rewords a word.
  [Overlay and dictation](https://docs.vinowhisper.com/tray/)

## Install

```bash
curl -fsSL https://github.com/karanshukla/vinoWhisper/releases/latest/download/install.sh | bash
```

That verifies the release's wheel, installs it with [uv](https://docs.astral.sh/uv/)
and hands over to `vinowhisper-setup`, which prints every command before running
it and asks first.

| I want | Run |
|---|---|
| The overlay and dictation | `vinowhisper-setup --gui` |
| pip instead of the installer | `pip install vinowhisper && vinowhisper-setup` (Python 3.11-3.13) |
| A Flatpak | `flatpak install --user vinowhisper-x86_64.flatpak` from the release. Not run on a desktop yet: [what has been checked](https://docs.vinowhisper.com/install/#flatpak) |
| To upgrade | **Check for updates** in Settings → About, or `vinowhisper-upgrade` |
| To remove it | `vinowhisper-upgrade --uninstall`. Models go, sessions and settings stay |

pip gets you the commands. It cannot get you an NPU driver or a model export,
which is what `vinowhisper-setup` is for either way.
[Installing](https://docs.vinowhisper.com/install/) has what each of these does.

## What you need

| | |
|---|---|
| **OS** | Linux. Developed on Fedora 45 / KDE Plasma 6 / Wayland. See [Supported distros](#supported-distros) |
| **Audio** | PipeWire (`pw-record`) or PulseAudio (`parec`), picked automatically |
| **Accelerator** | Intel NPU for the numbers above. An Intel GPU or the CPU runs, slower. AMD NPUs are detected and not usable (no OpenVINO plugin) |
| **Python** | 3.11 to 3.13. [3.14 cannot export the model](https://docs.vinowhisper.com/install/) |
| **Disk** | ~1.5GB for the model export |
| **Overlay** (optional) | A Wayland compositor with wlr-layer-shell: KDE Plasma 6, Sway, Hyprland, niri, COSMIC. Not GNOME |

The NPU needs a userspace driver half that no distro packages completely, and
`vinowhisper-doctor` will tell you exactly which half is missing.
[docs/hardware.md](https://docs.vinowhisper.com/hardware/)
covers every way it fails to appear.

## Supported distros

`vinowhisper-setup` and `vinowhisper-doctor` read `/etc/os-release` and print
install commands in your distro's own package names. Eight families are covered:
Fedora, Debian, Arch, openSUSE, Gentoo, Void, Alpine and NixOS, plus their
derivatives and the image-based ones (Fedora Atomic, openSUSE Aeon). **Only
Fedora's have been used for real.**
[The table, with the NPU driver per family](https://docs.vinowhisper.com/install/#supported-distros)

## Commands

```
vinowhisper-caption                       # caption system audio
vinowhisper-caption --source mic          # caption yourself
vinowhisper-caption --list-targets        # capture one app instead of the whole sink
vinowhisper-caption --debug               # per-cycle timings, levels, raw transcript
vinowhisper-caption --record ~/sess       # save the session for replay
vinowhisper-caption --plain > out.txt     # no status bar (implied when piping)
vinowhisper-caption --json                # one event per line, what the overlay reads

vinowhisper-transcribe lecture.mp3        # a file into a saved session, no playback

vinowhisper-setup --language fr           # French (de, es, auto): sets up the multilingual model
vinowhisper-setup --language auto --task translate   # foreign speech, captioned in English

vinowhisper-tray                           # the floating overlay and tray icon
vinowhisper-tray toggle                    # show/hide it; Meta+Alt+C does the same
vinowhisper-tray dictate                   # start/finish dictating; hold Meta+H does the same
vinowhisper-tray --install --autostart     # launcher entry, and the tray at login
                                          #   (or tick "Start at login" in the tray)

vinowhisper-setup                         # guided install; re-runnable, idempotent
vinowhisper-setup --dry-run               # print the plan, change nothing
vinowhisper-server --stop                 # unload the model now; the next use starts it again
vinowhisper-setup --gui                   # just the optional overlay and dictation
vinowhisper-setup --ovfetch               # just ovfetch, NPU driver data for the doctor
vinowhisper-upgrade --uninstall           # remove it, models included; sessions and settings stay

vinowhisper-doctor                        # devices, model, digests, audio, live levels
vinowhisper-doctor --json                 # the same, for a bug report
vinowhisper-doctor --no-probe             # skip the 2s-per-target level capture

vinowhisper-replay ~/sess --restitch      # re-run the merge logic offline
vinowhisper-replay ~/sess --sweep 8,12,20 # measure what --window actually costs
```

## Docs

| | |
|---|---|
| [Installing](https://docs.vinowhisper.com/install/) | What the installer does, the OpenVINO version floor and why, digest pinning, pinning the window on top |
| [Terminal captions](https://docs.vinowhisper.com/terminal/) | Scrollback, paragraphs, the level meter, and plain output |
| [Overlay and dictation](https://docs.vinowhisper.com/tray/) | The floating box, tray icon and shortcuts, dictation and how its text is typed, which desktops it works on, and why it is Rust |
| [Saved sessions](https://docs.vinowhisper.com/sessions/) | The `.vwsession` file, summaries, and transcribing a recording |
| [Speech models](https://docs.vinowhisper.com/models/) | Whisper and Canary 1B v2, other languages and translation |
| [Hardware](https://docs.vinowhisper.com/hardware/) | Device selection, the two model exports, and every way the NPU fails to appear |
| [Audio capture](https://docs.vinowhisper.com/audio/) | PipeWire vs PulseAudio, distro coverage, and what actually silences a capture (it is not the mute button) |
| [Latency](https://docs.vinowhisper.com/latency/) | Why captions trail the audio, the one knob that changes it, and why the wording drifts |
| [Debugging](https://docs.vinowhisper.com/debugging/) | `--record`, offline replay, and what `vinowhisper-doctor` measures |
| [Architecture](https://docs.vinowhisper.com/architecture/) | Socket activation and scale-to-zero, and how to stop it |
| [Development](https://docs.vinowhisper.com/development/) | Tests without the hardware, the build config, CI and releases |

## Other languages

French, German and Spanish work offline with Whisper's multilingual
`whisper-small`, and `--task translate` captions them in English. It is opt-in
(`vinowhisper-setup --language fr`) and **not benchmarked on the NPU**.
[Speech models](https://docs.vinowhisper.com/models/#other-languages) has the
details, and Canary 1B v2 as a second model.

## Honest limits

Worth saying before you install it, because the numbers above are all from one
machine:

- **Every benchmark here is n=1**, on one laptop, with early-silicon NPU
  drivers. The GPU and CPU fallbacks have run on this laptop's Xe3 iGPU and
  Core 5 320 and nowhere else, and 2 of 15 GPU model loads segfaulted inside
  OpenVINO's GPU plugin.
- **Captions trail the audio by roughly twice the cycle time.** That is
  inherent to a two-cycle commit policy, not a bug to be tuned away.
  [docs/latency.md](https://docs.vinowhisper.com/latency/)
  explains the one knob that moves it.
- **Wording drifts between cycles**, because each window is re-decoded with
  more right-context than the last. The stitcher hides most of it and not all.
- **The overlay needs wlr-layer-shell.** Tested on KDE Plasma 6.7 on hardware,
  and on headless Sway 1.9 in a VM with a stub in place of the model. GNOME
  does not offer the protocol, so there the overlay refuses to start and the
  terminal UI is the way in. Hyprland, niri and COSMIC should work and are
  untested. Compositors without a shortcuts portal bind the tray's commands
  instead ([examples](https://docs.vinowhisper.com/tray/#without-a-shortcuts-portal)).
- **Dictation types into whatever has focus.** Wayland does not say what that
  is, so it cannot check. It types through the remote-desktop portal, which KDE
  and GNOME have and Sway, niri and COSMIC do not, so dictation does not type there.
  [docs/tray.md](https://docs.vinowhisper.com/tray/#dictation)
  has the details.
- **Only Fedora's package names have been used for real.** The other seven
  families come from their package indexes. If one is wrong for yours, that is
  expected, and it is the fastest thing in this repo to fix. The PulseAudio
  capture backend has never run against a real PulseAudio server either.
- **The NPU export needs `transformers<5.4`.** 5.4.0 and up produce a model
  that compiles and then fails at `generate()` (bisected 2026-09-04). The
  `vinowhisper[export]` extra holds the pin. That version carries two open
  transformers CVEs, and both need you to export a malicious model repo, which
  the default `openai/whisper-small.en` is not.
  [docs/install.md](https://docs.vinowhisper.com/install/)
  has the bisect table.

## More

- [CONTRIBUTING.md](https://github.com/karanshukla/vinoWhisper/blob/main/CONTRIBUTING.md)
  and [docs/development.md](https://docs.vinowhisper.com/development/)
- [SECURITY.md](https://github.com/karanshukla/vinoWhisper/blob/main/SECURITY.md):
  what stays on the machine, and the local server's trust boundary
- [CHANGELOG.md](https://github.com/karanshukla/vinoWhisper/blob/main/CHANGELOG.md)

If you run this on hardware that isn't a Wildcat Lake laptop, I want the
report, working or not. An
[issue](https://github.com/karanshukla/vinoWhisper/issues) with
`vinowhisper-doctor --json` pasted into it is worth more than any benchmark I
can run here.

MIT licensed.
