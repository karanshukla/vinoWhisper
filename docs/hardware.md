# Hardware, and what happens when you don't have it

The NPU is the point. Everything below it exists so a broken driver degrades
the tool instead of bricking it. The latency design assumes NPU-class cycle
times, and the two-cycle commit policy doubles any regression.

| Device | Selected | Model export | What you get |
|---|---|---|---|
| **NPU** (`Intel(R) AI Boost`) | first | `--disable-stateful` | 0.70s per 12s window on LibriVox speech, measured 2026-09-12 (~1.19s per 30s window, 2026-08-03) |
| **GPU** (Arc / Xe) | second | stateful | 0.95s per 12s window on the Wildcat Lake Xe3 iGPU (p90 1.15-1.31s) with compute-runtime 26.22, measured 2026-09-12. Loads in 2.0s warm, 8.0s the first time. 2 of 15 loads segfaulted inside the GPU plugin's `compile_model`; decoding never failed |
| **CPU** | last resort | stateful | 2.30s per 12s window (p90 2.68s) on a Core 5 320, measured 2026-09-12, so lag lands near 4.6s. Competes with everything else on the machine |

Selection is automatic (`--device auto`). An *explicit* `--device NPU` that
isn't available is refused rather than quietly downgraded, because someone who
typed it wants to know it didn't happen. The order itself is not configurable:
there is no machine where the GPU over the NPU is the right default for this
workload.

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

## When the GPU doesn't show up

OpenVINO drives Intel GPUs through OpenCL, so it needs Intel's compute
runtime, which a desktop install usually lacks: Mesa covers graphics and
Vulkan, not OpenCL compute. Without it OpenVINO lists `CPU` and `NPU`, and
nothing says why. That was this laptop until 2026-09-12: an Xe3 iGPU
(`8086:fd80`, driver `xe`), the OpenCL ICD loader, and no
`/etc/OpenCL/vendors` at all.

`vinowhisper-doctor` reads the PCI bus from sysfs and compares it with what
OpenVINO enumerated, so it tells these apart:

| On the PCI bus | OpenVINO sees it | Doctor says |
|---|---|---|
| Intel GPU | yes | `ok` |
| Intel GPU | no | `warn`, names what is missing (no ICD, or only another vendor's) and prints the install command for your distro |
| AMD or NVIDIA GPU | never | `??`, since OpenVINO only drives Intel GPUs, so it is not a fallback |

On Fedora the runtime is one package, `sudo dnf install intel-compute-runtime`,
a metapackage that pulls in `intel-opencl` and `intel-level-zero`. There is no
`level-zero` package on Fedora (the loader is `oneapi-level-zero`), which the
install lines here named until 2026-09-12, so they failed outright. Wildcat
Lake and Panther Lake need compute-runtime 26.22 or later; Fedora 44 ships
26.22.

## Other vendors

Neither has a path yet, and both are detected rather than ignored:

- **AMD NPUs** (XDNA, driver `amdxdna`) are reported as present and unusable.
  OpenVINO has no AMD NPU plugin, and as of 2026-09-12 no Linux runtime runs
  Whisper small on one. The only Linux Whisper on an AMD NPU is FastFlowLM,
  which is XDNA2-only and large-v3-turbo-only. AMD NPUs report PCI class
  `0x1180`, which Intel reuses for thermal and telemetry devices, so they are
  recognised by the `amdxdna` driver or its ID table, not by class.
- **AMD and NVIDIA GPUs** need an engine other than OpenVINO. whisper.cpp over
  Vulkan is the candidate, since it reaches all three vendors without ROCm or
  CUDA.

The wizard no longer offers the Intel NPU driver on a machine with no Intel
NPU, and offers the GPU runtime where that is what's actually missing.

## When the NPU doesn't show up

`vinowhisper-doctor` walks it in fix-first order rather than reporting "no NPU"
and stopping: the `/dev/accel/accel0` node (does the in-tree `intel_vpu` driver
have the device?), its permissions (are you in the `render` group?), the
`intel_vpu` module, and then the userspace driver package for your distro, with
[Intel's release page](https://github.com/intel/linux-npu-driver/releases) as
the authoritative fallback. Those have different fixes and are indistinguishable
from OpenVINO's device list alone. A missing `intel_vpu` in `/proc/modules` is
weak evidence on its own, since the driver can be built into the kernel; the
`/dev/accel` node is the check that decides.

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
those versions come from. The search tries `LD_LIBRARY_PATH` first, since a
toolkit sourced through `setupvars.sh` is how these libraries end up outside
the system directories. A reverted symlink prints the `ln -sf` that selects
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
