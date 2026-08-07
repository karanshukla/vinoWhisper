"""Shared paths and tunables — single source of truth for server and client."""

from pathlib import Path

MODEL_DIR = Path.home() / ".local/share/vinowhisper/models/whisper-small.en-ov"

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8099
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

SAMPLE_RATE_HZ = 16_000

# "output" captures system audio (whatever's playing — a video, etc.), "mic"
# captures the physical microphone. Default is "output" since live captioning
# videos is the actual use case this was built for.
DEFAULT_SOURCE = "output"

# --- Window sizing -------------------------------------------------------
#
# MAX_WINDOW_S is a hard ceiling, not a preference: WhisperPipeline's streamer
# callback only supports short-form (<30s) audio, and it's also how big the
# capture ring buffer gets allocated.
MAX_WINDOW_S = 29.5

# WINDOW_S is what actually gets sent per cycle, and it's the main latency
# lever. Whisper's *encoder* cost is fixed (it pads to 30s regardless), but
# decoding is autoregressive — one forward pass per token — so a window packed
# with 29.5s of dense speech emits ~2.5x the tokens of a 12s one and takes
# correspondingly longer per cycle. The caption loop is synchronous and the
# commit policy is LocalAgreement-2, so user-visible lag is roughly
# 2 x cycle_time: every second shaved off a cycle is two seconds off the
# captions. 12s still leaves multiple seconds of overlap at realistic cycle
# times, which is all the stitcher needs. Tune with --window.
WINDOW_S = 12.0

# Don't transcribe a nearly-empty buffer at startup — Whisper hallucinates
# freely on sub-second clips.
MIN_WINDOW_S = 1.5

# Don't re-transcribe until at least this much *new* audio exists. Without it,
# a fast cycle re-decodes a window that's ~99% identical to the last one: pure
# NPU burn for no new words.
MIN_HOP_S = 0.5

# --- Levels --------------------------------------------------------------
#
# True digital silence on a sink monitor measures ~0.0-0.004 RMS (dither and
# processing noise from the EQ filter chain, not exactly zero). This gate only
# exists to avoid burning NPU cycles on dead air — it is NOT the defense
# against silence-hallucination, that's stitch.py's LocalAgreement-2 commit
# policy plus collapse_repeats. So it's tuned low on purpose: the cost of
# being too low is wasted cycles, the cost of being too high is dropping real
# quiet speech, and real quiet speech genuinely sits at the noise floor.
SILENCE_RMS_THRESHOLD = 0.002

# Whisper accuracy degrades on quiet input, so boost quiet windows to a
# speech-like level before sending. Boost only, never attenuate.
#
# This used to be justified by the sink monitor being post-volume. It isn't:
# measured 2026-08-07, the monitor holds ~0.7-0.9x of the playing app's level
# at both 100% and 20% sink volume, so the volume slider does not reach it.
# What remains is genuinely quiet *source* material, which is real and common
# enough (0.014 rms on ordinary web video) to keep this.
TARGET_RMS = 0.05
MAX_GAIN = 20.0

# --- Networking ----------------------------------------------------------
#
# The server is socket-activated, so the very first request blocks through the
# NPU model load (~10-30s). Health-check with the long timeout up front so a
# cold start is visible as "warming up" rather than as a stalled caption loop.
CONNECT_TIMEOUT_S = 5.0
MODEL_LOAD_TIMEOUT_S = 180.0
REQUEST_TIMEOUT_S = 60.0

# Self-exit after this long with no requests. Systemd socket activation
# (vinowhisper-server.socket) respawns the process on the next connection —
# scale-to-zero for the NPU model instead of holding it resident forever.
IDLE_TIMEOUT_S = 30 * 60
IDLE_CHECK_INTERVAL_S = 30.0
