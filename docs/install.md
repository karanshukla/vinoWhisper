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
get you: an NPU driver, a model export, or systemd units. Run
`vinowhisper-setup` afterwards for those, exactly as the installer script would.

**Python must be 3.11-3.13.** 3.14 made `functools.partial` a descriptor, which
breaks `optimum`'s `NORMALIZED_CONFIG_CLASS = SomeConfig.with_args(...)`
class-attribute idiom outright. Version-independent root cause, confirmed
2026-08-03 across every optimum/transformers pairing tried. `requires-python`
enforces it, so pip will refuse rather than install something broken.

## The OpenVINO version floor

`openvino>=2026.3.1`, and the floor is exact rather than cautious: 2026.3.1 is
the first *stable* release that can build the NPU static Whisper pipeline.

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

## Pinning it on top

The status bar is Rich in an ordinary terminal, so keeping it above other
windows is a window-manager job, not the app's. On KWin: System Settings >
Window Management > Window Rules, match the terminal window, set Keep Above
Other Windows to Force/Yes, plus Skip Taskbar and Skip Pager if you want it out
of the way. No titlebar and a small fixed size make it read like an overlay
rather than a terminal.
