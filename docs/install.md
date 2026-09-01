# Installing

The short version is in the [README](../README.md). This is what the
installer actually does, and the dependency pin behind it.

```bash
curl -fsSL https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/scripts/install.sh | bash
```

That installs [uv](https://docs.astral.sh/uv/), clones the repo, builds the
environment, and hands over to `vinowhisper-setup`, which is where every
machine-specific decision happens: your capture tool, your NPU driver, the
model export your device needs, and systemd units generated against the paths
that actually exist. It prints every command before running it and asks first.

From a checkout, or to see what it would do without doing it:

```bash
git clone https://github.com/karanshukla/vinoWhisper && cd vinoWhisper
uv sync
uv run vinowhisper-setup --dry-run   # the whole plan, nothing changed
uv run vinowhisper-setup             # for real, one prompt per step
```

**Why not `pip install vinowhisper`.** It would resolve `openvino` from PyPI,
where the builds that can construct the NPU static Whisper pipeline do not
exist yet, and the pin lives in `[tool.uv.sources]`, which pip does not read. An
install that succeeds and then cannot load a model is worse than no install, so
that path is deliberately not offered. See [the nightly note](#openvino-nightly-and-why)
below.

## OpenVINO nightly, and why

Three things `uv sync` handles that a bare `pip install -e .` does not, all
encoded in `pyproject.toml` rather than passed as flags:

1. **Python is held below 3.14.** 3.14 made `functools.partial` a descriptor,
   which breaks `optimum`'s `NORMALIZED_CONFIG_CLASS = SomeConfig.with_args(...)`
   class-attribute idiom outright. Version-independent root cause, confirmed
   2026-08-03 across every optimum/transformers pairing tried.
2. **The three `openvino*` packages route to the nightly wheel index**, with
   prereleases allowed. Stable `openvino-genai` (2026.2.1 as of writing) cannot
   build the NPU static Whisper pipeline: its pattern-matcher does not recognise
   the current export's SDPA attention-mask node shape.
3. **The nightly index is `explicit = true`**, so it adds to PyPI rather than
   replacing it.

This is the project's standing dependency risk, and it is watched rather than
assumed: `.github/workflows/deps-canary.yml` runs the full resolve weekly, off
the pull-request path, precisely because nightly builds get pruned upstream on
their own schedule. The outcome to hope for is that a stable OpenVINO release
catches up and the whole nightly pin can be deleted.

## Pinning it on top

The status bar is Rich in an ordinary terminal, so keeping it above other
windows is a window-manager job, not the app's. On KWin: System Settings >
Window Management > Window Rules, match the terminal window, set Keep Above
Other Windows to Force/Yes, plus Skip Taskbar and Skip Pager if you want it out
of the way. No titlebar and a small fixed size make it read like an overlay
rather than a terminal.
