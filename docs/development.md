# Development

How the repo is built, tested and released, and why the build config looks the
way it does. [CONTRIBUTING.md](../CONTRIBUTING.md) is the short version.

## Two environments

```bash
uv sync --extra export        # the real one: OpenVINO, the export tooling, Python <3.14

uv venv
uv pip install --group dev    # tests and lint only, ~5 seconds, what CI uses
```

The `dev` group is installable on its own on purpose. CI has no NPU, no audio
server and no reason to download ~400MB of OpenVINO wheels to run tests that
are not allowed to import it. Nothing under `tests/` imports `openvino`,
`transcriber` or `server`, which is what keeps the split honest.

`requests` is in the dev group even though no test talks to the server:
`vinowhisper.caption` imports it at module scope through `client.py`, so
`tests/test_caption.py` cannot be collected without it. It is ~100KB of pure
Python.

Pytest imports the package from the source tree (`pythonpath = ["."]`) rather
than from an install, because installing the project would pull in the
OpenVINO stack the dev group exists to avoid. For the same reason
`transcriber.py`, `server.py` and `replay.py` are left out of coverage: they
need an NPU, an audio server or a live socket.

## Checks

```bash
uv run poe check      # ruff check, ruff format --check, mypy, pytest
uv run poe fix        # ruff --fix and ruff format
uv run poe security   # the same Bandit scan the Bandit workflow runs
```

**mypy is not pinned to `python_version = "3.11"`.** numpy's stubs use
3.12-only syntax (`type X = ...`), and mypy parses them against the target
version, so pinning 3.11 while running on 3.12 fails inside numpy before it
checks any code here. The 3.11 floor is enforced by `requires-python` and
ruff's `target-version` instead. `openvino_genai` ships no stubs, and CI runs
without it installed, hence `ignore_missing_imports`.

**Bandit skips three checks**, each because the finding is this tool's job
rather than a defect in it:

| Check | Why it is skipped |
|---|---|
| B404 | Flags `import subprocess` itself, in a program whose purpose is to drive pw-record, parec, pactl, optimum-cli and systemctl |
| B603 | Every call site passes a literal argv list with no shell. The variable parts (PipeWire node names from pw-dump, a `--target` the user typed) arrive as single argv elements |
| B607 | pw-record, parec and pactl live in different places on different distros. They are found with `shutil.which` before use, and hardcoding `/usr/bin` would break the fallbacks |

B602 (`shell=True`) stays on. There is no shell anywhere in this codebase, and
if one appears it should fail the scan.

## Packaging

`[tool.setuptools] packages = ["vinowhisper"]` is explicit because flat-layout
auto-discovery sees the top-level `systemd/` directory, decides there are two
packages, and refuses to build (confirmed 2026-08-03).

Two data files ride in the wheel, and both fail quietly if they go missing:

- **`model_digests.json`**: `integrity.load_pins()` treats a missing file as
  "nothing pinned" rather than crashing, so dropping it from `package-data`
  would degrade silently. `tests/test_integrity.py` checks it is declared and
  readable.
- **`gui_release.json`**: exists only in a release build. `release.yml` writes
  it (the sha256 of the overlay binary on the GitHub Release) with
  `scripts/pin_gui_release.py` just before `uv build`, then checks the wheel
  carries it. A checkout has none, and `overlay.availability()` then refuses
  to download, which is the point.

`openvino-tokenizers` is listed directly even though `openvino-genai` pulls it
in. The NPU pipeline hard-depends on it, and a transitive-only dependency is
one upstream packaging change from disappearing.

## The overlay's dependencies

Every crate in `gui/Cargo.toml` is pure Rust, so the binary links nothing but
libc and installs as one file on any distro (see [gui.md](gui.md#why-rust)).

| Crate | Does |
|---|---|
| smithay-client-toolkit | Wayland: layer-shell, shm buffers, the event loop. Pure-Rust backend, so no libwayland, and no xkbcommon since nothing reads the keyboard |
| cosmic-text | Shaping, wrapping and glyph rasterising. Finds fonts by parsing fontconfig's files, not by linking libfontconfig |
| ksni | The tray icon (StatusNotifierItem over D-Bus) |
| ashpd | Global shortcuts, and Shift+Insert for dictation (xdg-desktop-portal) |
| rustix | Signals, the child's parent-death signal, the memfd that carries a keymap to the compositor, and the tray's idle timerfd |

`rust-version = "1.88"` is for let-chains.

## Shell completion

`vinowhisper-setup` installs `scripts/vinowhisper-completion.bash`. By hand,
with no root:

```bash
mkdir -p ~/.local/share/bash-completion/completions
ln -sf "$PWD/scripts/vinowhisper-completion.bash" \
       ~/.local/share/bash-completion/completions/vinowhisper-caption
for c in server replay doctor setup; do
    ln -sf vinowhisper-caption ~/.local/share/bash-completion/completions/vinowhisper-$c
done
```

bash-completion loads a file lazily on the first Tab for a command of the same
name, which is why it is one symlink per command rather than a line in
`.bashrc`. It completes `vinowhisper-caption ...`, not `uv run
vinowhisper-caption ...`: there the command word is `uv`, and uv's own
completion owns the line.

`--target` completion runs `vinowhisper-caption --list-targets` off `PATH`, so
the pw-dump parse stays in one place. `COMP_WORDS[0]` is not used because it
can be a relative path that no longer resolves. It costs ~0.2s of Python
startup per Tab, only on `--target`.

## Conventions

- **Comments are minimal.** No module, class or function docstrings in
  `vinowhisper/`, no `//!` or item docs in `gui/src/`. A comment stays only
  where the code would invite a wrong "fix" without it, and then it is one
  line. Rationale, measurements and gotchas go in the matching `docs/` page.
  `tests/` is exempt.
- **Measured claims carry a date.** "Measured 2026-08-07: the sink monitor
  reads 0.98x of the app's level while muted" is worth keeping. "The monitor
  is pre-mute" on its own is how the project spent a week believing the
  opposite of the truth.
- **A failure names its fix.** Every error path tries to print the command
  that resolves it, in the local distro's package names
  (`distro.remediation`).
- **Tests are for the logic, not the hardware.** Anything under `tests/` runs
  with no NPU, no audio server and no OpenVINO. If a change can only be
  verified on the laptop, say so in the PR rather than faking a test.
- **`test_characterization_*` pins a known oddity on purpose.** `--target`
  refused on PulseAudio rather than reinterpreted, the sink monitor being
  pre-volume and pre-mute, `Live.update()` needing `refresh=True`. These are
  correct and look like bugs cold. If one goes red, the question is whether
  the behaviour was supposed to change, not which assertion to update.

## CI

| Workflow | Runs | What it is for |
|---|---|---|
| `ci.yml` | every push and PR | ruff, mypy, pytest on 3.11 to 3.13, shellcheck, and the overlay's `cargo` checks |
| `bandit.yml` | pushes and PRs to main, and Mondays | The Bandit scan, into Security > Code scanning. Config is `[tool.bandit]`, so `poe security` matches it |
| `deps-canary.yml` | Mondays, and on changes to `pyproject.toml` or `uv.lock` | The full dependency set, which nothing on the PR path installs |
| `release.yml` | a `v*` tag | Build, PyPI, GitHub Release |

3.14 is left out of the test matrix on purpose, for the reason in
[install.md](install.md). shellcheck runs on the installer and the export
script because they are the first thing a new user runs. The overlay's tests
need no compositor, tray or portal: they draw into memory, bind scratch
sockets and write into temp dirs, the same bargain `tests/` makes about the
NPU.

**The dependency canary** exists because PR CI installs only the dev group, so
nothing there would notice the real set breaking. A failure is usually
optimum or transformers moving, a new OpenVINO stable breaking the import or
plugin load, or a yanked transitive release. It has three steps, kept separate
so the failures stay distinguishable:

1. `uv sync --extra export`, not `--frozen`, since resolving from scratch is
   part of the test, then import OpenVINO and enumerate devices (no inference,
   the runner has no NPU).
2. `optimum-cli env`. A resolve can succeed on a toolchain that cannot export:
   satisfying a raised transformers cap by backtracking optimum to a pre-5.x
   release still resolves, and `optimum-cli` then dies on import. Dependabot
   #24 did exactly that and went green on step 1. `env` is the cheapest
   subcommand that imports the whole command tree.
3. `uv lock --upgrade --dry-run`: "the lock no longer installs" and "nothing
   newer resolves either" are different problems.

Before 2026-08-31 it had a sharper job: the project pinned OpenVINO nightly
wheels, which upstream prunes on its own schedule.

## Commits and releases

Conventional commits (`feat:`, `fix:`, `docs:`, `ci:`, ...). `CHANGELOG.md` is
generated from them with git-cliff, so the prefix decides the section.

```bash
uv run bump-my-version bump minor                       # writes the version, commits, tags vX.Y.Z
git cliff -c cliff.toml --tag vX.Y.Z -o CHANGELOG.md    # then hand-edit
git push --follow-tags
```

`git cliff --unreleased` previews what is pending. The version lives in
`pyproject.toml`, `gui/Cargo.toml` and `gui/Cargo.lock`, and bump-my-version
moves all three. They cannot differ: the overlay is downloaded from the
release tagged with the Python package's version, and
`tests/test_packaging.py` fails if they drift.

Pushing the tag runs `release.yml`:

1. **gui** builds the overlay once for `x86_64-unknown-linux-musl`, so the one
   binary is fully static (static-pie, 6.0MB, measured 2026-09-12). It never
   goes to PyPI.
2. **build** checks the tag matches `pyproject.toml`, pins the overlay's
   sha256 into the package, builds the sdist and wheel, checks the wheel
   carries the pin, runs `twine check --strict` (a rejected PyPI upload
   cannot be retried under the same version), and cuts the release notes out
   of `CHANGELOG.md`. The changelog is the single source of release prose
   because it is hand-edited, and re-deriving notes from commits would drop
   those edits.
3. **pypi** publishes the same bytes with Trusted Publishing.
4. **github-release** attaches the wheel, sdist, overlay binary and
   `SHA256SUMS`.

The split is about permissions. `build` holds no token, `pypi` holds only
`id-token: write`, and only `github-release` can write to the repo. PyPI goes
before the GitHub Release on purpose: a PyPI version can never be re-uploaded,
so if that step fails the tag can be deleted and re-cut, which a published
release pointing at a missing PyPI version would not allow.

**Trusted Publishing** means no API token exists anywhere. GitHub mints a
short-lived OIDC token that PyPI trusts because the project's publisher
config names this repo, `release.yml` and the `pypi` environment. Renaming
either breaks the upload by design, so change the publisher config on PyPI
first. The `pypi` environment is also where a required reviewer would go.

`github-release` has no checkout, so `gh` is told the repo through `GH_REPO`.
Without it `gh` exits with "not a git repository", which is how v0.3.0 had to
be released by hand after its PyPI upload had already gone through.
