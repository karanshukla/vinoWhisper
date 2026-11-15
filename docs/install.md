# Installing

The short version is in the [README](../README.md). This is what the
installer actually does, and the dependency pin behind it.

```bash
curl -fsSL https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/scripts/install.sh | bash
```

That installs [uv](https://docs.astral.sh/uv/) (a pinned release tarball checked
against a sha256 in the script, not astral.sh's install script, and only if you
have none), clones the latest `vX.Y.Z` release tag rather than `main`
(`--ref main` for unreleased code, `--ref v0.6.1` for a specific one), builds
the environment with `uv sync --locked`, so exactly what `uv.lock` pins and
never a fresh resolve, and hands over to `vinowhisper-setup`, which is where every
machine-specific decision happens: your capture tool, your NPU driver, the
model export your device needs. It prints every command before running it and asks first.
`--yes` answers yes to everything and `--dry-run` no to everything, and piped
stdin without `--yes` refuses rather than run sudo commands nobody agreed to.
A step that is already done says so and is skipped. It never invents a package
command where a distro has none (the NPU userspace driver, on most), and points
at Intel's releases instead. The `~/.local/bin` entries are symlinks into the
checkout, so they follow a `git pull`, and bash completion gets one link per
command, because bash-completion loads a file lazily on the first Tab for a
command of the same name.

The whole script is one `main` function called on its last line, so a
download cut short by the network is a syntax error that runs nothing, rather
than half an install. The release tag comes from `git ls-remote` on the
repository, not the GitHub API, so there is no rate limit and no JSON to
parse. Re-running it on an existing checkout moves that checkout to the
requested ref (detached), so a checkout installed from `main` goes back to the
latest release unless you pass `--ref main` again.

From a checkout, or to see what it would do without doing it:

```bash
git clone https://github.com/karanshukla/vinoWhisper && cd vinoWhisper
uv sync --extra export               # --extra export: the one-time model export
uv run vinowhisper-setup --dry-run   # the whole plan, nothing changed
uv run vinowhisper-setup             # for real, one prompt per step
```

## From PyPI

```bash
pip install vinowhisper
```

This works, and until 2026-08-31 it could not. The dependency set resolved
`openvino` from PyPI, where the builds that can construct the NPU static
Whisper pipeline did not exist, so a pip install would have succeeded and then
failed to load a model, which is worse than not shipping at all. Stable
2026.3.1 removed that constraint (see the version floor below).

What it gets you: the five commands and the Python dependencies. What it cannot
get you: an NPU driver or a model export. Run
`vinowhisper-setup` afterwards for those, exactly as the installer script would.
Re-run it after upgrading from 0.6.x or earlier too: the server moved from
`127.0.0.1:8099` to a Unix socket in `$XDG_RUNTIME_DIR`. Re-run it after
upgrading from 1.7 or earlier as well: the server no longer runs from systemd
units, and the wizard removes the old ones (the release installer's upgrade
does this itself).
The export tooling (optimum, and torch with it) is the `export` extra, which
the wizard offers to install when it gets to the model step;
`pip install --extra-index-url https://download.pytorch.org/whl/cpu 'vinowhisper[export]'`
does it up front. The export runs on the CPU, so that index's CPU torch is
enough; PyPI's Linux wheel brings CUDA libraries, several GB more.

**Python must be 3.11-3.13.** 3.14 made `functools.partial` a descriptor, which
breaks `optimum`'s `NORMALIZED_CONFIG_CLASS = SomeConfig.with_args(...)`
class-attribute idiom outright. Version-independent root cause, confirmed
2026-08-03 across every optimum/transformers pairing tried. `requires-python`
enforces it, so pip will refuse rather than install something broken.

## Image-based distros (Fedora Atomic, openSUSE Aeon)

Silverblue, Kinoite, Bazzite, Aurora, Bluefin and openSUSE Aeon keep `/usr`
read-only, so `dnf install` and `zypper install` cannot work there. Most of
vinoWhisper never touches `/usr`: uv, the checkout, the virtualenv (with a
uv-managed Python when the host's is 3.14), the model export, the commands
in `~/.local/bin`, and the overlay's launcher and autostart entries are all in
your home directory, and install the same way as anywhere else. Run them on the
host, not in a toolbox: the tray, the server and the audio they capture belong
to the host's session.

Host packages are the exception, and `vinowhisper-setup` and
`vinowhisper-doctor` detect the image (`/run/ostree-booted`, or a read-only
`/usr`, with or without `transactional-update`) and print the image's own tool:

| System | Install line | Then |
|---|---|---|
| Fedora Atomic | `sudo rpm-ostree install --idempotent <packages>` | reboot |
| openSUSE Aeon / MicroOS | `sudo transactional-update --non-interactive pkg install <packages>` | reboot |
| any other image-based system (SteamOS, Vanilla OS 2, Endless OS) | none: the doctor names the packages the image needs | |

The wizard says to reboot and re-run rather than reporting the step done,
since a layered package only exists after the reboot. `pipewire-utils`
(`pw-record`) is already in every Fedora Atomic desktop image, so the usual
packages to layer are `intel-npu-driver` or `intel-compute-runtime`.

Two things differ from a mutable install:

- **The NPU compiler needs no layering**: `ovfetch npu install` puts it in
  `~/.local/lib/ovfetch-npu` and vinoWhisper loads it from there (see
  [hardware.md](hardware.md#when-the-userspace-half-is-missing)). Doing it by
  hand instead, it goes in `/usr/local/lib64`, which is writable
  (`/var/usrlocal`), with a file in `/etc/ld.so.conf.d` so the loader finds
  it; the doctor prints those steps instead of the `/usr/lib64` ones, and if
  the host has no `dpkg-deb`, unpack the `.deb` in a toolbox, which shares
  your home directory.
- **Joining the `render` group** (for `/dev/accel`) needs the group copied
  into `/etc/group` first. The image keeps system groups in `/usr/lib/group`,
  and `usermod` only edits `/etc/group`, so it reports the group as missing:

  ```bash
  grep -E '^render:' /usr/lib/group | sudo tee -a /etc/group
  sudo usermod -aG render $USER
  ```

None of this has run on an image-based system yet: the commands are from
each distro's documentation, and the detection is covered by tests only.

## Flatpak

A `.flatpak` bundle holds everything: the server and every command, the tray and
overlay, the settings and sessions windows, a Python 3.13 of its own, OpenVINO, and
Intel's NPU and GPU drivers. The host needs only the kernel's NPU driver (`intel_vpu`)
and its firmware. Each release has it as `vinowhisper-x86_64.flatpak`, with its
checksum:

```bash
curl -fLO https://github.com/karanshukla/vinoWhisper/releases/latest/download/vinowhisper-x86_64.flatpak
curl -fLO https://github.com/karanshukla/vinoWhisper/releases/latest/download/vinowhisper-x86_64.flatpak.sha256
sha256sum -c vinowhisper-x86_64.flatpak.sha256
flatpak install --user vinowhisper-x86_64.flatpak
```

The runtime (`org.freedesktop.Platform` 26.08) comes from Flathub if it isn't
installed yet. Then get a speech model, once, in a terminal:

```bash
flatpak run --command=vinowhisper-setup io.github.karanshukla.vinowhisper
```

That is the same wizard as everywhere else, with less to do: it checks the device and
exports Whisper for it. The export needs optimum and torch, which the bundle does not
carry (they would triple it). The wizard offers to put them in a venv under the app's
own data, `~/.var/app/io.github.karanshukla.vinowhisper/data/vinowhisper/export-env`,
1.3 GB that only exports use; delete the folder once you have your models if you
want the space back. The windows' hints say "run vinowhisper-setup"; in the Flatpak
that is the command above. Or skip the export altogether: open vinoWhisper from the app
menu, and Settings → Speech downloads Canary 1B v2, which needs no export
([models.md](models.md)).

Open **vinoWhisper** from the app menu for the tray, the overlay and dictation.
The first dictation asks permission to type through the desktop's remote control, and
the desktop then notifies you when a typing session starts: at the first dictation, and
again after 5 minutes without one by default ([why](tray.md#dictation)).
**Start at login** in the tray's menu asks the desktop's Background portal, which
writes the login entry itself.

To update, use **Update** on the settings window's About page. It downloads the new
release's bundle and checksum, refuses the bundle unless the checksum matches, has the
host's `flatpak` install it where this one is installed, checks the installed build
is the release it asked for, and then stops the server and restarts the tray so both
run the new build. Reopen the settings window to run its new version too. A
system-wide install (`flatpak install --system`) asks for an administrator's password
first, as any system Flatpak install does. By hand, download and check the new bundle
as above and run the same `flatpak install --user vinowhisper-x86_64.flatpak`, then:

```bash
flatpak run --command=vinowhisper-server io.github.karanshukla.vinowhisper --stop
flatpak run io.github.karanshukla.vinowhisper restart
```

Only a release bundle (branch `stable`) updates itself; a bundle built from a checkout
(branch `master`) links to the release page instead. To move from one to a release,
run `flatpak uninstall io.github.karanshukla.vinowhisper//master` first (the models,
sessions and settings under `~/.var/app` stay) and install the release's bundle. To
build the bundle from a checkout:

```bash
flatpak install --user flathub org.flatpak.Builder org.freedesktop.Sdk//26.08 \
  org.freedesktop.Sdk.Extension.rust-stable//26.08
flatpak run --filesystem=$HOME org.flatpak.Builder --user --install --force-clean \
  --repo=repo build packaging/flatpak/io.github.karanshukla.vinowhisper.yml
flatpak build-bundle repo vinowhisper.flatpak io.github.karanshukla.vinowhisper
```

Every command is there under its usual name:
`flatpak run --command=vinowhisper-caption io.github.karanshukla.vinowhisper`, and the
same for `vinowhisper-doctor`, `vinowhisper-transcribe FILE` and the rest. An alias
saves the typing:

```bash
alias vw='flatpak run io.github.karanshukla.vinowhisper'        # vw toggle, vw dictate
```

What it is allowed, and why:

| Permission | For |
|---|---|
| `--device=all` | the NPU (`/dev/accel`), which nothing narrower exposes, and the GPU |
| `--filesystem=home` | recordings given to `vinowhisper-transcribe`, the paths `--record` and `--transcript` name, the sessions window's app menu entry, and reading the login entry the portal wrote |
| `--share=network` | downloading the models, and checking for a release |
| `--socket=wayland` | the overlay, and the settings and sessions windows |
| `--filesystem=xdg-run/pipewire-0` | `pw-record` and `pw-dump`: system audio, the microphone, and what is playing |
| `--socket=pulseaudio` | `pactl` and `parec`, through PipeWire's PulseAudio server or a real one |
| `--talk-name=org.kde.StatusNotifierWatcher` | the tray icon |
| `--talk-name=org.freedesktop.Notifications`, `--talk-name=org.a11y.Bus` | finished summaries, and screen reader announcements |
| `--talk-name=org.mpris.MediaPlayer2.*` | pausing players while you dictate |
| `--talk-name=org.freedesktop.FileManager1` | Show in folder |
| `--talk-name=org.freedesktop.Flatpak` | installing an update with the host's `flatpak`, and asking the host's `busctl` what is playing (`flatpak-spawn --host`) |

The global shortcuts, start at login, the file dialogs and the remote-desktop
fallback for typing are portals, which need no permission.

What differs from the native install:

- **Its settings, models and sessions are its own**, under
  `~/.var/app/io.github.karanshukla.vinowhisper` (`config/vinowhisper`,
  `data/vinowhisper/models`, `data/vinowhisper/transcripts`). To bring a native
  install's along, with neither running:

  ```bash
  app=~/.var/app/io.github.karanshukla.vinowhisper
  mkdir -p $app/config $app/data
  cp -a ~/.config/vinowhisper $app/config/
  cp -a ~/.local/share/vinowhisper $app/data/
  ```

  The exports are the same files, so nothing is exported again. vinoSearch finds the
  sessions in either place (karanshukla/vinoSearch, from the release after v11).
- **Don't run it beside a native install.** Both are `io.github.karanshukla.vinowhisper`
  to the desktop, on purpose: the portal keeps your shortcuts under that name, so they
  carry over. A native install's launcher in `~/.local/share/applications` hides the
  Flatpak's, so remove the native one first (`vinowhisper-upgrade --uninstall` keeps
  your sessions and settings).
- **Each command runs in a sandbox of its own**, so the tray's socket and the server's
  are in `$XDG_RUNTIME_DIR/app/io.github.karanshukla.vinowhisper`, the one part of the
  runtime directory they share. The server is started in its own sandbox too
  (`flatpak-spawn`), so closing the window that happened to start it does not stop it.
  Its log is `~/.var/app/io.github.karanshukla.vinowhisper/.local/state/vinowhisper/server.log`.
- **The launcher, icons and `.vwsession` file type come with it**, exported by Flatpak;
  `vinowhisper-tray --install` has nothing to write. The Sessions page's app menu
  switch still writes its entry in `~/.local/share/applications`, which
  `vinowhisper-tray --uninstall` removes with the login entry; `flatpak uninstall`
  alone leaves those two files.
- **What was playing** is read with the host's `busctl` (`flatpak-spawn --host`): the
  runtime has none.
- `vinowhisper-setup` installs no packages there. Its advice about the kernel driver
  and the `render` group names the host's distro, read from `/run/host/os-release`.
- On sway, Hyprland and the like, bind
  `flatpak run io.github.karanshukla.vinowhisper toggle` and `... dictate-press` /
  `dictate-release` as in [tray.md](tray.md).

**What the sandbox costs**, measured 2026-11-14 on a 4-core cloud Xeon, CPU device,
the bundle's own binaries inside the sandbox and outside it:

| | In the Flatpak | Same binaries, no sandbox |
|---|---|---|
| A 12 s window decoded (median of 8) | 0.86 s | 0.86 s |
| The server from nothing until its socket accepts | 0.34 s | 0.25-0.30 s |
| `flatpak run` of a command, before it starts | 0.07 s | |

Decoding is the same. A cold start pays about 60 ms for the server's own sandbox, and
each `flatpak run` about 70 ms; the tray starts its caption and dictation processes
inside its own sandbox, so neither pays that. The bundle's Python is compiled to
bytecode at build time, standard library included: `/app` is read-only, so anything
left uncompiled would be compiled again at every start (17 modules were, before).
None of this is an NPU measurement.

**Not yet run on a desktop.** What was run, 2026-11-14, in a cloud container with
Flatpak 1.14.6, no NPU, no GPU and no audio server:

- the build, and `packaging/flatpak/smoke.sh` on the installed bundle;
- `vinowhisper-setup --yes --device CPU` inside it: the export venv, the pinned
  Whisper source downloaded and checked, and the stateful export;
- `vinowhisper-transcribe` on 13 s of synthesised speech: the server started in a
  sandbox of its own, decoded on the CPU at 9x real time and wrote the session, and
  `vinowhisper-server --stop` from a third sandbox stopped it in half a second;
- `vinowhisper-flatpak-update` between two `stable` bundles served from a local
  directory: a bundle that did not match its `.sha256` was refused and nothing
  changed; the right one was installed by the host's `flatpak`, the installed commit
  changed, and the running server was stopped;
- on headless Sway 1.9: the tray started, `toggle`, `hide`, `shortcuts`, `restart` and
  `quit` reached it from other sandboxes, the overlay drew the caption process's
  error (no audio server), and the settings window opened.

Untested anywhere: the NPU and GPU from inside the sandbox, capture, the tray icon,
the shortcut and Background portals, and Plasma. Two
things to check first on a desktop. Flatpak 1.15.6 and later give a sandboxed app a
restricted Wayland connection (`wp_security_context_v1`), which the container's 1.14.6
does not, and a compositor may withhold `wlr-layer-shell` (the overlay; Sway does) or
the data-control clipboard from it. KWin 6.8 withholds data-control and fake-input but not
layer-shell (measured 2026-11-14), which is why dictation types through the
remote-desktop portal; that typing was measured on Plasma 6.8.2 the same day. And `restart` starts the tray through
Flatpak's portal, which needs the session's D-Bus environment to hold
`WAYLAND_DISPLAY`: Plasma and GNOME put it there, and a Sway config needs the usual
`exec dbus-update-activation-environment --systemd WAYLAND_DISPLAY XDG_CURRENT_DESKTOP`.

`flatpak uninstall io.github.karanshukla.vinowhisper` removes it; add `--delete-data`
to delete its models, sessions and settings too.

## The OpenVINO version floor

`openvino>=2026.4.1`. 2026.3.1 is the first *stable* release that can build
the NPU static Whisper pipeline, and was the floor until 1.6.1. The floor is now
2026.4.1 because vinoSpeak and vinoPop pin exactly that release, and every
process on one NPU should run the same OpenVINO (an NPU fault resets the device
for all of them). 2026.4.1 also hardens the NPU's import of compiled blobs
against malformed or forged cache files. It is a floor rather than a pin, so a
later release still resolves; bump the other apps in step when it does.

From 2026-08-03 to 2026-08-31 this project pinned nightly wheels from
`storage.openvinotoolkit.org`, with prereleases allowed globally, because
stable 2026.2.1 could not build that pipeline at all: its
`pipeline_static.cpp` pattern-matcher did not recognise the `optimum-intel`
export's SDPA attention-mask node shape, and
`OPENVINO_ASSERT(!self_attn_nodes.empty())` failed. That pin was the project's
standing dependency risk, since upstream prunes nightly builds on its own
schedule.

Re-measured 2026-08-31 on the Wildcat Lake NPU, against the same
`--disable-stateful` export, with `WhisperPipeline(..., STATIC_PIPELINE=True)`:

| Version | Source | Pipeline build | `generate()` |
|---|---|---|---|
| 2026.3.1 | PyPI stable | 2.0s | ok |
| 2026.4.0.dev20260805 | nightly | 2.3s | ok |
| 2026.5.0.dev20260831 | nightly | 1.9s | ok |

Stable caught up, so the nightly index, the `prerelease = "allow"` policy and
the `[tool.uv.sources]` routing are all gone and these resolve from PyPI like
anything else. `.github/workflows/deps-canary.yml` still runs the full resolve
weekly, off the pull-request path, but now for ordinary upstream churn rather
than for wheels aging out from under the lock.

**`STATIC_PIPELINE=True` is not optional and does not degrade.** Omitting it
sends the NPU down the generic stateful path, which fails with `Stateful models
without 'beam_idx' input are not supported in StatefulToStateless
transformation`. That reads like a bad export and is not one.

## The model export, and what verifies it

Exporting downloads ~1GB from Hugging Face and converts it to OpenVINO IR that
then runs on your hardware. The download comes first and is checked first:
`vinowhisper/model_sources.json` pins the repository to a commit and records
the sha256 of each file the export reads, `python -m vinowhisper.source`
fetches exactly those into `models/source/` under the data directory, and any
difference fails before `optimum-cli` starts. The export then runs from that
directory with `HF_HUB_OFFLINE=1`. optimum-cli has no `--revision` flag
(optimum-intel 2.2.0), which is why the snapshot is local rather than in the
Hugging Face cache. A `--model` with no pinned source is exported unchecked,
with a warning.

`vinowhisper/model_digests.json` pins the sha256 of
every file in the export this project has actually run, and both
`scripts/convert_model.sh` and `vinowhisper-setup` check what came down against
it. `vinowhisper-doctor` re-checks it on demand, at about 1.2s for 1.5GB, and
only for an export that is present and the right shape for its device, since
"hashes don't match" is noise next to "wrong export entirely".

The pin is on the exported IR, not on the upstream safetensors, because the IR
is what `WhisperPipeline` loads and the export is not a pure function of the
weights. Six statuses, and only three of them stop anything:

| Status | Means | Blocks setup |
|---|---|---|
| `verified` | every pinned file matches | |
| `unpinned` | no pin for this model and variant | no |
| `drift` | bytes and export toolchain both moved | no |
| `mismatch` | the *pinned* toolchain produced different bytes | yes |
| `incomplete` | pinned files are missing, so the export is partial | yes |
| `known_bad` | exported by a toolchain measured to produce a broken export | yes |

An unpinned export warning is not a problem to fix. It is what
`--model openai/whisper-base.en` looks like, and what the stateful export looks
like until someone produces one. Re-pin with
`./scripts/update_digests.py --variant npu` once you trust an export, and
commit the diff.

**The export is bit-reproducible, which is what makes any of this work.**
Measured 2026-09-04: two independent `optimum-cli` exports of
whisper-small.en on the same toolchain produced all 16 files byte-identical.
Across toolchains it is not: against the 2026-08-03 export
(OpenVINO 2026.2.1, optimum-intel 2.0.0, transformers 5.0.0), an export under
OpenVINO 2026.3.1 / optimum-intel 2.1.0 / transformers 5.5.4 changed 9 of 16
files, including both decoder `.bin` weights. `openvino_encoder_model.bin` came
out identical across both. That is why drift is reported separately from a
real mismatch, and the versions are read out of the export's own `rt_info`
block rather than from whatever happens to be installed. That makes `drift`
the export's own claim about itself: something able to rewrite the export can
rewrite every `rt_info` block to match and get `drift` instead of `mismatch`.
The source check is what guards the download; this one catches accidents.

That block is read from every `.xml` in the export and merged, and any version
two files disagree on makes the answer unknown, never fine: reading just one
file would let a single edited graph buy the softer `drift` verdict (editing
all of them consistently still does). The
digest covers every file, not just the weights, because `generation_config.json`
decides how decoding behaves and `tokenizer.json` decides the text. A missing
or truncated pin file downgrades to `unpinned` rather than breaking every
export. Verification is deliberately not part of loading the model, since
hashing takes about 1.2s and would land on every cold start.

`known_bad` entries come in two shapes: an exact combination measured broken,
or a floor (`at_least`, as for transformers 5.4.0). A version the export does
not report never satisfies a floor, and `update_digests.py` rewrites the
hashes without touching the list.

## transformers 5.4.0 breaks the NPU export

**Bisected on hardware 2026-09-04.** Export with `transformers<5.4`. Anything
from 5.4.0 on produces a Whisper decoder the NPU static pipeline compiles and
then cannot run:

```
RuntimeError: Port for tensor name cache_position was not found.
  (src/inference/src/cpp/infer_request.cpp:191)
```

It fails at `generate()`, not at load, so nothing complains until the first
transcription.

The bisect held optimum-intel 2.1.0, optimum 2.3.0, openvino 2026.3.1,
openvino-genai 2026.3.1.0 and torch 2.13.0 fixed, exported whisper-small.en
with `--disable-stateful` in a clean venv per version, and loaded each on the
NPU:

| transformers | Pipeline build | `generate()` |
|---|---|---|
| 5.0.0 | ok | 0.96s |
| 5.2.0 | ok | 0.86s |
| 5.3.0 | ok | 0.71s |
| 5.4.0 | ok | **fails** |
| 5.5.4 | ok | **fails** |

Two control runs rule out the rest of the stack. optimum-intel 2.1.0 with
transformers 5.0.0 works, so the optimum pair is not at fault; the full
2026-08-03 package set re-run under openvino 2026.3.1 also works, so the
runtime is not either.

**The mechanism is a tensor name.** `cache_position` appears exactly once in
each transformers 5.3.0 decoder graph, on the output port of
`__module.model.model.decoder/aten::arange/Range`, and zero times in the 5.4.0
graphs. It is neither a model input nor a model output in either export, so the
static pipeline is resolving an internal traced tensor by name, and 5.4.0
stopped emitting that name. The exported input and output signatures are
otherwise identical between the two.

Since 2026-09-12 the export's dependencies are their own extra,
`vinowhisper[export]`, which holds `transformers<5.4`. Before that a fresh
install resolved 5.5.4 and `vinowhisper-setup` failed at the model step. The
extra is separate because only the export uses optimum and transformers, and
optimum brings torch with it, while the runtime imports none of them.
`vinowhisper-setup` offers to install it when it needs to export, as
`uv sync --extra export` in a checkout and `pip install 'vinowhisper[export]'`
otherwise, either way with CPU torch (`--torch-backend cpu` under uv, PyTorch's
CPU index under pip).

The pin's `known_bad` entry still carries a floor at transformers 5.4.0, for
an export made some other way, so `vinowhisper-setup` and `convert_model.sh`
report a `known_bad` export rather than handing over a model that fails later.
The stateful (CPU/GPU) export is unaffected: it builds and decodes under 5.5.4
(measured 2026-09-12).

**Raising the cap does not upgrade anything (2026-09-20).** Every released
optimum-intel, 2.2.0 included, requires `transformers<5.6,>=4.51`. Ask for
anything past that and the resolver satisfies it by backtracking optimum to a
pre-transformers-5.x release instead, at which point `optimum-cli` fails on
import rather than at export time. Two open transformers advisories,
GHSA-fgcw-684q-jj6r (fixed in 5.5.0) and GHSA-xrqw-3rrv-vx5w (fixed in
5.10.0), are unreachable for that reason and the bisection above. Nothing at
runtime imports transformers; the only call site is the one-time
`optimum-cli export openvino` in `scripts/convert_model.sh`, against a
hardcoded `openai/whisper-small.en` unless you pass your own `--model`.

## The desktop overlay (optional)

`vinowhisper-setup --gui` installs `vinowhisper-tray`, a floating caption box
that also does dictation, with a tray icon and global shortcuts. It downloads the binary from the
GitHub release and checks it against the sha256 pinned in the Python package,
so no Rust toolchain is needed. It is never on PyPI; from a checkout it can be
built with cargo instead (`./scripts/install.sh --gui`). See [tray.md](tray.md).

## NPU platform data (optional)

`vinowhisper-setup --ovfetch` installs [ovfetch](https://github.com/karanshukla/ovfetch),
which gives `vinowhisper-doctor` Intel's per-platform NPU driver data: the
first driver verified on your NPU, and the OpenVINO range recorded as working
with the driver you have. Setup offers it only when there is an Intel NPU,
downloads the static release binary and checks it against the sha256 pinned in
this package, and leaves an ovfetch that is already current alone. Nothing
needs it unless your distro's driver package has no NPU compiler (Fedora's
`intel-npu-driver` 1.32.0): then `ovfetch npu install` (0.6.0 or newer) is
the fix, and vinoWhisper loads what it installs. Otherwise the doctor just has
two fewer lines. See [hardware.md](hardware.md#platform-data-from-ovfetch).

## Uninstalling

```bash
vinowhisper-upgrade --uninstall
```

`vinowhisper-upgrade` is the copy of the release installer that a release
install keeps. From a checkout install, which has none, pipe the installer in
instead; it downloads nothing for this:

```bash
curl -fsSL https://github.com/karanshukla/vinoWhisper/releases/latest/download/install.sh | bash -s -- --uninstall
```

It removes, in this order:

- the server: `vinowhisper-server --stop`, and the systemd units versions up to
  1.7 ran it from, if they are still there (`systemctl --user disable --now`,
  both unit files from `~/.config/systemd/user`, `daemon-reload`);
- the tray (`vinowhisper-tray quit`) and what `vinowhisper-tray --install
  --autostart` wrote: launcher, settings and sessions entries, icons, the
  `.vwsession` file type and the login entry;
- the commands: `uv tool uninstall vinowhisper`, the `~/.local/bin` links a
  checkout's `vinowhisper-setup` made into its `.venv`, the bash completion
  links, and `vinowhisper-tray`, `vinowhisper-gui` and `vinowhisper-upgrade`;
- every model, with `$XDG_DATA_HOME/vinowhisper/models` (Whisper exports and
  their source download, Canary, SmolLM3, Silero VAD) and all of
  `$XDG_CACHE_HOME/vinowhisper` (SmolLM3's compiled NPU blobs, export scratch);
- `scripts/install.sh`'s own clone at `~/.local/share/vinowhisper/src`, unless
  it has uncommitted changes, commits on a local branch that no remote has, or
  a stash. It is then kept and the reason printed. A checkout you cloned
  yourself stays.

It keeps what is yours: saved sessions and transcripts
(`~/.local/share/vinowhisper/transcripts`), settings (`~/.config/vinowhisper`),
the summariser's state (`~/.local/state/vinowhisper`) and any document you
exported. Tools other apps use stay too: uv, ovfetch and its
`~/.local/lib/ovfetch-npu`, and the NPU driver's `~/.cache/ze_intel_npu_cache`.
Delete those yourself if nothing else needs them.

To free the space without uninstalling, the settings window deletes each model:
Whisper and Canary on the Speech page, SmolLM3 on the Cleanup page. From a
terminal, `vinowhisper-whisper model delete`, `vinowhisper-canary model delete`
and `vinowhisper-cleanup model delete` do the same.

## Pinning the terminal on top

The overlay above stays on top by itself. For the terminal UI instead: the
status bar is Rich in an ordinary terminal, so keeping it above other
windows is a window-manager job, not the app's. On KWin: System Settings >
Window Management > Window Rules, match the terminal window, set Keep Above
Other Windows to Force/Yes, plus Skip Taskbar and Skip Pager if you want it out
of the way. No titlebar and a small fixed size make it read like an overlay
rather than a terminal.
