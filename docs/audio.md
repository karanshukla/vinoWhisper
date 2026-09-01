# Audio capture

What captures the audio on your machine, and the one question about it
that this project answered wrong for four days.

## Distro support

Audio capture works two ways, picked automatically:

- **PipeWire** (`pw-record`), preferred, and the only backend that can tap an
  individual application's playback stream, which is what `--target` is for.
- **PulseAudio** (`parec`), the fallback. Everything works except
  per-application capture; `--list-targets` there lists monitor sources and
  says why.

`pactl` is used for the default sink and mute state where present (on PipeWire
too, via `pipewire-pulse`); without it, PipeWire's own `default.audio.sink`
metadata answers the same question.

Package names per family live in one table in
[`vinowhisper/distro.py`](../vinowhisper/distro.py), so `vinowhisper-doctor` and
the wizard both speak your distro:

| Family | Covered | Confidence |
|---|---|---|
| Fedora / RHEL / derivatives | ✓ | Built and run here |
| Debian / Ubuntu / Pop / Mint | ✓ | From the package index, not from use |
| Arch / CachyOS / EndeavourOS / Manjaro | ✓ | Same |
| openSUSE / SLES | ✓ | Same |
| Void, Gentoo, Alpine | ✓ | Same |
| NixOS | ✓ | Configuration advice, not `nix-env` lines: imperative installs there do not persist |
| Anything else | degrades | Generic advice, and it says so |

**If a package name is wrong for your distro, that is expected, and it is the
fastest thing here to fix.** See [CONTRIBUTING.md](../CONTRIBUTING.md) or the
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
