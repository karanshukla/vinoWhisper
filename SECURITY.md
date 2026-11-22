# Security

## Local data handling

Audio is captured from a local PipeWire or PulseAudio node, transcribed on local hardware, and displayed in your terminal or overlay. The capture and transcription paths make no network calls, collect no telemetry, and use no cloud services.

OpenVINO bundles Intel's Google Analytics telemetry (`openvino_telemetry`), which treats a missing consent file as opt-in. vinoWhisper blocks `openvino_telemetry` from being imported inside its own process before `openvino` loads, forcing the no-op stub without modifying your `~/intel/openvino_telemetry` file. Because `optimum-cli` runs in a separate process, the model export sets `CI=true`, which OpenVINO's converter honors (`disable_in_ci`). The only outbound network requests are user-triggered package installs and one-time model downloads from Hugging Face, which are checked against pinned digests.

Optional dictation LLM cleanup (`VINOWHISPER_CLEANUP=1` or Settings → Cleanup) also runs locally inside the same server. (Before 2026-10-27 it sent dictation text to a configurable API endpoint; that code has been removed.) The cleanup model is downloaded once from Hugging Face, verified against commit and per-file SHA-256 hashes in `vinowhisper/cleanup_model.json`, and re-hashed before loading whenever a file's size, mtime, or inode changes. Output validation enforces pure word deletion—a tampered model can drop words, but cannot insert or reword them. Every cleanup request has a 10-second server timeout. See "LLM cleanup" in `docs/tray.md`.

- `--record` writes audio and event logs to a directory you specify, creating files as `0600` and newly created directories as `0700`.
- `--transcript` (off by default) writes confirmed text with the same permissions and is never deleted automatically. Both files are as sensitive as the audio you captured.

## Server trust boundary

The transcription server listens on a Unix domain socket at `$XDG_RUNTIME_DIR/vinowhisper/server.sock` (mode `0600` inside a `0700` directory), restricting connections to your user account. Using a separate server process avoids repeating the 10–30 second NPU model load on every session. Any process running as your user can connect to the socket to transcribe audio, run text cleanup, check `/health`, or stop the server via `POST /quit`.

Before 0.6.x, the server listened on `127.0.0.1:8099`, which allowed any local account to connect, preemptively bind the port before the server started (intercepting audio and injecting dictated text), or receive cross-site POST requests from browsers. Moving to a user-private Unix socket eliminates those exposures.

The server refuses to start if `XDG_RUNTIME_DIR` is unset (rather than falling back to `/tmp`) and rejects TCP sockets passed by legacy systemd units. If upgrading from a pre-socket release, re-run `vinowhisper-setup`.

Since 2026-11-14, clients start the server directly on demand rather than through a systemd socket unit. Removing the unit also removed its seccomp-backed options (address family, namespace, and personality restrictions), while the server continues to set umask `0077` and `PR_SET_NO_NEW_PRIVS` on startup.

## Model download and export verification

`vinowhisper-setup` and `scripts/convert_model.sh` download ~1 GB from Hugging Face and export it to OpenVINO IR using two distinct checks:

1. **Pinned source download (enforced):** `vinowhisper/model_sources.json` pins `openai/whisper-small.en` to commit `e8727524f962` (HEAD since 2024-01-22) and lists the SHA-256 digest of every file the export reads (safetensors weights, configs, and tokenizer files, recorded 2026-10-02 from Hub API LFS hashes and verified against git blob IDs). `vinowhisper.source` downloads only those files into `~/.local/share/vinowhisper/models/source/`, hashes every file, and aborts on any missing, altered, or unpinned file. `optimum-cli` then runs offline from that directory with `HF_HUB_OFFLINE=1`.
2. **Exported IR digest check (advisory across toolchains):** `vinowhisper/model_digests.json` records the SHA-256 of every file in the reference NPU export. Setup checks the export after conversion, and `vinowhisper-doctor` can re-run the check on demand.

Important limitations of the exported IR check:

- **`drift` trusts the export's own metadata:** If exported bytes differ from the pin and the toolchain recorded in the export's `rt_info` differs from the pinned toolchain, the check reports `drift` (warning) rather than `mismatch` (error). Because `rt_info` lives inside the checked files, a local attacker able to modify the export could spoof `rt_info` to trigger `drift` (though a process running as your user could also modify the verification code). The source download check protects the upstream download; the IR check catches accidental corruption and stale exports.
- **Current fresh installs report `drift`:** As of 2026-10-02, `uv.lock` resolves OpenVINO 2026.4.1, `optimum-intel` 2.2.0, and `torch` 2.14.1, while the NPU pin was recorded on 2026.3.1, 2.1.0, and 2.13.0. A CI test prevents further divergence, and closing the gap requires re-exporting and re-pinning on NPU hardware.
- **Stateful exports and custom models are `unpinned`:** Only the default NPU export has a digest pin. The stateful (CPU/GPU) export and custom `--model` exports report `unpinned` and continue with a warning, though pinned models still pass through the source download check.
- **Toolchain reproducibility:** Exports are bit-reproducible on a fixed toolchain (measured 2026-09-04: two runs produced 16 of 16 identical files), but change across toolchain versions. `model_digests.json` also tracks `known_bad` toolchains known to produce broken exports.

To update pins after a verified NPU export, run `./scripts/update_digests.py` and review the resulting hash diff before committing.

### Open advisories in the export toolchain

The `export` extra pins `transformers<5.4` (resolving 5.3.0) because 5.4.0+ exports a decoder the NPU cannot execute (bisected 2026-09-04). Neither of the following advisories can currently be resolved by upgrading, as released `optimum-intel` versions also cap `transformers` below 5.6:

- [GHSA-fgcw-684q-jj6r](https://github.com/advisories/GHSA-fgcw-684q-jj6r) (CVE-2026-5241, fixed in 5.5.0): crafted model repository can execute code on load even with `trust_remote_code` disabled.
- [GHSA-xrqw-3rrv-vx5w](https://github.com/advisories/GHSA-xrqw-3rrv-vx5w) (CVE-2026-9856, fixed in 5.10.0): crafted `tokenizer_config.json` can cause `save_pretrained()` to write outside the output directory.

Nothing at runtime imports `transformers`; it is used only during the one-time export. For the default model, the commit pin and source SHA-256 verification ensure `transformers` only reads verified files. **Passing `--model` with an unpinned repository bypasses source verification** (`convert_model.sh` warns and exports the repository's default branch) and is exposed to both advisories—only export repositories you trust.

## Setup wizard and installer script

`vinowhisper-setup` prints every command (including `sudo` commands) and prompts before executing it. Pass `--yes` to accept prompts automatically or `--dry-run` to print the plan without changes. Suggested package names come from a static table in `vinowhisper/distro.py` and are never fetched from the network.

`scripts/install.sh` (~180 lines) can be inspected before running. It installs no system packages itself and delegates system changes to `vinowhisper-setup`. Its main body is wrapped in a single function invoked on the last line so truncated downloads do not execute, it checks out the latest release tag rather than `main`, downloads a hash-pinned `uv` release if needed, and runs `uv sync --locked` against `uv.lock`.

## Reporting a vulnerability

Please open a [GitHub security advisory](https://github.com/karanshukla/vinoWhisper/security/advisories/new) instead of a public issue. As a personal project, responses are best-effort rather than under an SLA.
