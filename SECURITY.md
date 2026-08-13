# Security

## What this software does with your data

Nothing leaves the machine. Audio is captured from a local PipeWire/PulseAudio
node, transcribed by a model running on local hardware, and printed to your
terminal. There is no network call in the capture or transcription path, no
telemetry, and no cloud service involved at any point. The only outbound
traffic the project ever makes is package installation and the one-time model
download from Hugging Face, both of which you trigger explicitly.

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
