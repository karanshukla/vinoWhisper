# Audio capture

How vinoWhisper captures desktop and microphone audio, handles system mute, and filters silence and non-speech audio.

## Distro support

Audio capture uses one of two backends, selected automatically:

- **PipeWire (`pw-record`):** Preferred, and the only backend that supports capturing a single application's playback stream (`--target`).
- **PulseAudio (`parec`):** Fallback backend. Supports full-sink and microphone capture, but not per-application `--target` (`--list-targets` lists monitor sources instead).

When `pactl` is available (including via `pipewire-pulse`), it is used to query the default sink and mute state; otherwise PipeWire's `default.audio.sink` metadata is used. On systems with both stacks installed, set `VINOWHISPER_CAPTURE_BACKEND=pipewire` or `VINOWHISPER_CAPTURE_BACKEND=pulseaudio` to override automatic detection.

Per-distro package mappings live in [`vinowhisper/distro.py`](../vinowhisper/distro.py) and are shared by `vinowhisper-setup` and `vinowhisper-doctor`:

| Family | Covered | Confidence |
|---|---|---|
| Fedora / RHEL / Alma / Rocky / Nobara | ✓ | Tested on hardware |
| Fedora Atomic (Silverblue, Kinoite, Bazzite) | ✓ (`rpm-ostree install`) | From docs, untested; `pw-record` ships in the image |
| Debian / Ubuntu / Pop / Mint / elementary / Raspbian | ✓ | From package index, untested |
| Arch / CachyOS / EndeavourOS / Manjaro / Garuda | ✓ | From package index, untested |
| openSUSE / SLES | ✓ | From package index, untested |
| openSUSE Aeon / MicroOS | ✓ (`transactional-update pkg install`) | From package index, untested |
| Void, Gentoo, Alpine | ✓ | From package index, untested |
| NixOS | ✓ | Configuration snippets (imperative `nix-env` installs do not persist) |
| Other distros | Degrades | Generic advice with a clear notice |

If a package name is incorrect for your distribution, please report it via [CONTRIBUTING.md](../CONTRIBUTING.md) or the distro-support issue template.

Because non-Fedora entries are untested, `vinowhisper-setup` and `vinowhisper-doctor` print commands for confirmation rather than running them silently. Printed commands include `-y` or `--needed` so they can be run as shown, and NPU entries link to Intel's upstream release as a fallback. Distribution detection matches `/etc/os-release` on `ID` first, falling back to `ID_LIKE` only if `ID` is unrecognized.

## System mute and volume behavior

System mute and volume sliders do **not** stop captions on PipeWire. Measured with `vinowhisper-doctor` on 2026-08-07 against a Chrome playback stream as a control:

| Sink state | Default sink monitor | Chrome stream | Monitor / app ratio |
|---|---|---|---|
| 100% volume | 0.01419 | 0.02040 | 0.70 |
| 20% volume | 0.05739 | 0.06610 | 0.87 |
| **Muted** | **0.08578** | **0.08781** | **0.98** |

Because the audio content varied across runs, the monitor-to-app ratio is the relevant metric: it stays high regardless of volume or mute state. On PipeWire, `monitor.channel-volumes` defaults to `false` (confirmed via `pw-dump`), so the default sink monitor taps audio pre-volume and pre-mute.

### What actually silences the capture

1. **Muting the application itself:** Using YouTube's mute button or muting Chrome in the desktop mixer causes the app to write silence into its PipeWire stream. `--target` cannot bypass this because the stream itself is silent (the node still appears in `--list-targets`, which lists existing nodes rather than active signal).
2. **Targeting `effect_output.bass_eq`:** Although `--target effect_output.bass_eq` appears in `--list-targets`, it sits downstream of both volume and mute. In the 2026-08-07 measurements, it dropped to 0.00034 RMS at 20% volume and 0.00000 while muted while the sink monitor read 0.05739 and 0.08578. Target a real application stream instead.
3. **Nothing playing:** `vinowhisper-doctor` reports explicitly when all probed targets read zero.

Quiet audio is normalized separately: every window is boosted toward `config.TARGET_RMS` (up to 20x gain) before inference, since typical web video sits around 0.014 RMS and Whisper accuracy drops on quiet input.

## Silence

Digital silence on a sink monitor measures roughly 0.0–0.004 RMS due to dither and EQ noise. The 0.002 RMS threshold (`config.SILENCE_RMS_THRESHOLD`) exists only to save NPU cycles on dead air; hallucinations are suppressed by the two-cycle commit policy and `collapse_repeats`. Raising this threshold risks dropping quiet speech.

Because brief pauses are normal, the terminal displays a "no signal" troubleshooting notice only after 45 continuous seconds of silence.

## Sound that is not speech (2026-11-13)

Whisper transcribes non-speech audio if passed through the RMS gate (especially after up to 20x normalization). Since 2026-11-13, the caption loop runs each captured chunk through Silero VAD (the same pinned model used for file imports in `vad.py`) on the capture thread:

- Windows containing less than `config.CAPTION_MIN_SPEECH_S` (0.3s) of detected speech are skipped, emitting a `Silence` event with `no_speech` set. The UI shows "no speech" instead of "no signal", and the 45-second dead-capture notice does not fire.
- Audio further than `TRIM_PAD_S` from detected speech is scaled by `1e-4` before gain normalization so background music or hum between sentences does not reach Whisper.

Pass `--no-speech-gate` to disable Silero VAD. If the detector fails to load, `Ready.warnings` reports the error and captioning falls back to decoding all audio above the RMS gate.

**Mumbling is not filtered:** Silero detects voice-like energy rather than intelligible words. Measured on `espeak-ng` audio (2026-11-13), reversed speech and low-passed speech (30–120 tap filters) all scored a median 1.00 (identical to clear speech) at thresholds of both 0.5 and 0.8, while noise and 120 Hz hum scored 0. Filtering unintelligible speech requires Whisper confidence scores (`no_speech_prob` and average log-probability), which OpenVINO's NPU static pipeline does not expose (though the custom decode loop in `whisper_step.py` has access to logits). Using `--language auto` with `--task translate` amplifies hallucinations on mumbled audio by forcing English output from a guessed language. Covered by unit tests; not yet verified in a live caption session.
