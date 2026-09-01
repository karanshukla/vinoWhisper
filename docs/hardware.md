# Hardware, and what happens when you don't have it

The NPU is the point. Everything below it exists so a broken driver degrades
the tool instead of bricking it. The latency design assumes NPU-class cycle
times, and the two-cycle commit policy doubles any regression.

| Device | Selected | Model export | What you get |
|---|---|---|---|
| **NPU** (`Intel(R) AI Boost`) | first | `--disable-stateful` | ~1.19s per 30s window, measured 2026-08-03 |
| **GPU** (Arc / Xe) | second | stateful | Untested here. Works in principle; watch the lag figure |
| **CPU** | last resort | stateful | Runs. Competes with everything else on the machine, and lags |

Selection is automatic (`--device auto`). An *explicit* `--device NPU` that
isn't available is refused rather than quietly downgraded, because someone who
typed it wants to know it didn't happen.

A fallback is never silent. It appears in the server's journal, in `/health`,
in `vinowhisper-doctor`, and on the status bar as a red border and a `⚠` line:

```
╭─ vinoWhisper CPU ────────────────────────────────────────────────────────────╮
│ ● live    ███───────────  -48dB  ×12   ⟳ 6.4s ███▇█▇  lag ~12.8s  ⏳9  88 words │
│ ⚠ Running on the CPU. Every cycle now competes with everything else on the... │
╰──────────────────────────────────────────────────────────────────────────────╯
```

**The two model exports are not interchangeable.** The NPU needs
`--disable-stateful`, which produces the separate `decoder_with_past` KV-cache
submodel its static pipeline requires. That same export cannot run on CPU at
all: it fails on a `beam_idx` port error, which is incidentally how the NPU
was confirmed to be doing real work rather than silently falling back. So the
CPU/GPU path needs a second export in a second directory, and the wizard, the
doctor and the server all check which one you have against the device you got.

```bash
./scripts/convert_model.sh --variant npu        # ~/.local/share/vinowhisper/models/whisper-small.en-ov
./scripts/convert_model.sh --variant stateful   # ...-ov-stateful
./scripts/convert_model.sh --variant both
```

## When the NPU doesn't show up

`vinowhisper-doctor` walks it in fix-first order rather than reporting "no NPU"
and stopping: the `/dev/accel/accel0` node (does the in-tree `intel_vpu` driver
have the device?), its permissions (are you in the `render` group?), the
`intel_vpu` module, and then the userspace driver package for your distro, with
[Intel's release page](https://github.com/intel/linux-npu-driver/releases) as
the authoritative fallback. Those have different fixes and are indistinguishable
from OpenVINO's device list alone.

## When the userspace half is missing

The kernel checks above can all pass, OpenVINO can enumerate
`Intel(R) AI Boost`, and `compile_model()` can still fail. The NPU stack has a
userspace half that device enumeration does not exercise, and it goes wrong two
ways, both silently:

- **The compiler libraries are absent.** No distro packages
  `libopenvino_intel_npu_compiler.so`. Fedora's `intel-npu-driver` rpm ships
  the level-zero backend and stops there, and the same holds for every family
  in the table. Intel ships it only inside `intel-driver-compiler-npu`, in the
  release archive, so waiting for a package upgrade never fixes it.
- **The level-zero backend is older than the silicon.** An older backend
  enumerates the device perfectly well and then fails at compile time with
  `Missing upper bound for one or more nodes`, which reads as a model problem
  and is actually a graph-extension protocol mismatch.

The second one has a nastier variant. A hand-installed `.so` is untracked by
the package manager, so reinstalling the distro package rewrites the soname
symlink back to the packaged version and orphans the newer one, turning a
working machine into that failure with nothing on screen to say why.

`vinowhisper-doctor` reports both, and reports them whether or not the NPU
enumerated, because "the NPU is there" is not evidence that either is right:

```
[  ok] npu: level-zero NPU backend   /usr/lib64/libze_intel_npu.so.1.35.0 (1.35.0.20260722)
[  ok] npu: NPU compiler             /usr/lib64/libopenvino_intel_npu_compiler.so (OpenVINO 2026.3.0-22159-4089686065a)
```

Each library carries its own provenance as an embedded string, which is where
those versions come from. A reverted symlink prints the `ln -sf` that selects
the newer backend again. A missing compiler prints the extraction steps, which
work unchanged on an rpm distro because these are plain userspace `.so` files
with no kernel-module or packaging-system dependency:

```bash
tar xf linux-npu-driver-*.tar.gz
dpkg-deb -x intel-driver-compiler-npu_*.deb extracted
sudo install -m 0755 $(find extracted -name 'libopenvino_intel_npu_compiler*.so') /usr/lib64/
sudo ldconfig
```

`dpkg-deb` is in the `dpkg` package and is present on rpm distros too, so no
`alien` or `rpm2cpio` conversion is needed. The result is untracked by your
package manager: note it somewhere, because nothing will upgrade it.
