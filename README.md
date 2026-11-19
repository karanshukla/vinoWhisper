<p align="center">
  <img src="https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/docs/assets/vinowhisper.svg" width="112" alt="The vinoWhisper mark: a dark caption box with green voice bars turning into lines of text">
</p>

# vinoWhisper

[![PyPI](https://img.shields.io/pypi/v/vinowhisper?logo=pypi&logoColor=white)](https://pypi.org/project/vinowhisper/)
[![Python](https://img.shields.io/pypi/pyversions/vinowhisper)](https://pypi.org/project/vinowhisper/)
[![License](https://img.shields.io/badge/license-MIT-blue)](https://github.com/karanshukla/vinoWhisper/blob/main/LICENSE)
[![CI](https://github.com/karanshukla/vinoWhisper/actions/workflows/ci.yml/badge.svg)](https://github.com/karanshukla/vinoWhisper/actions/workflows/ci.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**Local live captions and dictation for Linux, running on an Intel NPU.** All audio capture and transcription happens on your machine with no cloud services or accounts. Captions appear in your terminal or in an optional floating overlay above other windows, and the overlay also supports hold-to-talk and hands-free dictation.

<img width="1237" height="530" alt="vinoWhisper captioning a video, with the status bar pinned at the bottom" src="https://github.com/user-attachments/assets/f263eabf-f1f4-4ab2-9b68-bc50eaf92ea0" />

```bash
pip install vinowhisper && vinowhisper-setup && vinowhisper-caption
```

## Background

Live captioning is a continuous, latency-sensitive workload that fits well on a 16 TOPS Intel NPU. Offloading transcription to the NPU leaves the CPU and GPU free and avoids spinning up the fan. vinoWhisper runs `openvino-genai`'s `WhisperPipeline` with `device="NPU"` (similar in approach to [vinoAuthFace](https://github.com/karanshukla/vinoAuthFace)).

Measured on a Wildcat Lake laptop (stepping A0) with `whisper-small.en` on the NPU (2026-09-12):

| Metric | Result |
|---|---|
| `generate()` per 12s window (default) | **0.70s** mean, 0.85s p90 |
| Caption lag behind audio | **~1.5s** at best |
| First streamed token | **0.204s** |
| Idle resource usage after stopping | **0** (server exits automatically) |

The Intel iGPU decodes the same 12s window in 0.95s and the CPU in 2.30s, so both fallbacks are usable with higher latency.

## Key features

- **Append-only transcript:** Words are committed only after two overlapping windows agree on them (LocalAgreement-2), so printed text stays in terminal scrollback without being rewritten. A dimmed `hearing…` line shows pending words that have not yet been confirmed. See [Latency](https://docs.vinowhisper.com/latency/).
- **On-demand server:** Loading the model onto the NPU takes 10–30s, so a background server holds it across sessions. The first client starts the server on a private Unix socket, and the server exits automatically after 5 minutes of inactivity. See [Architecture](https://docs.vinowhisper.com/architecture/).
- **Verified model downloads:** The Hugging Face source repository is pinned to a commit with per-file SHA-256 verification, and the exported OpenVINO IR is checked against `vinowhisper/model_digests.json`. See [SECURITY.md](https://github.com/karanshukla/vinoWhisper/blob/main/SECURITY.md).
- **Explicit hardware fallback:** Device selection tries NPU, then GPU, then CPU. Any fallback is reported in the server log, `/health`, `vinowhisper-doctor`, and as a red status-bar border. See [Hardware](https://docs.vinowhisper.com/hardware/).
- **Distro-aware diagnostics:** Error messages and `vinowhisper-doctor` print remediation commands using your distribution's package names.
- **Optional native overlay:** `vinowhisper-tray` is a 6.3 MB Rust binary that floats captions above other windows, including fullscreen video. It links only libc and adds no dependencies to the Python package.
- **Dictation in the overlay:** Hold `Meta+H`, speak, and release to type into the focused window. An optional local LLM cleanup pass on the NPU can remove fillers, stammers, and spoken edits ("scratch that") strictly by deleting words—it never inserts or rewords text. See [Overlay and dictation](https://docs.vinowhisper.com/tray/).

## Install

```bash
curl -fsSL https://github.com/karanshukla/vinoWhisper/releases/latest/download/install.sh | bash
```

The installer verifies the release wheel, installs it with [uv](https://docs.astral.sh/uv/), and runs `vinowhisper-setup`, which prints every command and asks for confirmation before running it.

| Goal | Command |
|---|---|
| Install the overlay and dictation | `vinowhisper-setup --gui` |
| Install via pip instead of the script | `pip install vinowhisper && vinowhisper-setup` (Python 3.11–3.13) |
| Install the Flatpak bundle | `flatpak install --user vinowhisper-x86_64.flatpak` from the release ([Flatpak test status](https://docs.vinowhisper.com/install/#flatpak)) |
| Upgrade | **Check for updates** in Settings → About, or `vinowhisper-upgrade` |
| Uninstall | `vinowhisper-upgrade --uninstall` (removes models; keeps sessions and settings) |

`pip install` installs the Python commands, while `vinowhisper-setup` handles the NPU driver checks and model export. See [Installing](https://docs.vinowhisper.com/install/) for details on each installation method.

## Requirements

| Requirement | Details |
|---|---|
| **OS** | Linux (developed on Fedora 45 / KDE Plasma 6 / Wayland; see [Supported distros](#supported-distros)) |
| **Audio** | PipeWire (`pw-record`) or PulseAudio (`parec`), detected automatically |
| **Accelerator** | Intel NPU for the benchmark numbers above; Intel GPU and CPU fallbacks work with higher latency; AMD NPUs are detected as unsupported (no OpenVINO plugin) |
| **Python** | 3.11 to 3.13 ([Python 3.14 cannot export the model](https://docs.vinowhisper.com/install/)) |
| **Disk** | ~1.5 GB for the exported model |
| **Overlay** (optional) | A Wayland compositor supporting `wlr-layer-shell` (KDE Plasma 6, Sway, Hyprland, niri, COSMIC; not GNOME) |

The Intel NPU requires a userspace compiler library that Linux distributions do not fully package; `vinowhisper-doctor` identifies which component is missing. See [docs/hardware.md](https://docs.vinowhisper.com/hardware/) for troubleshooting NPU detection.

## Supported distros

`vinowhisper-setup` and `vinowhisper-doctor` read `/etc/os-release` and format install commands for eight distribution families: Fedora, Debian, Arch, openSUSE, Gentoo, Void, Alpine, and NixOS (including derivatives and image-based variants like Fedora Atomic and openSUSE Aeon). **Only Fedora has been tested directly.** See the [supported distros table](https://docs.vinowhisper.com/install/#supported-distros) for per-family NPU driver details.

## Commands

```bash
vinowhisper-caption                       # caption system audio
vinowhisper-caption --source mic          # caption microphone input
vinowhisper-caption --list-targets        # capture one app instead of the whole sink
vinowhisper-caption --debug               # per-cycle timings, levels, raw transcript
vinowhisper-caption --record ~/sess       # save session audio and events for replay
vinowhisper-caption --plain > out.txt     # plain output without status bar (implied when piped)
vinowhisper-caption --json                # one JSON event per line (used by the overlay)

vinowhisper-transcribe lecture.mp3        # transcribe an audio/video file into a saved session

vinowhisper-setup --language fr           # set up multilingual model (fr, de, es, auto)
vinowhisper-setup --language auto --task translate   # caption foreign speech in English

vinowhisper-tray                           # start the floating overlay and tray icon
vinowhisper-tray toggle                    # show/hide overlay (same as Meta+Alt+C)
vinowhisper-tray dictate                   # start/stop dictation (same as holding Meta+H)
vinowhisper-tray --install --autostart     # install launcher entry and autostart at login
                                           #   (or enable "Start at login" in the tray menu)

vinowhisper-setup                         # guided setup (idempotent, safe to re-run)
vinowhisper-setup --dry-run               # print planned steps without making changes
vinowhisper-server --stop                 # unload the model immediately; next client restarts it
vinowhisper-setup --gui                   # install only the optional overlay and dictation
vinowhisper-setup --ovfetch               # install ovfetch for NPU driver/platform diagnostics
vinowhisper-upgrade --uninstall           # remove app and models (keeps sessions and settings)

vinowhisper-doctor                        # check devices, models, digests, audio, and live levels
vinowhisper-doctor --json                 # JSON diagnostic output for bug reports
vinowhisper-doctor --no-probe             # skip the 2s-per-target live level probe

vinowhisper-replay ~/sess --restitch      # re-run stitcher offline against recorded transcripts
vinowhisper-replay ~/sess --sweep 8,12,20 # benchmark window sizes against recorded audio
```

## Documentation

| Page | Description |
|---|---|
| [Installing](https://docs.vinowhisper.com/install/) | Installers, OpenVINO version floor, digest verification, Flatpak, and terminal window pinning |
| [Terminal captions](https://docs.vinowhisper.com/terminal/) | Scrollback behavior, paragraph breaks, level meter, transcripts, and plain output |
| [Overlay and dictation](https://docs.vinowhisper.com/tray/) | Floating caption box, tray icon, shortcuts, dictation, local LLM cleanup, compositor support, and Rust implementation |
| [Saved sessions](https://docs.vinowhisper.com/sessions/) | `.vwsession` bundle format, background summaries, file transcription, and exporting |
| [Speech models](https://docs.vinowhisper.com/models/) | Whisper and Canary 1B v2, multilingual support, translation, and custom step decoding |
| [Hardware](https://docs.vinowhisper.com/hardware/) | Device selection, NPU vs. stateful exports, driver checks, and NPU troubleshooting |
| [Audio capture](https://docs.vinowhisper.com/audio/) | PipeWire vs. PulseAudio, distro support, mute behavior, and Silero VAD gating |
| [Latency](https://docs.vinowhisper.com/latency/) | Cycle-time latency, window sizing, stitcher overlap rules, fast speech, and language switches |
| [Debugging](https://docs.vinowhisper.com/debugging/) | `--record`, offline replay, and `vinowhisper-doctor` |
| [Architecture](https://docs.vinowhisper.com/architecture/) | On-demand server lifecycle, scale-to-zero behavior, Unix socket, and power measurements |
| [Development](https://docs.vinowhisper.com/development/) | Hardware-free test setup, local builds, packaging, CI workflows, and releases |

## Other languages

French, German, and Spanish are supported offline via Whisper's multilingual `whisper-small` model, and `--task translate` captions them in English. Enable it with `vinowhisper-setup --language fr` (multilingual Whisper has **not been benchmarked on the NPU**). See [Speech models](https://docs.vinowhisper.com/models/#other-languages) for details, including Canary 1B v2 support for 25 European languages.

## Limitations

- **Single-machine benchmarks (n=1):** All numbers above were measured on one Wildcat Lake laptop with early-silicon NPU drivers. GPU and CPU fallbacks were tested only on its Xe3 iGPU and Core 5 320, where 2 of 15 GPU model loads segfaulted inside OpenVINO's GPU plugin.
- **Two-cycle latency floor:** Captions trail audio by roughly twice the cycle time because two consecutive windows must agree before committing a word. See [docs/latency.md](https://docs.vinowhisper.com/latency/) for tuning `--window`.
- **Cross-window wording drift:** Re-decoding overlapping windows with changing right-context causes occasional phrasing changes; the stitcher suppresses most but not all of them.
- **Overlay requires `wlr-layer-shell`:** Tested on KDE Plasma 6.7 on hardware and headless Sway 1.9 in a VM with a stubbed model. GNOME does not expose `wlr-layer-shell`, so the overlay refuses to start there (use the terminal UI instead). Hyprland, niri, and COSMIC are untested. Compositors without a global shortcuts portal can bind `vinowhisper-tray` commands directly ([examples](https://docs.vinowhisper.com/tray/#without-a-shortcuts-portal)).
- **Dictation focus and portal requirements:** Wayland does not expose which window has focus, so dictation types into whichever window is currently focused. Typing uses the `RemoteDesktop` portal (available on KDE and GNOME, but not Sway, niri, or COSMIC). See [docs/tray.md](https://docs.vinowhisper.com/tray/#dictation).
- **Untested non-Fedora distros and PulseAudio:** Package names for the other seven distro families come from their package indexes and may contain mistakes. The PulseAudio (`parec`) capture backend has not been run against a live PulseAudio server.
- **NPU export requires `transformers<5.4`:** Version 5.4.0 and later export a Whisper decoder that compiles on the NPU but fails at `generate()` (bisected 2026-09-04). The `vinowhisper[export]` extra holds this pin. `transformers` 5.3.0 has two open CVEs that require exporting a malicious model repository; the default pinned `openai/whisper-small.en` is unaffected. See [docs/install.md](https://docs.vinowhisper.com/install/) for the bisect table.

## More

- [CONTRIBUTING.md](https://github.com/karanshukla/vinoWhisper/blob/main/CONTRIBUTING.md) and [docs/development.md](https://docs.vinowhisper.com/development/)
- [SECURITY.md](https://github.com/karanshukla/vinoWhisper/blob/main/SECURITY.md): local data handling and server trust boundaries
- [CHANGELOG.md](https://github.com/karanshukla/vinoWhisper/blob/main/CHANGELOG.md)

Reports from other hardware and distributions are welcome—open an [issue](https://github.com/karanshukla/vinoWhisper/issues) with the output of `vinowhisper-doctor --json`.

MIT licensed.
