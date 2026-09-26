# Contributing

This was built on one Fedora/KDE laptop with an Intel NPU. The most useful
thing you can bring is what it does on *your* machine, since that is the one
thing I cannot test from here.

## Most useful

1. **A distro correction.** `vinowhisper/distro.py` maps capabilities to
   package names for eight families. Only Fedora is confirmed by use; the rest
   came from package indexes. If a command it printed was wrong for your
   distro, [open a distro issue](https://github.com/karanshukla/vinoWhisper/issues/new?template=distro_support.yml).
   It is usually a one-line fix.
2. **A PulseAudio report.** The `parec` backend is written and tested, and has
   never run on a PulseAudio-only machine.
3. **Other hardware.** Other NPU generations, Arc GPUs, the CPU fallback.
   Attach `vinowhisper-doctor --json` and a few cycles of
   `vinowhisper-caption --debug`.

## Setup

For anything that does not touch the model (the stitcher, the UI, the distro
table, capture argv), you do not need OpenVINO. This is what CI does:

```bash
uv venv
uv pip install --group dev
.venv/bin/python -m pytest
```

For the real thing, with OpenVINO and the export tooling:

```bash
uv sync --extra export
uv run vinowhisper-setup --dry-run
```

The overlay in `gui/` is plain cargo: `cargo test` from that directory.

## Before you push

```bash
uv run poe check    # ruff, ruff format --check, mypy, pytest
```

- Tests run with no NPU, no audio server and no OpenVINO. If a change can
  only be verified on hardware, say so in the PR instead of faking a test.
- A red `test_characterization_*` test means a deliberate oddity changed.
  Say in the PR whether that was the point.
- Keep code comments to one line, and only where the code would invite a
  wrong "fix". Longer reasoning goes in `docs/`.
- Measured claims carry a date.
- Commits are conventional (`feat:`, `fix:`, `docs:`, ...); the changelog is
  generated from them.

[docs/development.md](docs/development.md) has the rest: why the build config
looks the way it does, what CI runs, and how releases are cut.
