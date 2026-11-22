# Installing

Quick install commands are in the [README](../README.md). This page covers what each installer does, supported distributions, Flatpak packaging, OpenVINO/transformers version constraints, digest verification, and uninstallation.

## Release installer

```bash
curl -fsSL https://github.com/karanshukla/vinoWhisper/releases/latest/download/install.sh | bash
```

The script verifies the latest release wheel against `SHA256SUMS`, installs it with [uv](https://docs.astral.sh/uv/) (downloading a hash-pinned `uv` release if missing), and runs `vinowhisper-setup`. The setup wizard handles machine-specific steps (audio capture tools, Intel NPU/GPU drivers, and the model export matching your device), printing each command and asking for confirmation before running it.

To add the optional desktop overlay and dictation, run `vinowhisper-setup --gui` (see [The desktop overlay (optional)](#the-desktop-overlay-optional)).

## Upgrading

Use **Check for updates** under Settings → About, or run `vinowhisper-upgrade`. Both install the new release wheel, update the overlay and settings window if installed, and restart the server while keeping your exported models.

## Installing from a git checkout

```bash
curl -fsSL https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/scripts/install.sh | bash
```

This bootstrap script:

- Installs a hash-pinned `uv` tarball if `uv` is not present.
- Clones the latest `vX.Y.Z` release tag discovered via `git ls-remote` (pass `--ref main` for unreleased code or `--ref v0.6.1` for a specific tag; re-running moves an existing checkout back to the requested detached ref).
- Installs locked dependencies via `uv sync --locked` against `uv.lock`.
- Hands off to `vinowhisper-setup` (`--yes` accepts all prompts, `--dry-run` prints the plan without changes, and piped `stdin` without `--yes` exits rather than executing `sudo` commands unattended). Completed steps are skipped.
- Symlinks CLI commands into `~/.local/bin` (so they track `git pull`) and installs per-command `bash-completion` symlinks.

The entire script runs inside a single `main` function invoked on the final line so truncated downloads exit safely without partial execution.

To clone and run setup manually:

```bash
git clone https://github.com/karanshukla/vinoWhisper && cd vinoWhisper
uv sync --extra export               # includes one-time model export dependencies
uv run vinowhisper-setup --dry-run   # preview plan without changing anything
uv run vinowhisper-setup             # interactive setup
```

## Installing from PyPI

```bash
pip install vinowhisper
```

(Supported since OpenVINO 2026.3.1 landed on PyPI on 2026-08-31; earlier releases required OpenVINO nightly wheels.)

`pip` installs the CLI commands and Python runtime dependencies, but does not configure NPU drivers or export the model; run `vinowhisper-setup` afterward. Also re-run `vinowhisper-setup` if upgrading from 0.6.x or earlier (server moved from `127.0.0.1:8099` to a Unix socket in `$XDG_RUNTIME_DIR`) or from 1.7 or earlier (removes legacy systemd user units; `vinowhisper-upgrade` handles this automatically).

Model export dependencies (`optimum`, `transformers`, and CPU `torch`) are in the `export` extra. `vinowhisper-setup` offers to install it during the model step, or you can install it up front using PyTorch's CPU wheel index (avoiding multi-GB CUDA wheels):

```bash
pip install --extra-index-url https://download.pytorch.org/whl/cpu 'vinowhisper[export]'
```

**Python 3.11–3.13 required:** Python 3.14 turned `functools.partial` into a descriptor, which breaks `optimum`'s `NORMALIZED_CONFIG_CLASS = SomeConfig.with_args(...)` pattern across all tested `optimum`/`transformers` versions (confirmed 2026-08-03). `requires-python` blocks installation on 3.14.

## Supported distros

`vinowhisper-setup` and `vinowhisper-doctor` read `/etc/os-release` (`ID` first, then `ID_LIKE`) and format install commands for eight distribution families:

| Family | Derivatives | NPU driver | Test status |
|---|---|---|---|
| Fedora | RHEL, CentOS, Alma, Rocky, Nobara; Fedora Atomic (Silverblue, Kinoite, Bazzite, Aurora, Bluefin) | `intel-npu-driver` in repos | **Tested on hardware** (Atomic untested) |
| Debian | Ubuntu, Pop!\_OS, Mint, elementary, Raspbian | Intel upstream `.deb` packages | From package index |
| Arch | CachyOS, EndeavourOS, Manjaro, Garuda | AUR (`intel-npu-driver`) | From package index |
| openSUSE | Tumbleweed, Leap, SLES, Aeon, MicroOS | `libze_intel_npu1` + `level-zero` (Tumbleweed, Leap 16); else upstream | From package index |
| Gentoo | — | Community overlays or upstream | From package index |
| Void | — | Upstream release | From package index |
| Alpine | — | Unsupported (musl; upstream driver requires glibc) | From package index |
| NixOS | — | `hardware.cpu.intel.npu.enable` (25.11+) | Config snippets (no `nix-env`) |

openSUSE's `libze_intel_npu1` (Tumbleweed 1.38.0, Leap 16.0 1.35.0; checked 2026-11-05) builds with `ENABLE_NPU_COMPILER_BUILD=OFF` and does not pull in the Level Zero loader, so `level-zero` is listed alongside it.

Other distributions receive generic instructions. On every distribution, the NPU compiler library (`libopenvino_intel_npu_compiler.so`) must come from Intel's release archive (either via `ovfetch npu install` or manual extraction), which `vinowhisper-doctor` checks and reports. See [audio.md](audio.md#distro-support) for audio capture packages.

## Image-based distros (Fedora Atomic, openSUSE Aeon)

Immutable distributions (Silverblue, Kinoite, Bazzite, Aurora, Bluefin, Aeon) keep `/usr` read-only. Everything except host drivers (`uv`, virtualenv with uv-managed Python if host Python is 3.14, models, `~/.local/bin`, and desktop entries) installs into your home directory normally. Run vinoWhisper on the host rather than inside a toolbox so it shares the host Wayland, audio, and D-Bus session.

`vinowhisper-setup` and `vinowhisper-doctor` detect immutable systems (`/run/ostree-booted` or read-only `/usr`) and adjust system package commands:

| System | Package command | Follow-up |
|---|---|---|
| Fedora Atomic | `sudo rpm-ostree install --idempotent <packages>` | Reboot and re-run setup |
| openSUSE Aeon / MicroOS | `sudo transactional-update --non-interactive pkg install <packages>` | Reboot and re-run setup |
| Other immutable distros (SteamOS, Vanilla OS 2, Endless OS) | Prints required package names only | Install per distro method |

`pipewire-utils` (`pw-record`) ships in Fedora Atomic desktop images by default, so usually only `intel-npu-driver` or `intel-compute-runtime` needs layering.

Two additional differences on immutable systems:

- **NPU compiler:** `ovfetch npu install` places it in `~/.local/lib/ovfetch-npu` without layering (see [hardware.md](hardware.md#when-the-userspace-half-is-missing)). Manual installation targets `/usr/local/lib64` (`/var/usrlocal`) with an `/etc/ld.so.conf.d` entry (extract the `.deb` inside a toolbox if `dpkg-deb` is absent on the host).
- **`render` group membership:** System groups are defined in `/usr/lib/group` while `usermod` edits `/etc/group`, so copy the `render` entry first:

  ```bash
  grep -E '^render:' /usr/lib/group | sudo tee -a /etc/group
  sudo usermod -aG render $USER
  ```

Image-based support is based on distro documentation and covered by unit tests; it has not yet been run on an image-based host.

## Flatpak

Each release publishes `vinowhisper-x86_64.flatpak`, bundling all CLIs, the server, tray/overlay, settings and sessions windows, standalone Python 3.13, OpenVINO, and Intel's userspace NPU and GPU drivers. Only the kernel `intel_vpu` driver and firmware are needed on the host.

### Installing and setting up

```bash
curl -fLO https://github.com/karanshukla/vinoWhisper/releases/latest/download/vinowhisper-x86_64.flatpak
curl -fLO https://github.com/karanshukla/vinoWhisper/releases/latest/download/vinowhisper-x86_64.flatpak.sha256
sha256sum -c vinowhisper-x86_64.flatpak.sha256
flatpak install --user vinowhisper-x86_64.flatpak
```

(`org.freedesktop.Platform` 26.08 is pulled from Flathub if not already installed.)

Next, export Whisper once:

```bash
flatpak run --command=vinowhisper-setup io.github.karanshukla.vinowhisper
```

Because `optimum` and `torch` are excluded from the bundle to keep its size down, the wizard offers to create an export venv at `~/.var/app/io.github.karanshukla.vinowhisper/data/vinowhisper/export-env` (1.3 GB; safe to delete once models are exported). Alternatively, open **vinoWhisper** from your app menu and download Canary 1B v2 under Settings → Speech, which requires no export ([models.md](models.md)).

Open **vinoWhisper** from the app menu to start the tray, overlay, and dictation. The first dictation prompts for `RemoteDesktop` portal typing access, and the desktop notifies you whenever a new typing session starts (closing after 5 idle minutes by default; see [tray.md](tray.md#dictation)). **Start at login** uses the Background portal.

All CLI commands work via `--command`:

```bash
flatpak run --command=vinowhisper-caption io.github.karanshukla.vinowhisper
alias vw='flatpak run io.github.karanshukla.vinowhisper'   # vw toggle, vw dictate
```

### Updating, uninstalling, and building locally

Use **Update** under Settings → About (release `stable` bundles only; verifies `.sha256`, installs via host `flatpak`, checks the installed version, stops the server, and restarts the tray; system-wide `--system` installs prompt for admin auth). Manual update:

```bash
# download, verify sha256, and flatpak install --user vinowhisper-x86_64.flatpak, then:
flatpak run --command=vinowhisper-server io.github.karanshukla.vinowhisper --stop
flatpak run io.github.karanshukla.vinowhisper restart
```

Local checkout builds use branch `master` and link to GitHub Releases instead of self-updating (uninstall `io.github.karanshukla.vinowhisper//master` before switching to a `stable` release; `~/.var/app` data stays). To build locally:

```bash
flatpak install --user flathub org.flatpak.Builder org.freedesktop.Sdk//26.08 \
  org.freedesktop.Sdk.Extension.rust-stable//26.08
flatpak run --filesystem=$HOME org.flatpak.Builder --user --install --force-clean \
  --repo=repo build packaging/flatpak/io.github.karanshukla.vinowhisper.yml
flatpak build-bundle repo vinowhisper.flatpak io.github.karanshukla.vinowhisper
```

Uninstall with `flatpak uninstall io.github.karanshukla.vinowhisper` (add `--delete-data` to remove models, sessions, and settings; run `vinowhisper-tray --uninstall` first if you want the sessions app-menu entry and login entry cleaned up too).

### Sandbox permissions and differences from native

| Permission | Reason |
|---|---|
| `--device=all` | Intel NPU (`/dev/accel`, no narrower Flatpak option) and GPU |
| `--filesystem=home` | Input files for `vinowhisper-transcribe`, `--record`/`--transcript` paths, sessions menu entry, and reading portal autostart entry |
| `--share=network` | Model downloads and update checks |
| `--socket=wayland` | Overlay, settings, and sessions windows |
| `--filesystem=xdg-run/pipewire-0` | `pw-record` and `pw-dump` (system audio, mic, playing apps) |
| `--socket=pulseaudio` | `pactl` and `parec` |
| `--talk-name=org.kde.StatusNotifierWatcher` | Tray icon |
| `--talk-name=org.freedesktop.Notifications`, `--talk-name=org.a11y.Bus` | Summary notifications and screen reader announcements |
| `--talk-name=org.mpris.MediaPlayer2.*` | Pausing media players during dictation |
| `--talk-name=org.freedesktop.FileManager1` | Show in folder |
| `--talk-name=org.freedesktop.Flatpak` | Host `flatpak` updates and host `busctl` for playing media (`flatpak-spawn --host`) |

Global shortcuts, autostart, file dialogs, and dictation typing/clipboard use XDG portals and require no static permissions.

Key differences from a native install:

- **Separate data directory:** Config, models, and sessions are stored under `~/.var/app/io.github.karanshukla.vinowhisper` (`config/vinowhisper`, `data/vinowhisper/models`, `data/vinowhisper/transcripts`). Migrate from a native install (with neither running) by copying those directories; exports are identical and `vinoSearch` (after v11) discovers sessions in both locations:

  ```bash
  app=~/.var/app/io.github.karanshukla.vinowhisper
  mkdir -p $app/config $app/data
  cp -a ~/.config/vinowhisper $app/config/
  cp -a ~/.local/share/vinowhisper $app/data/
  ```

- **Do not install alongside a native install:** Both use app ID `io.github.karanshukla.vinowhisper` so portal shortcuts carry over; a native `.desktop` file in `~/.local/share/applications` shadows the Flatpak's launcher. Remove the native install first (`vinowhisper-upgrade --uninstall`).
- **Per-command sandboxes:** Tray and server sockets are placed in the shared `$XDG_RUNTIME_DIR/app/io.github.karanshukla.vinowhisper`. The server spawns in its own sandbox via `flatpak-spawn` so closing the caller does not kill it. Server logs go to `~/.var/app/io.github.karanshukla.vinowhisper/.local/state/vinowhisper/server.log`.
- **Desktop exports & host detection:** Launcher, icons, and `.vwsession` MIME registration are exported by Flatpak (`vinowhisper-tray --install` is a no-op). `playing.py` uses host `busctl` via `flatpak-spawn --host`. `vinowhisper-setup` reads `/run/host/os-release` for host kernel/group instructions. On Sway/Hyprland, bind `flatpak run io.github.karanshukla.vinowhisper toggle` and `dictate-press`/`dictate-release` ([tray.md](tray.md)).

### Flatpak overhead and test status

Measured 2026-11-14 on a 4-core cloud Xeon (CPU device, bundle binaries inside vs. outside sandbox; not an NPU measurement):

| Metric | Inside Flatpak | Same binaries without sandbox |
|---|---|---|
| 12s window decode (median of 8) | 0.86s | 0.86s |
| Cold server startup to socket accept | 0.34s | 0.25–0.30s |
| `flatpak run` wrapper overhead | 0.07s | — |

The tray starts caption and dictation children inside its own sandbox, avoiding per-call `flatpak run` overhead. Bundled Python (including stdlib) is precompiled to `.pyc` at build time because `/app` is read-only.

**Verified in a container (2026-11-14, Flatpak 1.14.6, CPU only, no audio server):** full build and `packaging/flatpak/smoke.sh`, `vinowhisper-setup --yes --device CPU` (export venv, pinned source, stateful export), `vinowhisper-transcribe` on 13s synthetic speech (9x real time) + cross-sandbox `vinowhisper-server --stop`, `vinowhisper-flatpak-update` rejecting a bad `.sha256` and applying a valid update, and headless Sway 1.9 (`toggle`, `hide`, `shortcuts`, `restart`, `quit`, overlay, settings window).

**Not yet tested on a full desktop:** NPU/GPU inside the sandbox, audio capture, tray icon, shortcut/Background portals, and full Plasma desktop runs. Note two desktop specifics:

- Flatpak 1.15.6+ assigns apps a restricted Wayland security context (`wp_security_context_v1`), under which some compositors withhold `wlr-layer-shell` (Sway) or data-control. KWin 6.8 withholds data-control and fake-input but allows `wlr-layer-shell` (measured 2026-11-14), and portal typing/copying was verified on Plasma 6.8.2 the same day.
- `restart` uses Flatpak's portal and requires `WAYLAND_DISPLAY` in the D-Bus activation environment (automatic on Plasma/GNOME; on Sway add `exec dbus-update-activation-environment --systemd WAYLAND_DISPLAY XDG_CURRENT_DESKTOP`).

## OpenVINO version floor

`openvino>=2026.4.1` is required:

- `2026.3.1` was the first stable release able to build the NPU static Whisper pipeline (prior to 2026-08-31, vinoWhisper pinned OpenVINO nightly wheels because 2026.2.1's `pipeline_static.cpp` failed with `OPENVINO_ASSERT(!self_attn_nodes.empty())` on `optimum-intel`'s SDPA attention mask).
- The floor moved from `2026.3.1` to `2026.4.1` after v1.6.1 to match `vinoSpeak` and `vinoPop` (processes sharing an NPU should run the same OpenVINO version, as an NPU fault resets the device for all clients) and to include 2026.4.1's compiled blob cache hardening.

Measured 2026-08-31 on Wildcat Lake NPU (`--disable-stateful` export, `WhisperPipeline(..., STATIC_PIPELINE=True)`):

| OpenVINO version | Source | Pipeline build | `generate()` |
|---|---|---|---|
| 2026.3.1 | PyPI stable | 2.0s | ok |
| 2026.4.0.dev20260805 | nightly | 2.3s | ok |
| 2026.5.0.dev20260831 | nightly | 1.9s | ok |

`STATIC_PIPELINE=True` is mandatory on the NPU; omitting it triggers the stateful path and fails with `Stateful models without 'beam_idx' input are not supported in StatefulToStateless transformation`. `.github/workflows/deps-canary.yml` still verifies a fresh dependency resolve weekly.

## Model export and verification

Model export uses two verification layers:

1. **Pinned source download (`vinowhisper/model_sources.json`):** Pins the Hugging Face commit and per-file SHA-256 hashes. `python -m vinowhisper.source` downloads files into `models/source/` under the data directory and aborts on any mismatch before `optimum-cli` runs with `HF_HUB_OFFLINE=1` (a local snapshot is used because `optimum-cli` 2.2.0 lacks a `--revision` flag). Unpinned `--model` repositories export with a warning.
2. **Exported IR digests (`vinowhisper/model_digests.json`):** `convert_model.sh`, `vinowhisper-setup`, and `vinowhisper-doctor` hash all 16 files in the exported IR (~1.2s for 1.5 GB, including `generation_config.json` and `tokenizer.json`; skipped on server startup to avoid adding 1.2s to cold start).

| Verification status | Meaning | Blocks setup |
|---|---|---|
| `verified` | All pinned files match | No |
| `unpinned` | No pin exists for this model/variant (e.g., `whisper-base.en` or stateful export) | No |
| `drift` | Both file bytes and export toolchain in `rt_info` differ from the pin | No |
| `mismatch` | Exported by the *pinned* toolchain, but bytes differ | Yes |
| `incomplete` | One or more pinned files are missing | Yes |
| `known_bad` | Exported by a toolchain known to produce a broken export (exact match or `at_least` floor) | Yes |

Exports are bit-reproducible on a fixed toolchain (measured 2026-09-04: two runs produced 16/16 identical files), but vary across toolchains (comparing OpenVINO 2026.2.1 / optimum-intel 2.0.0 / transformers 5.0.0 against 2026.3.1 / 2.1.0 / 5.5.4 changed 9 of 16 files, while `openvino_encoder_model.bin` stayed identical). Toolchain versions are merged from `rt_info` across every `.xml` file (any disagreement across XML files marks versions unknown). Because `rt_info` resides inside the exported files, `drift` guards against accidental corruption rather than a local attacker; the source pin protects the upstream download. Update pins after a verified export with `./scripts/update_digests.py --variant npu` (which preserves `known_bad`). A missing or corrupt pin file degrades to `unpinned`.

## `transformers` 5.4.0 breaks the NPU export

**Bisected on hardware 2026-09-04:** NPU exports require `transformers<5.4`. Exports created with 5.4.0+ compile on the NPU without error but fail at `generate()`:

```
RuntimeError: Port for tensor name cache_position was not found.
  (src/inference/src/cpp/infer_request.cpp:191)
```

Bisect with `optimum-intel` 2.1.0, `optimum` 2.3.0, `openvino` 2026.3.1, `openvino-genai` 2026.3.1.0, and `torch` 2.13.0 held fixed (`whisper-small.en`, `--disable-stateful`, clean venv per version):

| `transformers` | Pipeline build | `generate()` |
|---|---|---|
| 5.0.0 | ok | 0.96s |
| 5.2.0 | ok | 0.86s |
| 5.3.0 | ok | 0.71s |
| 5.4.0 | ok | **fails** |
| 5.5.4 | ok | **fails** |

Control runs confirmed `optimum-intel` 2.1.0 + `transformers` 5.0.0 works, and the 2026-08-03 package set works under OpenVINO 2026.3.1. In 5.3.0 exports, `cache_position` appears once as an internal tensor name on `__module.model.model.decoder/aten::arange/Range`; 5.4.0 stopped emitting that name, breaking the NPU static pipeline's internal lookup (input/output signatures are otherwise identical). The stateful (CPU/GPU) export is unaffected (verified under 5.5.4 on 2026-09-12).

Since 2026-09-12, export dependencies are in `vinowhisper[export]` (holding `transformers<5.4` and installed with CPU torch via `--torch-backend cpu` under `uv` or PyTorch's CPU index under `pip`). `model_digests.json` also marks `transformers>=5.4.0` as `known_bad`.

As of 2026-09-20, raising the cap is not possible because all `optimum-intel` releases (including 2.2.0) require `transformers<5.6,>=4.51`; forcing a higher `transformers` backtracks `optimum` to a broken pre-5.x release. Consequently, `GHSA-fgcw-684q-jj6r` (fixed in 5.5.0) and `GHSA-xrqw-3rrv-vx5w` (fixed in 5.10.0) remain open in the export extra; `transformers` is never imported at runtime and only reads pinned files for the default model (see [SECURITY.md](../SECURITY.md)).

## The desktop overlay (optional)

`vinowhisper-setup --gui` installs `vinowhisper-tray` (floating captions, dictation, tray icon, global shortcuts) and `vinowhisper-gui` (settings and sessions window). It downloads the release binaries from GitHub and verifies their SHA-256 digests pinned in the Python package, so no Rust toolchain is required. From a source checkout, `./scripts/install.sh --gui` builds them with Cargo instead. See [tray.md](tray.md).

## NPU platform data (optional)

`vinowhisper-setup --ovfetch` installs the hash-verified static release binary of [ovfetch](https://github.com/karanshukla/ovfetch) (offered only when an Intel NPU is present, skipped if already current). It adds platform driver verification lines to `vinowhisper-doctor`, and provides `ovfetch npu install` (0.6.0+) when a distro driver package lacks the NPU compiler (such as Fedora's `intel-npu-driver` 1.32.0). See [hardware.md](hardware.md#platform-data-from-ovfetch).

## Uninstalling

```bash
vinowhisper-upgrade --uninstall
```

From a checkout install (which has no `vinowhisper-upgrade` wrapper), run the release script directly (downloads nothing else):

```bash
curl -fsSL https://github.com/karanshukla/vinoWhisper/releases/latest/download/install.sh | bash -s -- --uninstall
```

Uninstall removes, in order:

1. **Server:** Runs `vinowhisper-server --stop` and disables/removes pre-1.7 systemd user units in `~/.config/systemd/user` if present (`daemon-reload`).
2. **Tray and desktop entries:** Runs `vinowhisper-tray quit` and removes launchers, settings/sessions desktop entries, icons, `.vwsession` MIME registration, and autostart entries.
3. **Commands and completions:** Runs `uv tool uninstall vinowhisper` and removes `~/.local/bin` symlinks, bash completion symlinks, `vinowhisper-tray`, `vinowhisper-gui`, and `vinowhisper-upgrade`.
4. **Models and caches:** Removes `$XDG_DATA_HOME/vinowhisper/models` (Whisper exports/source, Canary, SmolLM3, Silero VAD) and `$XDG_CACHE_HOME/vinowhisper` (compiled SmolLM3 blobs, export scratch).
5. **Managed clone:** Removes `scripts/install.sh`'s clone at `~/.local/share/vinowhisper/src` only if clean (kept if uncommitted changes, unpushed branch commits, or stashes exist; user-cloned checkouts are untouched).

**Preserved user data and shared tools:** Saved sessions/transcripts (`~/.local/share/vinowhisper/transcripts`), config (`~/.config/vinowhisper`), summariser state (`~/.local/state/vinowhisper`), exported documents, `uv`, `ovfetch`, `~/.local/lib/ovfetch-npu`, and `~/.cache/ze_intel_npu_cache`.

To delete individual models without uninstalling, use Settings → Speech (Whisper, Canary) or Settings → Cleanup (SmolLM3), or run `vinowhisper-whisper model delete`, `vinowhisper-canary model delete`, or `vinowhisper-cleanup model delete`.

## Pinning the terminal on top

Unlike the Wayland overlay, `vinowhisper-caption` runs in a regular terminal, so keeping it above other windows is configured in your window manager. On KDE KWin: open **System Settings → Window Management → Window Rules**, match your terminal window, and force **Keep Above Other Windows** (optionally enabling **Skip Taskbar**, **Skip Pager**, and removing the titlebar).
