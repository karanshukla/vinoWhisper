"""Shared paths and constants."""

from pathlib import Path

MODEL_DIR = Path.home() / ".local/share/vinowhisper/models/whisper-small.en-ov"

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8099
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

SAMPLE_RATE_HZ = 16_000

# "output" captures system audio (whatever's playing — a video, etc.),
# "mic" captures the physical microphone. Default is "output" since live
# captioning videos is the actual use case this was built for.
DEFAULT_SOURCE = "output"

# WhisperPipeline's streamer callback only supports audio under 30s: keep
# the rolling capture buffer just under that so live captioning can always
# use streaming output.
WINDOW_S = 29.5

# Self-exit after this long with no requests. Systemd socket activation
# (vinowhisper-server.socket) respawns the process on the next connection —
# scale-to-zero for the NPU model instead of holding it resident forever.
IDLE_TIMEOUT_S = 30 * 60
