import os
from pathlib import Path


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


MODEL_ID = "openai/whisper-small.en"
MODEL_ROOT = _data_home() / "vinowhisper/models"
TRANSCRIPT_DIR = _data_home() / "vinowhisper/transcripts"

MODEL_DIR = MODEL_ROOT / "whisper-small.en-ov"
STATEFUL_MODEL_DIR = MODEL_ROOT / "whisper-small.en-ov-stateful"


# The multilingual sibling. Opt-in (`--language`), because the English-only model is
# the one benchmarked and pinned; this one is the same size and architecture.
MULTILINGUAL_MODEL_ID = "openai/whisper-small"
MULTILINGUAL_MODEL_DIR = MODEL_ROOT / "whisper-small-ov"
MULTILINGUAL_STATEFUL_MODEL_DIR = MODEL_ROOT / "whisper-small-ov-stateful"

AUTO_LANGUAGE = "auto"
# Checked on the CPU device 2026-10-06 with the multilingual small: transcription,
# forced language and translation to English. Whisper knows ~99, but these are the
# ones anyone has run here.
LANGUAGES = ("en", "fr", "de", "es")
TASKS = ("transcribe", "translate")
DEFAULT_LANGUAGE = "en"
DEFAULT_TASK = "transcribe"


def check_language(language: str, task: str) -> None:
    if language != AUTO_LANGUAGE and language not in LANGUAGES:
        raise ValueError(
            f"language must be one of {', '.join((AUTO_LANGUAGE, *LANGUAGES))}, got {language!r}"
        )
    if task not in TASKS:
        raise ValueError(f"task must be one of {', '.join(TASKS)}, got {task!r}")
    if task == "translate" and language == "en":
        # Whisper translates into English, so there is nothing to do for English audio.
        raise ValueError("translate turns speech into English; give the spoken language or auto")


# Shared with vinowhisper-gui, whose tray writes it. Not baked into the systemd unit, so a
# switch needs a server restart and no edited unit. A flag on the server overrides it.
LANGUAGE_FILE = _config_home() / "vinowhisper/language.json"


def load_language(path: Path | None = None) -> tuple[str, str]:
    import json

    try:
        raw = json.loads((path or LANGUAGE_FILE).read_text(encoding="utf-8"))
        language = str(raw.get("language", DEFAULT_LANGUAGE))
        task = str(raw.get("task", DEFAULT_TASK))
        check_language(language, task)
    except (OSError, ValueError, AttributeError):
        # A missing, hand-broken or contradictory file is English, never a dead server.
        return DEFAULT_LANGUAGE, DEFAULT_TASK
    return language, task


def save_language(language: str, task: str, path: Path | None = None) -> None:
    import json

    check_language(language, task)
    target = path or LANGUAGE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"language": language, "task": task}, indent=2) + "\n")


def resolve_language(language: str | None, task: str | None) -> tuple[str, str]:
    """A flag beats the file; a flag on one half replaces the file's whole choice."""
    saved_language, saved_task = load_language()
    if language is None and task is None:
        return saved_language, saved_task
    language = language or saved_language
    # `--language en` must not be undone by a saved translate, which English cannot do.
    return language, task or (saved_task if language == saved_language else DEFAULT_TASK)


def is_multilingual(language: str, task: str = DEFAULT_TASK) -> bool:
    # English alone keeps the English-only model: smaller vocabulary, better English.
    return language != "en" or task == "translate"


def model_id(multilingual: bool = False) -> str:
    return MULTILINGUAL_MODEL_ID if multilingual else MODEL_ID


def model_dir(device_kind: str, multilingual: bool = False) -> Path:
    npu = device_kind.upper() == "NPU"
    if multilingual:
        return MULTILINGUAL_MODEL_DIR if npu else MULTILINGUAL_STATEFUL_MODEL_DIR
    return MODEL_DIR if npu else STATEFUL_MODEL_DIR


DEFAULT_DEVICE = "auto"

# Beside the overlay's gui.json; see failures.py.
FAILED_DEVICES_FILE = _config_home() / "vinowhisper/failed-devices.json"

# Relative to $XDG_RUNTIME_DIR, which the socket unit spells %t.
SERVER_SOCKET_RELPATH = "vinowhisper/server.sock"


def server_socket() -> Path | None:
    # None, never a /tmp fallback: anywhere shared would let another user squat the path.
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    if not os.path.isabs(runtime_dir):
        return None
    return Path(runtime_dir) / SERVER_SOCKET_RELPATH


def server_address() -> str:
    path = server_socket()
    if path is None:
        return f"$XDG_RUNTIME_DIR/{SERVER_SOCKET_RELPATH} (XDG_RUNTIME_DIR is not set)"
    return str(path)


SAMPLE_RATE_HZ = 16_000

DEFAULT_SOURCE = "output"

# Hard ceiling: WhisperPipeline's streamer only handles audio under 30s.
MAX_WINDOW_S = 29.5

# Dictation cuts a long utterance at pauses and decodes the pieces while the key is held.
DICTATION_MAX_S = 300.0
SEGMENT_MIN_S = 5.0
SEGMENT_PAUSE_S = 0.3
SEGMENT_FALLBACK_S = 3.0

WINDOW_S = 12.0

MIN_WINDOW_S = 1.5

STALL_AFTER_S = 4.0

STALL_WINDOW_S = 6.0

MIN_HOP_S = 0.5

MAX_TOKENS_PER_S = 12.0
MAX_TOKENS_MARGIN = 16
# French, German and Spanish take more tokens per second than English. Measured only on
# synthetic speech (3.5-4.9 tok/s, 2026-10-06), so this is a margin, not a measurement.
MULTILINGUAL_TOKEN_FACTOR = 1.25


# Whisper's decoder context; the 29.5s multilingual cap would otherwise ask for more.
WHISPER_MAX_TOKENS = 448


def max_new_tokens(duration_s: float, multilingual: bool = False) -> int:
    rate = MAX_TOKENS_PER_S * (MULTILINGUAL_TOKEN_FACTOR if multilingual else 1.0)
    return min(int(duration_s * rate) + MAX_TOKENS_MARGIN, WHISPER_MAX_TOKENS)


SILENCE_RMS_THRESHOLD = 0.002

HANDS_FREE_MIN_SPEECH_S = 0.5
HANDS_FREE_SILENCE_S = 2.5

TARGET_RMS = 0.05
MAX_GAIN = 20.0

CONNECT_TIMEOUT_S = 5.0
MODEL_LOAD_TIMEOUT_S = 180.0
REQUEST_TIMEOUT_S = 60.0

IDLE_TIMEOUT_S = 5 * 60
IDLE_CHECK_INTERVAL_S = 30.0


CONVERT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "convert_model.sh"


def export_command(variant: str, multilingual: bool = False) -> str:
    if CONVERT_SCRIPT.is_file():
        extra = f" --model {MULTILINGUAL_MODEL_ID}" if multilingual else ""
        return f"./scripts/convert_model.sh --variant {variant}{extra}"
    return "vinowhisper-setup --language auto" if multilingual else "vinowhisper-setup"
