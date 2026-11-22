# Hardware

vinoWhisper targets the Intel NPU so live captioning can run continuously without competing with the CPU or GPU. GPU and CPU fallbacks exist so a missing or broken NPU driver degrades latency instead of blocking use. Because the two-cycle commit policy doubles cycle latency, slower devices directly increase caption lag.

## Device selection and benchmarks

| Device | Priority | Model export | Measured performance |
|---|---|---|---|
| **NPU** (`Intel(R) AI Boost`) | 1st | `--disable-stateful` | 0.70s per 12s window on LibriVox speech (2026-09-12); ~1.19s per 30s window (2026-08-03) |
| **GPU** (Arc / Xe) | 2nd | `stateful` | 0.95s per 12s window on Wildcat Lake Xe3 iGPU (p90 1.15–1.31s, compute-runtime 26.22, 2026-09-12); loads in 2.0s warm / 8.0s cold; 2 of 15 loads segfaulted inside OpenVINO's GPU `compile_model` (decoding never failed) |
| **CPU** | 3rd | `stateful` | 2.30s per 12s window (p90 2.68s) on Core 5 320 (2026-09-12), giving ~4.6s lag; competes with other workloads |

By default (`--device auto`), selection walks NPU → GPU → CPU. An explicit `--device NPU` fails rather than silently downgrading if the NPU is unavailable.

Fallbacks are always reported in `~/.local/state/vinowhisper/server.log`, `/health`, `vinowhisper-doctor`, and the UI status bar (red border and `⚠` warning):

```
╭─ vinoWhisper CPU ────────────────────────────────────────────────────────────╮
│ ● live    ███───────────  -48dB  ×12   ⟳ 6.4s ███▇█▇  lag ~12.8s  ⏳9  88 words │
│ ⚠ Running on the CPU. Every cycle now competes with everything else on the... │
╰──────────────────────────────────────────────────────────────────────────────╯
```

## When a device enumerates and then fails

OpenVINO can enumerate a device that later fails during `compile_model()` (for example, if a package reinstall reverts a Level Zero symlink). Without tracking failures, every server start would repeat a 10–30s load only to fail again (and under the pre-2026-11-14 systemd unit, `Restart=on-failure` hit its start limit and stopped the socket).

When pipeline creation fails on a device, the server records the device name, timestamp, and first error line in `~/.config/vinowhisper/failed-devices.json`. Subsequent `--device auto` starts skip that device and report the skip in logs, `/health`, the `Ready` event, the status bar, and `vinowhisper-doctor`. If the file is missing or unreadable, no device is skipped.

**Device resets are handled differently:** If another process faults and resets the NPU, a decode fails with a device-lost error. The server reloads the pipeline on the same device without writing to `failed-devices.json`. If the reload fails under `--device auto`, it falls back to the next device for the remainder of that run and retries the NPU on the next server start. An explicitly requested device is never swapped. The reload runs outside the `except` block so the exception traceback does not retain a reference to the dead pipeline (`tests/test_device_reset.py`).

**Clearing failure marks:**

- Running `vinowhisper-doctor` clears all recorded marks (so a repaired system is re-tested on the next start).
- A successful model load clears its own mark.
- Explicit `--device NPU` bypasses the mark with a warning, allowing manual testing after a fix.

## Model exports

The NPU and CPU/GPU exports are not interchangeable:

- **NPU (`--disable-stateful`):** Produces a separate `decoder_with_past` KV-cache submodel required by OpenVINO's static NPU pipeline. Running it on CPU fails with a `beam_idx` port error.
- **CPU / GPU (`stateful`):** Stored in a separate directory. `vinowhisper-setup`, `vinowhisper-doctor`, and the server verify that the installed export matches the active device.

```bash
./scripts/convert_model.sh --variant npu        # ~/.local/share/vinowhisper/models/whisper-small.en-ov
./scripts/convert_model.sh --variant stateful   # ...-ov-stateful
./scripts/convert_model.sh --variant both
./scripts/convert_model.sh --model openai/whisper-small   # multilingual, ...whisper-small-ov
```

During export, `optimum` writes a full-size intermediate copy to `$TMPDIR`. Because `/tmp` is RAM-backed `tmpfs` on Fedora (exporting SmolLM3-3B placed 6.15 GB there on 2026-10-27 and triggered an OOM kill), `convert_model.sh` and `vinowhisper-setup` set `TMPDIR` to `~/.cache/vinowhisper/export-tmp/` on disk and clean it up afterward. Where `systemd-run` is available, exports also run inside a user scope capped at 90% of available memory with no swap so an oversized export exits with an error instead of triggering a system OOM kill.

**Why `whisper-small.en` is the default:** Benchmarked on the NPU against `base.en` and `tiny.en` on the same clip (2026-08-03), `base.en` was 2.6x faster and `tiny.en` 3.8x faster, but both produced misheard/garbled words and `tiny.en` looped at the end. `small.en` had no errors on the clip.

Multilingual `whisper-small` (`vinowhisper-server --language fr|de|es|auto`, `--task translate`) is stored in separate directories alongside the English export. As of 2026-10-06 it has been tested only on CPU, not on the NPU static pipeline.

## When the GPU doesn't show up

OpenVINO uses OpenCL to drive Intel GPUs, which requires Intel's compute runtime (Mesa provides graphics and Vulkan, not OpenCL compute). Without `/etc/OpenCL/vendors` configured, OpenVINO lists only `CPU` and `NPU` with no warning (observed on Wildcat Lake Xe3 iGPU `8086:fd80`, driver `xe`, 2026-09-12).

`vinowhisper-doctor` compares PCI devices in `sysfs` against OpenVINO's device list:

| PCI device | Visible to OpenVINO | Doctor status |
|---|---|---|
| Intel GPU | Yes | `ok` |
| Intel GPU | No | `warn` (identifies missing ICD and prints your distro's install command) |
| AMD or NVIDIA GPU | Never | `??` (OpenVINO only supports Intel GPUs) |

On Fedora, install `sudo dnf install intel-compute-runtime`, which pulls in `intel-opencl` and `intel-level-zero` (Fedora names the loader `oneapi-level-zero`, not `level-zero`). Wildcat Lake and Panther Lake require compute-runtime 26.22 or newer (shipped in Fedora 44).

## Other vendors

- **AMD NPUs (XDNA, `amdxdna`):** Detected and reported as unsupported. OpenVINO has no AMD NPU plugin, and as of 2026-09-12 no Linux runtime runs Whisper small on XDNA (FastFlowLM supports XDNA2 and `large-v3-turbo` only). Because Intel reuses PCI class `0x1180` for thermal/telemetry devices, detection checks the `amdxdna` driver and its ID table rather than PCI class.
- **AMD and NVIDIA GPUs:** Require a non-OpenVINO backend; `whisper.cpp` over Vulkan is the leading candidate because it covers Intel, AMD, and NVIDIA without CUDA or ROCm.

`vinowhisper-setup` skips Intel NPU driver prompts on systems without an Intel NPU and offers the Intel GPU runtime only when an Intel GPU is missing OpenCL support.

## When the NPU doesn't show up

`vinowhisper-doctor` checks NPU prerequisites in order so each failure mode produces a specific fix:

1. `/dev/accel/accel0` exists (verifies the in-tree `intel_vpu` kernel driver bound the device).
2. Current user has permissions on `/dev/accel/accel0` (membership in the `render` group).
3. `intel_vpu` module presence in `/proc/modules` (advisory only, since it may be built into the kernel; `/dev/accel` is authoritative).
4. Userspace driver package for your distro, with [Intel's linux-npu-driver releases](https://github.com/intel/linux-npu-driver/releases) as fallback.

## When the userspace half is missing

Even when `/dev/accel/accel0` exists and OpenVINO enumerates `Intel(R) AI Boost`, `compile_model()` can fail silently for two userspace reasons:

- **Missing NPU compiler libraries:** Linux distributions do not package Intel's NPU compiler (Fedora's `intel-npu-driver` and other distro packages ship only the Level Zero backend). Intel distributes the compiler inside `intel-driver-compiler-npu` in its release archives. Driver 1.32.0 loads `libnpu_driver_compiler.so`, whereas 1.35.0 loads `libopenvino_intel_npu_compiler_loader.so` (which opens `libopenvino_intel_npu_compiler.so`) and falls back to the 1.32 name. `vinowhisper-doctor` inspects which symbol names the installed backend expects so mismatched versions are caught. Without the compiler, every compile fails with `ZE_RESULT_ERROR_UNSUPPORTED_FEATURE` (measured 2026-09-28: Fedora's 1.32.0 backend failed across OpenVINO 2025.4–2026.4, while Intel's 1.32.0 paired with its compiler passed on all of them).
- **Level Zero backend older than the silicon:** An outdated backend enumerates the NPU and then fails during compile with `Missing upper bound for one or more nodes` (a graph-extension protocol mismatch). A common trigger is reinstalling the distro driver package, which overwrites the soname symlink back to the older packaged `.so` and orphans a manually installed newer library.

`vinowhisper-doctor` checks both libraries unconditionally (searching `LD_LIBRARY_PATH` first to account for `setupvars.sh` toolkits) and reads embedded version strings from the `.so` files:

```
[  ok] npu: level-zero NPU backend   /usr/lib64/libze_intel_npu.so.1.35.0 (1.35.0.20260722)
[  ok] npu: NPU compiler             /usr/lib64/libopenvino_intel_npu_compiler.so (OpenVINO 2026.3.0-22159-4089686065a)
```

For a reverted symlink, the doctor prints the `ln -sf` command to restore it. For a missing compiler, the recommended fix uses [ovfetch](https://github.com/karanshukla/ovfetch):

```bash
vinowhisper-setup --ovfetch   # installs ovfetch 0.6.0+
ovfetch npu install           # installs compiler (and driver for 1.35+) into ~/.local/lib/ovfetch-npu
```

`ovfetch npu install` extracts the verified `.so` files from Intel's release archive into `~/.local/lib/ovfetch-npu` without root, `dpkg`, or image layering (for 1.35+, it installs both driver and compiler together because 1.35+ loads the compiler only from its own directory). Because `~/.local/lib/ovfetch-npu` is outside the system linker path, vinoWhisper preloads each library listed in `ovfetch-preload.txt` with `RTLD_GLOBAL` (`ovfetch.preload`) after `import openvino` and before opening the NPU. `vinowhisper-doctor` searches this prefix before system directories, and `ovfetch detect` reports it as `npu_prefix`.

Manual extraction also works (these are plain userspace `.so` files; `dpkg-deb` is available via the `dpkg` package on RPM distros as well):

```bash
tar xf linux-npu-driver-*.tar.gz
dpkg-deb -x intel-driver-compiler-npu_*.deb extracted
sudo install -m 0755 $(find extracted -name '*compiler*.so') /usr/lib64/
sudo ldconfig
```

Manually installed files are untracked by your package manager and must be updated by hand.

## Platform data from ovfetch

[ovfetch](https://github.com/karanshukla/ovfetch) maintains a weekly-refreshed table of Intel NPUs by PCI ID, minimum Intel-verified driver versions, and recorded compatible OpenVINO ranges (from Intel release notes and the kernel `ivpu` driver). When `ovfetch` is on `PATH` (`vinowhisper-setup --ovfetch`), `vinowhisper-doctor` runs `ovfetch detect` offline and adds platform status lines:

```
[  ok] npu: platform        Wildcat Lake (0xfd3e), Intel verifies driver 1.32.0 and newer
[  ok] npu: openvino range  OpenVINO 2026.4, inside 2025.4 to 2026.4 for NPU driver 1.35.0
```

These checks are optional and advisory. `ovfetch`'s OpenVINO range reflects the newest version *officially recorded* rather than the newest that works (measured 2026-09-28 on Wildcat Lake: drivers 1.32.0 and 1.35.0 ran a test model across OpenVINO 2025.0–2026.4 and `whisper-small.en` static pipeline on 2026.4), so an out-of-range warning means untested rather than broken. vinoWhisper uses `ovfetch` only for platform metadata and `ovfetch npu install`, not its OpenVINO/ONNX Runtime installer (see [architecture.md](architecture.md)).
