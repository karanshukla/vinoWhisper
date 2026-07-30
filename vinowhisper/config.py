"""Shared paths and constants."""

from pathlib import Path
import os

MODEL_DIR = Path.home() / ".local/share/vinowhisper/models/whisper-small.en-ov"

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8099
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
PIDFILE = RUNTIME_DIR / "vinowhisper-recorder.pid"
RECORDING_WAV = RUNTIME_DIR / "vinowhisper-capture.wav"

SAMPLE_RATE_HZ = 16_000

# Self-exit after this long with no requests. Systemd socket activation
# (vinowhisper-server.socket) respawns the process on the next connection —
# scale-to-zero for the NPU model instead of holding it resident forever.
IDLE_TIMEOUT_S = 30 * 60
