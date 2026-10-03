# Security

## What this software does with your data

Nothing leaves the machine. Audio is captured from a local PipeWire/PulseAudio
node, transcribed by a model running on local hardware, and printed to your
terminal. There is no network call in the capture or transcription path, no
telemetry, and no cloud service involved at any point. The only outbound
traffic the project ever makes is package installation and the one-time model
download from Hugging Face, both of which you trigger explicitly and the second
of which is checked against pinned digests (below).

`--record` writes audio and transcripts to a directory you name. That file is
as sensitive as whatever was playing; nothing else touches it. The files are
created readable by you alone (0600), and the directory too (0700) when the
recording creates it.

## The trust boundary

The transcription server listens on a Unix socket,
`$XDG_RUNTIME_DIR/vinowhisper/server.sock`, mode `0600` inside a `0700`
directory, so only your own user can connect to it. It is two processes rather
than one because the NPU model load costs 10-30 seconds and something has to
hold the loaded model across sessions. Anything running as your user can talk
to it and ask it to transcribe audio it sends; that is the same trust level as
anything else running as you, and the endpoint exposes nothing beyond
transcription and a health check.

Until 0.6.x it listened on `127.0.0.1:8099`, and that was weaker than this
file used to claim. A localhost port belongs to no user: any local account
could connect to it, and while it was free (before login without lingering,
after `systemctl --user stop`, or when the server was started by hand) any
account could bind it first, receive your microphone or system audio, and
answer with text that dictation would then type into your focused window. Web
pages could also send cross-site POSTs to it. None of that reaches a Unix
socket: other users cannot open it, the runtime directory is yours alone, and
a browser cannot address it.

The server refuses to start without `XDG_RUNTIME_DIR` rather than fall back to
a shared directory such as `/tmp`, and it refuses a TCP socket passed by an
old socket unit. If you installed before the move, re-run `vinowhisper-setup`
(see the CHANGELOG).

## The model download is verified

`vinowhisper-setup` and `scripts/convert_model.sh` download ~1GB from Hugging
Face and convert it into a model that then runs on your hardware. Two checks,
and they are not equally strong.

**The download is pinned and checked, and a mismatch stops the export.**
`vinowhisper/model_sources.json` pins `openai/whisper-small.en` to one commit
(`e8727524f962`, the repository's head since 2024-01-22) and records the
sha256 of every file the export reads: the safetensors weights, the configs and
the tokenizer files. `vinowhisper.source` downloads exactly those files at that
commit into `~/.local/share/vinowhisper/models/source/`, hashes each one, and
refuses the directory if anything differs, is missing, or is there unpinned.
Only then does `optimum-cli` run, from that local directory, with
`HF_HUB_OFFLINE=1`, so nothing it reads came from anywhere else. These hashes
do not depend on your toolchain, so there is no "drift" here: a difference
is a failure. Recorded 2026-10-02 from the Hub API (the safetensors' LFS
sha256) and from the files themselves, each checked against its git blob id.

**The export is then hashed against `vinowhisper/model_digests.json`**, the
sha256 of every file in the export this project has run on an NPU. The check
runs before you are told the export is done, and `vinowhisper-doctor` repeats
it on demand. This one can only warn in the common case, because the export is
not reproducible across toolchains (below), and it is weaker than it looks:

- **`drift` is the export's own claim.** Bytes that differ from the pin are
  called `drift` (warn, continue) rather than `mismatch` (fail) when the
  toolchain recorded in the export's `rt_info` differs from the pinned one.
  That `rt_info` is inside the files being checked, so anything that can
  rewrite the export can claim a different toolchain and get the softer
  verdict. The source check above is what protects the download; this check
  catches accidents and a stale export, not a determined local attacker (who,
  running as you, could edit the code doing the checking anyway).
- **A fresh install currently reports `drift`.** As of 2026-10-02 `uv.lock`
  resolves openvino 2026.4.1, optimum-intel 2.2.0 and torch 2.14.1, and the
  NPU pin was recorded with 2026.3.1, 2.1.0 and 2.13.0. A test now fails when
  the lock moves further from the pin; closing the existing gap needs a
  re-export and re-pin on an NPU.
- **The stateful (CPU/GPU) export has no pin at all**, so it always verifies
  as `unpinned`. Its source is checked exactly like the NPU export's.

Further limits worth stating plainly rather than implying:

- **It pins one export, not every export.** An unrecognised model or variant
  verifies as `unpinned`, which warns and continues. A hard failure there would
  make the tool unusable the first time anyone exported something new, and this
  project ships exactly one pinned export.
- **The pin also records toolchains measured to produce a broken export**, not
  only tampered bytes. That is not an integrity property and it lives here
  because this file is already the record of which toolchain the pinned export
  came from. See `known_bad` in the pin file.
- **A pin outlives about one toolchain.** The export is bit-reproducible on a
  fixed toolchain (measured 2026-09-04, two runs, 16 of 16 files identical) and
  is not across one, so a `drift` result is reported separately from bytes
  changing under the toolchain that produced the pin. Drift warns. Only the
  same toolchain producing different bytes is treated as alarming.

Re-pinning is `./scripts/update_digests.py`, deliberately a script and
deliberately not automatic: the diff it produces is a list of hashes, and it is
meant to be read in review.

### Open advisories in the export toolchain

The export extra holds `transformers<5.4`, which resolves 5.3.0, because 5.4.0
and later export a decoder the NPU cannot run (bisected 2026-09-04). 5.3.0 has
two open advisories, and neither fix is reachable: every optimum-intel release
caps transformers below 5.6.

- [GHSA-fgcw-684q-jj6r](https://github.com/advisories/GHSA-fgcw-684q-jj6r)
  (CVE-2026-5241, fixed in 5.5.0): a crafted model repository can run code
  at load time despite `trust_remote_code` being off.
- [GHSA-xrqw-3rrv-vx5w](https://github.com/advisories/GHSA-xrqw-3rrv-vx5w)
  (CVE-2026-9856, fixed in 5.10.0): a crafted `tokenizer_config.json` makes
  `save_pretrained()` write outside the output directory.

Both need attacker-controlled model files to reach transformers. Nothing at
runtime imports transformers; only the one-time export does. For the default
model, the revision pin and the source hashes mean the files transformers
reads are byte-for-byte the ones recorded above, so a compromised or
force-pushed Hugging Face repository, or anything between you and it, is
refused before the export starts. That does not fix the bugs; it keeps
untrusted input away from them. **`--model` with any other repository has no
pin** (`convert_model.sh` says so and exports whatever that repository's
default branch holds), and is exposed to both advisories: only export
repositories you trust.

## The setup wizard runs commands

`vinowhisper-setup` prints every command before running it and asks first,
including the ones with `sudo`. `--yes` skips the asking, which is what it is
for; `--dry-run` prints the plan and changes nothing. The package names it
suggests come from a static table in `vinowhisper/distro.py` — they are never
fetched from anywhere, so a network answer cannot decide what gets installed.

`scripts/install.sh` is a `curl | bash` installer, with the usual caveats.
Read it first if that matters to you; it is ~180 lines and deliberately
readable, and it installs no system packages itself — it hands that decision to
the wizard. What it does to narrow those caveats: everything runs from one
function called on the last line, so a truncated download runs nothing; it
installs the latest release tag, not `main`; the uv it installs (only if you
have none) is a pinned version; and `uv sync --locked` installs exactly what
`uv.lock` pins.

## Reporting a vulnerability

Open a [security advisory](https://github.com/karanshukla/vinoWhisper/security/advisories/new)
rather than a public issue. This is a personal project, so expect a
best-effort, spare-time response rather than an SLA.
