# Contributing

vinoWhisper is developed on a single Fedora/KDE laptop with an Intel NPU. Reports from other distributions, audio stacks, and hardware are especially helpful.

## High-value reports

1. **Distribution package fixes:** `vinowhisper/distro.py` maps dependencies to package names across eight distro families, but only Fedora has been tested directly. If a suggested command is wrong for your distribution, [open a distro issue](https://github.com/karanshukla/vinoWhisper/issues/new?template=distro_support.yml).
2. **PulseAudio testing:** The `parec` capture backend is covered by unit tests, but has not been run on a PulseAudio-only system.
3. **Other hardware:** Reports from other Intel NPU generations, Arc GPUs, or CPU fallback runs. Include `vinowhisper-doctor --json` and a few cycles of `vinowhisper-caption --debug`.

## Development setup

You do not need OpenVINO to work on the stitcher, terminal UI, distro tables, or capture command building. CI uses this minimal environment:

```bash
uv venv
uv pip install --group dev
.venv/bin/python -m pytest
```

For full runtime and model export dependencies:

```bash
uv sync --extra export
uv run vinowhisper-setup --dry-run
```

The overlay (`tray/`) and settings window (`gui/`) are standard Cargo projects; run `cargo test` in either directory. To install and run your local build, see [Running your own build](docs/development.md#running-your-own-build).

## Before pushing

```bash
uv run poe check    # ruff, ruff format --check, mypy, pytest
```

- **No hardware in unit tests:** Tests run without an NPU, audio server, or OpenVINO. If a change requires hardware verification, state that in the PR rather than adding a mock that hides it.
- **Characterization tests:** A failing `test_characterization_*` test indicates an intentional behavior changed. Note in the PR whether the change was intended.
- **Code comments:** Extract helper functions, use clear names, or pin invariants with tests before adding comments, and cite governing tests from code. See [docs/comment-style.md](docs/comment-style.md); the Doc Links CI job verifies these citations. Put longer design rationale in `docs/`.
- **Dated measurements:** Include the date on any benchmark or hardware measurement.
- **Commit format:** Use conventional commits (`feat:`, `fix:`, `docs:`, etc.), which are used to generate `CHANGELOG.md`.

See [docs/development.md](docs/development.md) for packaging details, CI jobs, and the release process.
