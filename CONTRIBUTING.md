# Contributing

This started as a single-machine tool for one Fedora/KDE laptop with an Intel
NPU. Most of what would make it good for anyone else is knowledge about
*their* machine, which is exactly the thing that cannot be tested from here.

## The most useful contributions

1. **A distro correction.** `vinowhisper/distro.py` maps capabilities to
   package names for eight families. Fedora is the only one confirmed by use;
   the rest came from reading package indexes. If a command it printed did not
   work on your distro, [say so](https://github.com/karanshukla/vinoWhisper/issues/new?template=distro_support.yml) —
   it is a one-line fix and it is the difference between the tool working and
   not on your machine.
2. **A capture backend report.** PipeWire is what this was built against.
   PulseAudio support (`parec`) is written but has never run on a
   PulseAudio-only machine.
3. **Hardware other than Wildcat Lake.** Different NPU generations, Arc GPUs,
   the CPU fallback. `vinowhisper-doctor --json` plus the per-cycle numbers
   from `vinowhisper-caption --debug` are the useful payload.

## Setup

```bash
git clone https://github.com/karanshukla/vinoWhisper
cd vinoWhisper
uv sync                 # the real environment: OpenVINO, Python <3.14
uv run vinowhisper-setup --dry-run   # see what a full install would do
```

For work that does not touch the model — the stitcher, the UI, the distro
table, the capture argv — you do not need OpenVINO at all:

```bash
uv venv
uv pip install --group dev    # no OpenVINO, ~5 seconds
.venv/bin/python -m pytest
```

That is exactly what CI does, and for the same reason: OpenVINO is ~400MB of
wheels that no test is allowed to import anyway, so nothing that has to pass on
every pull request depends on it.

## Before pushing

```bash
uv run poe check    # ruff check, ruff format --check, mypy, pytest
```

## What good looks like here

- **Comments say why, not what.** This codebase is unusually comment-heavy on
  purpose: nearly every non-obvious line is a bug someone already paid for.
  If you fix something subtle, leave the reason behind.
- **Measured claims carry a date.** "Measured 2026-08-07: the sink monitor
  reads 0.98x of the app's level while muted" is worth keeping. "The monitor
  is pre-mute" on its own is how the project spent a week believing the
  opposite of the truth.
- **A failure should name its fix.** Every error path here tries to print the
  command that resolves it. An exception that only says what went wrong is
  half-finished.
- **Tests are for the logic, not the hardware.** Anything under `tests/` must
  run with no NPU, no audio server and no OpenVINO. If a change can only be
  verified on the laptop, say so in the PR rather than faking a test for it.

## Commits

Conventional commits (`feat:`, `fix:`, `docs:`, `ci:`, …). `CHANGELOG.md` is
generated from them with git-cliff, so the prefix decides which section an
entry lands in.

## Releasing

```bash
uv run bump-my-version bump minor   # writes the version, commits, tags vX.Y.Z
git cliff -c cliff.toml --tag vX.Y.Z -o CHANGELOG.md   # then hand-edit
git push --follow-tags
```

Pushing the tag runs `.github/workflows/release.yml`, which builds the
artifacts and cuts a GitHub Release using that CHANGELOG section as its notes.
