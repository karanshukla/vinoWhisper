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
as sensitive as whatever was playing; nothing else touches it.

## The trust boundary

The transcription server binds `127.0.0.1:8099` and is loopback-only. It is
two processes rather than one because the NPU model load costs 10-30 seconds
and something has to hold the loaded model across sessions — not because of
any isolation goal. Anything running as your user on your machine can talk to
it and ask it to transcribe audio you send it. That is the same trust level as
anything else running as you, and the endpoint exposes nothing beyond
transcription and a health check.

If that boundary matters to you, a Unix domain socket in `$XDG_RUNTIME_DIR`
would be a marginally tighter swap and would be an accepted change.

## The model download is verified

`vinowhisper-setup` and `scripts/convert_model.sh` download ~1GB from Hugging
Face and convert it into a model that then runs on your hardware. Every file in
the result is hashed and compared against `vinowhisper/model_digests.json`,
which pins the sha256 of the export this project has actually run on an NPU.
The check runs before you are told the export is done, and
`vinowhisper-doctor` repeats it on demand.

Two limits worth stating plainly rather than implying:

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

## The setup wizard runs commands

`vinowhisper-setup` prints every command before running it and asks first,
including the ones with `sudo`. `--yes` skips the asking, which is what it is
for; `--dry-run` prints the plan and changes nothing. The package names it
suggests come from a static table in `vinowhisper/distro.py` — they are never
fetched from anywhere, so a network answer cannot decide what gets installed.

`scripts/install.sh` is a `curl | bash` installer, with the usual caveats.
Read it first if that matters to you; it is ~150 lines and deliberately
readable, and it installs no system packages itself — it hands that decision to
the wizard.

## Reporting a vulnerability

Open a [security advisory](https://github.com/karanshukla/vinoWhisper/security/advisories/new)
rather than a public issue. This is a personal project, so expect a
best-effort, spare-time response rather than an SLA.
