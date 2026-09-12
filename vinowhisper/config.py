import os
from pathlib import Path


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


MODEL_ID = "openai/whisper-small.en"
MODEL_ROOT = _data_home() / "vinowhisper/models"

MODEL_DIR = MODEL_ROOT / "whisper-small.en-ov"
STATEFUL_MODEL_DIR = MODEL_ROOT / "whisper-small.en-ov-stateful"


def model_dir(device_kind: str) -> Path:
    return MODEL_DIR if device_kind.upper() == "NPU" else STATEFUL_MODEL_DIR


DEFAULT_DEVICE = "auto"

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8099
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

SAMPLE_RATE_HZ = 16_000

DEFAULT_SOURCE = "output"

# Hard ceiling: WhisperPipeline's streamer only handles audio under 30s.
MAX_WINDOW_S = 29.5

WINDOW_S = 12.0

MIN_WINDOW_S = 1.5

MIN_HOP_S = 0.5

# Low on purpose: quiet speech sits at the noise floor (docs/audio.md).
SILENCE_RMS_THRESHOLD = 0.002

TARGET_RMS = 0.05
MAX_GAIN = 20.0

CONNECT_TIMEOUT_S = 5.0
MODEL_LOAD_TIMEOUT_S = 180.0
REQUEST_TIMEOUT_S = 60.0

IDLE_TIMEOUT_S = 30 * 60
IDLE_CHECK_INTERVAL_S = 30.0


CONVERT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "convert_model.sh"


def export_command(variant: str) -> str:
    if CONVERT_SCRIPT.is_file():
        return f"./scripts/convert_model.sh --variant {variant}"
    return "vinowhisper-setup"
