# Development

How vinoWhisper is built, tested, packaged, and released. See [CONTRIBUTING.md](../CONTRIBUTING.md) for the quick-start workflow.

## Two Python environments

```bash
uv sync --extra export        # full runtime + export toolchain (Python <3.14)

uv venv
uv pip install --group dev    # tests and linting only (~5s; used by CI)
```

The `dev` group intentionally excludes OpenVINO (~400 MB) so unit tests run quickly in CI without hardware:

- Nothing under `tests/` imports `openvino`. `transcriber.py` and `server.py` import it lazily so tests can inject a fake pipeline.
- `flask` and `werkzeug` are in `dev` for `tests/test_server.py`.
- `requests` (~100 KB pure Python) is in `dev` because `vinowhisper.caption` imports `client.py` at module scope, which `tests/test_caption.py` needs during collection.
- Pytest uses `pythonpath = ["."]` to import directly from the source tree without installing the package (which would pull in OpenVINO).

Coverage measures full-package branch coverage with no omissions, including hardware-only paths (`replay.py` is the largest gap). CI enforces the threshold in `ci.yml`, posts per-file results to the job summary, and uploads `coverage.xml`. Rust jobs measure coverage with `cargo llvm-cov` and upload `lcov.info`.

## Running your own build

You can run directly from a git checkout across all three components:

| Component | Installed location | Redeploy after edits |
|---|---|---|
| Python CLIs (`caption`, `dictate`, `server`, `doctor`, `replay`, `setup`) | Editable `.venv`, symlinked into `~/.local/bin` | `uv sync --extra export`, then start a fresh process |
| `vinowhisper-tray` | Binary in `~/.local/bin` | Rebuild, install atomically, restart tray |
| `vinowhisper-gui` (settings & sessions window) | Binary in `~/.local/bin` | Rebuild and install atomically (launches fresh each time) |

### Python

```bash
uv sync --extra export
uv run vinowhisper-setup     # symlinks commands into ~/.local/bin
```

Edits take effect on the next process start:

- **Stitcher and capture (`vinowhisper-caption`):** The tray spawns a fresh process whenever captions are shown; hide and show the overlay to pick up changes.
- **Dictation (`vinowhisper-dictate --json`):** The tray keeps one idle process running to avoid Python startup latency, so edits to `dictate.py` require restarting the tray.
- **Server (`vinowhisper-server`):** Run `vinowhisper-server --stop` so the next request starts the updated code (check `pgrep -af vinowhisper-caption` first, as stopping kills active captions and incurs a model reload).
- **`pyproject.toml` changes:** Re-run `uv sync`, and re-run `vinowhisper-setup` if you added a new CLI entry point.

### Tray and settings window (Rust)

```bash
cargo build --release --locked --manifest-path tray/Cargo.toml
cargo build --release --locked --manifest-path gui/Cargo.toml
for b in tray gui; do
    install -m755 $b/target/release/vinowhisper-$b ~/.local/bin/vinowhisper-$b.part
    mv ~/.local/bin/vinowhisper-$b.part ~/.local/bin/vinowhisper-$b
done
```

Install via a `.part` file and atomic `mv` so overwriting the running `vinowhisper-tray` binary does not fail with `Text file busy`. Then restart the tray:

```bash
pgrep -af vinowhisper-caption           # active caption session exits with the tray
vinowhisper-tray quit
setsid -f vinowhisper-tray >/dev/null 2>&1
```

Only one tray runs per session (bound to `$XDG_RUNTIME_DIR/vinowhisper-tray.sock`); launching a second without `quit` forwards its command to the running instance and exits. Child caption and dictation processes exit automatically via `PR_SET_PDEATHSIG`.

Local builds link glibc (unlike the static musl release asset), which works on the machine that built them. Run `VINOWHISPER_TRAY_TRACE=1 vinowhisper-tray` to log dictation key and state transitions to `stderr` (never dictated text).

### Verifying your active install

```bash
ls -l ~/.local/bin/vinowhisper-*        # Python symlinks into .venv; tray/gui timestamps
pgrep -af 'vinowhisper-(tray|caption|dictate)'
```

If a change does not appear, an older tray or server process is almost always still running.

## Checks

```bash
uv run poe check      # ruff check, ruff format --check, mypy, pytest
uv run poe fix        # ruff --fix and ruff format
uv run poe security   # Bandit scan matching bandit.yml
```

- **mypy `python_version`:** Not pinned to `3.11` because NumPy's type stubs use Python 3.12 syntax (`type X = ...`), which fails under `python_version = "3.11"`. Python 3.11 compatibility is enforced by `requires-python` and Ruff's `target-version`. `ignore_missing_imports` is enabled because `openvino_genai` ships no stubs and is absent in CI.
- **Bandit skips:** Configured in `[tool.bandit]`:

| Check | Rationale |
|---|---|
| `B404` | Flags `import subprocess`, which is core to invoking `pw-record`, `parec`, `pactl`, `optimum-cli`, and the local server |
| `B603` | Every subprocess call passes a literal `argv` list with `shell=False`; dynamic values (PipeWire nodes, `--target`) are single elements |
| `B607` | Tools like `pw-record`, `parec`, and `pactl` are installed in different paths across distros and are resolved with `shutil.which` |

`B602` (`shell=True`) remains enabled and must stay clean.

## Accessibility

`vinowhisper-gui` enables eframe's `accesskit` feature so it exposes an AT-SPI accessibility tree (disabling default features previously dropped it, leaving Orca with an empty window; found 2026-10-23). `gui/` tests verify the AccessKit tree headlessly with `egui_kittest`:

- Every page is a focusable `Tab` with name and selected state, navigable via `Tab` and activated with `Enter`/`Space`.
- Every control, list, and numeric field is linked to its label (`labelled_by`), with no nameless focusable widgets (including egui scrollbars).
- Section titles expose heading roles; model download status and reload refusals expose polite live regions.
- Light and dark themes pass WCAG 2.2 contrast ratios programmatically in the shared `vino-ui` crate (4.5:1 for text; 3:1 for field borders, focus rings, and active-page tints).
- At 2x zoom (`Ctrl++`), the sidebar collapses into tabs without losing controls; at 1.5x and 3x, no control clips past the window edge.
- Desktop text scaling sets initial zoom (#99): reads GNOME `text-scaling-factor` (Settings portal or `gsettings`) or Plasma forced font DPI (>96) / general font size (>10pt), clamped to 1x–3x. Re-checked on focus gain and applied only if changed so manual `Ctrl+`/`Ctrl-` adjustments persist.

Because the dictation pill is a click-through layer-shell surface with no accessibility tree, when a screen reader is active (`org.a11y.Status.ScreenReaderEnabled`) the tray mirrors failures, clipboard fallbacks, and cleanup notices as desktop notifications (#98). Live words and routine states (recording, transcribing, typed) are never sent.

The tray icon stays visually static while state is communicated in the tooltip (caption state and active dictation state, never dictated text). `tray.rs` tests verify menu item labels, check/radio states, and distinct state descriptions.

Verified 2026-10-23 on headless Sway with AT-SPI (`python3-pyatspi`): tree structure, names, and roles came through. Not tested: Orca speech output, GNOME/Plasma tray hosts, or the overlay itself (which has no accessibility tree). `accesskit_atspi_common` 0.14 reports disabled buttons as sensitive (upstream bug).

## Packaging

`[tool.setuptools] packages = ["vinowhisper"]` is set explicitly because flat-layout auto-discovery fails when multiple top-level directories exist (`tray/`, `gui/`, `settings/`, and formerly `systemd/`; confirmed 2026-08-03).

Data files bundled inside the wheel:

- **`model_digests.json`:** `integrity.load_pins()` falls back to "unpinned" if missing, so `tests/test_integrity.py` verifies it is packaged and readable, and fails if `uv.lock`'s export toolchain drifts further from the NPU pin.
- **`model_sources.json`:** Pinned Hugging Face commit and per-file SHA-256 hashes (hand-maintained from Hub LFS digests and file bytes). Missing it causes setup to refuse exporting; verified by `tests/test_source.py`.
- **`tray_release.json` and `gui_release.json`:** Generated only during release builds by `scripts/pin_tray_release.py` before `uv build` (verified in `release.yml`). Absent in source checkouts so `overlay.availability()` refuses unpinned downloads.

`openvino-tokenizers` is declared directly (even though `openvino-genai` pulls it in) because the NPU static pipeline hard-depends on it.

### Flatpak packaging

`packaging/flatpak/io.github.karanshukla.vinowhisper.yml` builds the Flatpak bundle from the checkout (see [install.md](install.md#flatpak) for build commands). Key implementation details:

- **Bundled Python 3.13:** Uses a pinned `python-build-standalone` tarball because `org.freedesktop.Platform` 26.08 ships Python 3.14, which cannot run Whisper export.
- **Locked `requirements.txt`:** Generated from `uv.lock` via `scripts/flatpak_requirements.py` (`uv export` + locked `setuptools`) and installed with `pip --require-hashes`. Re-run after updating `uv.lock`; `tests/test_flatpak.py` enforces parity.
- **Global `no-debuginfo`:** `flatpak-builder` 1.4.2's `eu-strip` corrupted standalone `python3.13` even when `no-debuginfo` was set per-module (found 2026-11-14). Top-level `build-options` disable stripping manifest-wide; Cargo strips the Rust binaries itself.
- **Bytecode precompilation:** The entire Python tree is compiled to `.pyc` during build because `/app` is read-only (`flatpak-builder` aligns `.pyc` timestamps with ostree).
- **`flatpak-spawn` cleanup:** `launch.ensure` and `restart` send `SIGKILL` to `flatpak-spawn` once the spawned server or tray answers; otherwise `flatpak-spawn` stays alive and holds calling pipes open until the server idles out (e.g., `flatpak run --command=vinowhisper-transcribe … | grep`; `SIGKILL` is required because `flatpak-spawn` forwards catchable signals).
- **Build cache order:** Rust modules build before Python so Python edits rebuild in seconds.
- **Shared Intel driver modules:** Version-matched with `vinoSearch` so both Flatpaks share one NPU stack; update them together.
- **Network build:** `pip` and `cargo` fetch hash-verified locks at build time (`--share=network`), so the manifest is not Flathub-offline.
- **App ID & exports:** Uses the tray's `APP_ID` so global shortcut portal entries carry over; the session MIME package is renamed to `<id>.xml` as required by Flatpak export rules.

**Version reporting (`release` vs. `dev`):** `--version`, `vinowhisper-doctor`, and Settings → About report the version number for release builds and `dev` for source builds (so tools like `vinoPilot` can distinguish them). Rust binaries stamp `VINOWHISPER_VERSION` at build time (`settings/src/version.rs` enforces a match with `Cargo.toml`). Python inspects PEP 610 `direct_url.json` at runtime (`vinowhisper/version.py`: directory/git installs report `dev`, wheels report the version; release Flatpak builds write `/app/lib/vinowhisper/release-version`). Overlay pins and `pip` constraints always use `vinowhisper.__version__`.

`packaging/flatpak/smoke.sh` (run by `flatpak.yml`) verifies every CLI's `--help`, version parity across Rust and Python, OpenVINO CPU enumeration, exported desktop entries, and cross-sandbox server startup.

Sandbox-specific behavior is gated on `/.flatpak-info` (`vinowhisper/sandbox.py`, `settings/src/sandbox.rs`): shared runtime dir, `flatpak-spawn` for server/tray restarts, Background portal autostart, updater command, and export venv path.

## Overlay Rust dependencies

All crates in `tray/Cargo.toml` are pure Rust so the binary links only libc (see [tray.md](tray.md#why-rust)):

| Crate | Purpose |
|---|---|
| `smithay-client-toolkit` | Pure-Rust Wayland client (`wlr-layer-shell`, SHM buffers, event loop; no `libwayland` or `xkbcommon`) |
| `cosmic-text` | Text shaping, wrapping, glyph rasterization, and direct fontconfig file parsing (no `libfontconfig`) |
| `ksni` | StatusNotifierItem tray icon over D-Bus |
| `vino-ui` | Shared tray menu icons (`default-features = false`, without egui) |
| `ashpd` | Global shortcuts and dictation typing/clipboard via `xdg-desktop-portal` |
| `rustix` | Signals, `PR_SET_PDEATHSIG`, keymap `memfd`, and `CLOCK_BOOTTIME` `timerfd` |

`rust-version = "1.95"` is set by `vino-ui` (matching `gui/`; `cosmic-text` 0.19 alone requires 1.89) and checked by CI's `msrv` job.

## Shell completion

`vinowhisper-setup` installs `scripts/vinowhisper-completion.bash`. To install manually without root:

```bash
mkdir -p ~/.local/share/bash-completion/completions
ln -sf "$PWD/scripts/vinowhisper-completion.bash" \
       ~/.local/share/bash-completion/completions/vinowhisper-caption
for c in server replay doctor setup; do
    ln -sf vinowhisper-caption ~/.local/share/bash-completion/completions/vinowhisper-$c
done
```

`bash-completion` lazy-loads completions by command name, which requires one symlink per command. Completions apply to `vinowhisper-*` directly, not `uv run vinowhisper-*` (where `uv` owns completion). Completing `--target` invokes `vinowhisper-caption --list-targets` from `PATH` (~0.2s Python startup per Tab press; avoids `COMP_WORDS[0]` in case of relative paths).

## Conventions

- **Minimal comments:** Extract helpers, name clearly, or pin invariants with tests before commenting, and cite governing tests from code ([comment-style.md](comment-style.md), verified by Doc Links CI). No module/class/function docstrings in `vinowhisper/` or `//!`/item docs in `tray/src/`. Put design rationale and measurements in `docs/`.
- **Dated measurements:** Always attach dates to measured hardware or benchmark claims.
- **Actionable errors:** Error paths print distro-specific fix commands via `distro.remediation`.
- **Hardware-free unit tests:** Everything in `tests/` runs without an NPU, audio server, or OpenVINO.
- **Characterization tests (`test_characterization_*`):** Pin counter-intuitive intentional behaviors (e.g., refusing `--target` on PulseAudio, pre-mute sink monitors, `Live.update(refresh=True)`). Review carefully before modifying assertions.

## CI workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `ci.yml` | Push, PR | `uv lock --check`, Ruff, mypy, pytest + coverage (Python 3.11–3.13), wheel smoke test (`scripts/smoke_wheel.sh`), `shellcheck`, Rust tests/coverage/MSRV, Doc Links citation check |
| `bandit.yml` | Push/PR to `main`, Mondays | Bandit security scan uploaded to GitHub Code Scanning and enforced as a gate (matches `poe security`) |
| `audit.yml` | Mondays, lockfile changes | `pip-audit` on runtime/dev deps (warning-only on `export` extra due to `transformers<5.4` advisories); `cargo audit` on all three Cargo locks |
| `deps-canary.yml` | Mondays, `pyproject.toml`/`uv.lock` changes | Verifies the full OpenVINO/export dependency set not installed by PR CI |
| `release.yml` | `v*` tag push | Builds static musl tray + wheel (`smoke_wheel.sh`), publishes to PyPI, creates GitHub Release, triggers Flatpak bundle |
| `flatpak.yml` | `packaging/flatpak/` PRs, `release.yml`, manual dispatch | Builds, installs, and smoke-tests (`packaging/flatpak/smoke.sh`) the Flatpak bundle; attaches `.flatpak` and `.sha256` on tag builds |

Python 3.14 is excluded from the matrix (see [install.md](install.md)). `shellcheck` validates every script in `scripts/`. Overlay tests run headlessly in memory using temporary directories and scratch sockets.

**Dependency canary (`deps-canary.yml`)** runs three separate steps to isolate breakages:

1. `uv sync --extra export` (without `--frozen`) followed by OpenVINO import and device enumeration.
2. `optimum-cli env` (catches valid resolves that fail on import, such as backtracking `optimum` to a pre-5.x release, as happened in Dependabot #24).
3. `uv lock --upgrade --dry-run` (separates lockfile breakage from unresolvable upgrades).

(Before 2026-08-31 this job primarily caught pruned OpenVINO nightly wheels.)

## Commits, documentation site, and releases

Commits follow Conventional Commits (`feat:`, `fix:`, `docs:`, `ci:`, etc.) for `git-cliff` changelog generation:

```bash
uv run bump-my-version bump minor                       # updates versions, commits, tags vX.Y.Z
git cliff -c cliff.toml --tag vX.Y.Z -o CHANGELOG.md    # review and edit changelog
git push --follow-tags
```

Preview unreleased entries with `git cliff --unreleased`. `bump-my-version` updates `pyproject.toml`, `tray/Cargo.toml`, and `tray/Cargo.lock` together (`tests/test_packaging.py` fails if versions diverge).

**Docs site (`docs.vinowhisper.com`):** Built with Astro Starlight from `site/`. `docs/` remains the source of truth; `site/scripts/sync-docs.mjs` copies pages into a gitignored build directory, extracts frontmatter titles from each file's H1, and rewrites relative links. Preview locally with `cd site && npm ci && npm run dev`; CI verifies builds in the `site` job.

**Release pipeline (`release.yml` on `v*` tag):**

1. **`tray`:** Builds the static musl `x86_64-unknown-linux-musl` binary (static-pie, 6.0 MB, measured 2026-09-12). Not uploaded to PyPI.
2. **`build`:** Verifies tag matches `pyproject.toml`, pins binary SHA-256 digests into the wheel, builds sdist/wheel, verifies pins, runs `twine check --strict`, and extracts release notes from hand-edited `CHANGELOG.md`.
3. **`pypi`:** Publishes wheel/sdist via PyPI Trusted Publishing (OIDC).
4. **`github-release`:** Creates the release (`gh release create --verify-tag` with `GH_REPO` set, since the job has no checkout, fixing the v0.3.0 issue) and attaches wheel, sdist, binaries, and `SHA256SUMS`.
5. **`flatpak` (`flatpak.yml`):** Builds, smoke-tests, and attaches `vinowhisper-x86_64.flatpak` and `.sha256` (outside `SHA256SUMS`, which is already generated). If this step fails, PyPI and the GitHub Release remain intact and the Flatpak workflow can be re-run manually with the tag.

**Release safeguards:**

- **Minimal job permissions:** `build` has no token, `pypi` holds only `id-token: write`, and only `github-release` can write to the repo. PyPI runs before GitHub Release so a failed PyPI upload allows deleting and re-cutting the tag.
- **Trusted Publishing:** Uses short-lived GitHub OIDC tokens bound to this repo, `release.yml`, and the `pypi` environment (no stored API tokens). Update PyPI publisher settings before renaming the workflow or environment.
- **Tag-only publishing:** No `workflow_dispatch` trigger; `pypi` and `github-release` also verify `github.ref`.
- **Pinned tooling and Renovate:** `twine`, `bandit` (`bandit.yml` and `poe security`), and the release Rust toolchain are pinned to exact versions. Renovate auto-merges only `dev` Python dependencies after a 7-day stability window; Cargo crates and GitHub Actions require manual review, and runtime Python dependencies are excluded from Renovate.
