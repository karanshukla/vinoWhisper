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

## The model export, and what verifies it

Exporting downloads ~1GB from Hugging Face and converts it to OpenVINO IR that
then runs on your hardware. `vinowhisper/model_digests.json` pins the sha256 of
every file in the export this project has actually run, and both
`scripts/convert_model.sh` and `vinowhisper-setup` check what came down against
it. `vinowhisper-doctor` re-checks it on demand, at about 1.2s for 1.5GB.

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
block rather than from whatever happens to be installed.

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

Until upstream fixes it, the pin's `known_bad` entry carries a floor at
transformers 5.4.0, so `vinowhisper-setup` and `convert_model.sh` report a
`known_bad` export rather than handing over a model that fails later.

## Pinning it on top

The status bar is Rich in an ordinary terminal, so keeping it above other
windows is a window-manager job, not the app's. On KWin: System Settings >
Window Management > Window Rules, match the terminal window, set Keep Above
Other Windows to Force/Yes, plus Skip Taskbar and Skip Pager if you want it out
of the way. No titlebar and a small fixed size make it read like an overlay
rather than a terminal.
