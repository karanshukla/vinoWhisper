import os
from pathlib import Path

from . import sandbox


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")


MODEL_ID = "openai/whisper-small.en"
MODEL_ROOT = _data_home() / "vinowhisper/models"
TRANSCRIPT_DIR = _data_home() / "vinowhisper/transcripts"
SUMMARIES_FILE = _config_home() / "vinowhisper/summaries.json"
SESSIONS_STATE_FILE = _state_home() / "vinowhisper/sessions.json"
ECHO_STATE_DIR = _state_home() / "vinowhisper/echo"
SUMMARIES_READY_RELPATH = "vinowhisper/summaries-ready.json"
SUMMARISING_RELPATH = "vinowhisper/summarising.json"

MODEL_DIR = MODEL_ROOT / "whisper-small.en-ov"
STATEFUL_MODEL_DIR = MODEL_ROOT / "whisper-small.en-ov-stateful"


MULTILINGUAL_MODEL_ID = "openai/whisper-small"
MULTILINGUAL_MODEL_DIR = MODEL_ROOT / "whisper-small-ov"
MULTILINGUAL_STATEFUL_MODEL_DIR = MODEL_ROOT / "whisper-small-ov-stateful"

# fp16: INT8 drifts (docs/models.md).
STEP_MODEL_DIR = MODEL_ROOT / "whisper-small.en-step-ov-fp16"
MULTILINGUAL_STEP_MODEL_DIR = MODEL_ROOT / "whisper-small-step-ov-fp16"


def step_model_dir(multilingual: bool = False) -> Path:
    return MULTILINGUAL_STEP_MODEL_DIR if multilingual else STEP_MODEL_DIR


AUTO_LANGUAGE = "auto"
# Whisper knows ~99; only these have been run (docs/models.md).
LANGUAGES = ("en", "fr", "de", "es")
TASKS = ("transcribe", "translate")
DEFAULT_LANGUAGE = "en"
DEFAULT_TASK = "transcribe"

WHISPER_MODEL = "whisper-small"
CANARY_MODEL = "canary-1b-v2"
MODELS = (WHISPER_MODEL, CANARY_MODEL)
DEFAULT_MODEL = WHISPER_MODEL
CANARY_MODEL_ID = "nvidia/canary-1b-v2"
# INT8 weights for the NPU; the CPU plugin cannot compile those (2026-11-06), so fp16 there.
CANARY_MODEL_DIR = MODEL_ROOT / "canary-1b-v2-ov"
CANARY_FP16_MODEL_DIR = MODEL_ROOT / "canary-1b-v2-ov-fp16"
CANARY_LANGUAGES = (
    "en", "bg", "hr", "cs", "da", "nl", "et", "fi", "fr", "de", "el", "hu", "it",
    "lv", "lt", "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "ru", "uk",
)  # fmt: skip

# (pinned by `test_canary.py::test_auto_translate_names_a_fixed_non_english_source`)
CANARY_AUTO_SOURCE = "fr"


def languages(model: str = DEFAULT_MODEL) -> tuple[str, ...]:
    if model == CANARY_MODEL:
        return (AUTO_LANGUAGE, *CANARY_LANGUAGES)
    return (AUTO_LANGUAGE, *LANGUAGES)


def check_language(language: str, task: str, model: str = DEFAULT_MODEL) -> None:
    if model not in MODELS:
        raise ValueError(f"model must be one of {', '.join(MODELS)}, got {model!r}")
    allowed = languages(model)
    if language not in allowed:
        raise ValueError(f"{model} language must be one of {', '.join(allowed)}, got {language!r}")
    if task not in TASKS:
        raise ValueError(f"task must be one of {', '.join(TASKS)}, got {task!r}")
    if model == CANARY_MODEL and language == AUTO_LANGUAGE and task != "translate":
        raise ValueError(
            f"{model} cannot detect a language to transcribe it; with auto it translates "
            "into English (--task translate), or name the spoken language"
        )
    if task == "translate" and language == "en":
        raise ValueError("translate turns speech into English; give the spoken language or auto")


# Read once when the server starts, so the tray stops the server to switch it.
LANGUAGE_FILE = _config_home() / "vinowhisper/language.json"

REPLACEMENTS_FILE = _config_home() / "vinowhisper/replacements.json"

VOCABULARY_FILE = _config_home() / "vinowhisper/vocabulary.txt"
VOCABULARY_MAX_TOKENS = 120

CLEANUP_FILE = _config_home() / "vinowhisper/cleanup.json"
CLEANUP_MODEL_ID = "arankays/SmolLM3-3B-int4-cw-ov"
CLEANUP_MODEL_DIR = MODEL_ROOT / "SmolLM3-3B-int4-cw-ov"
CLEANUP_CACHE_DIR = _cache_home() / "vinowhisper/cleanup-cache"


def load_language(path: Path | None = None) -> tuple[str, str, str]:
    import json

    try:
        raw = json.loads((path or LANGUAGE_FILE).read_text(encoding="utf-8"))
        model = str(raw.get("model", DEFAULT_MODEL))
    except (OSError, ValueError, AttributeError):
        # (pinned by `test_languages.py::test_a_missing_or_bad_language_file_means_english_not_a_dead_server`)
        return DEFAULT_LANGUAGE, DEFAULT_TASK, DEFAULT_MODEL
    # (pinned by `test_canary.py::test_a_bad_choice_falls_back_without_losing_the_model`)
    model = model if model in MODELS else DEFAULT_MODEL
    try:
        language = str(raw.get("language", DEFAULT_LANGUAGE))
        task = str(raw.get("task", DEFAULT_TASK))
        check_language(language, task, model)
    except ValueError:
        return DEFAULT_LANGUAGE, DEFAULT_TASK, model
    return language, task, model


def save_language(
    language: str, task: str, path: Path | None = None, *, model: str = DEFAULT_MODEL
) -> None:
    from . import atomic

    check_language(language, task, model)
    # (pinned by `test_languages.py::test_saving_the_language_through_a_symlink_keeps_the_link`)
    target = (path or LANGUAGE_FILE).resolve()
    body = {"language": language, "task": task, "model": model}
    atomic.write_json(target, body, temp_suffix=f".{os.getpid()}.partial")


def resolve_language(
    language: str | None, task: str | None, model: str | None = None
) -> tuple[str, str, str]:
    # (pinned by `test_languages.py::test_flags_beat_the_file`)
    saved_language, saved_task, saved_model = load_language()
    model = model or saved_model
    if language is None and task is None:
        if saved_language not in languages(model):
            # (pinned by `test_canary.py::test_a_model_flag_drops_a_saved_language_it_lacks`)
            return DEFAULT_LANGUAGE, DEFAULT_TASK, model
        return saved_language, saved_task, model
    language = language or saved_language
    return language, task or (saved_task if language == saved_language else DEFAULT_TASK), model


def is_multilingual(language: str, task: str = DEFAULT_TASK) -> bool:
    # (pinned by `test_languages.py::test_english_keeps_the_english_only_model`)
    return language != "en" or task == "translate"


def model_id(multilingual: bool = False) -> str:
    return MULTILINGUAL_MODEL_ID if multilingual else MODEL_ID


def served_model_id(model: str, language: str, task: str) -> str:
    if model == CANARY_MODEL:
        return CANARY_MODEL_ID
    return model_id(is_multilingual(language or DEFAULT_LANGUAGE, task or DEFAULT_TASK))


def model_dir(device_kind: str, multilingual: bool = False, model: str = DEFAULT_MODEL) -> Path:
    npu = device_kind.upper() == "NPU"
    if model == CANARY_MODEL:
        return CANARY_MODEL_DIR if npu else CANARY_FP16_MODEL_DIR
    if multilingual:
        return MULTILINGUAL_MODEL_DIR if npu else MULTILINGUAL_STATEFUL_MODEL_DIR
    return MODEL_DIR if npu else STATEFUL_MODEL_DIR


AUTO_DEVICE = "auto"
DEFAULT_DEVICE = AUTO_DEVICE

FAILED_DEVICES_FILE = _config_home() / "vinowhisper/failed-devices.json"

SERVER_FILE = _config_home() / "vinowhisper/server.json"
SERVER_LOG = _state_home() / "vinowhisper/server.log"

SERVER_SOCKET_RELPATH = "vinowhisper/server.sock"


def load_device(path: Path | None = None) -> str:
    import json

    try:
        device = json.loads((path or SERVER_FILE).read_text(encoding="utf-8")).get("device")
    except (OSError, ValueError, AttributeError):
        return DEFAULT_DEVICE
    return device if isinstance(device, str) and device else DEFAULT_DEVICE


def save_device(device: str, path: Path | None = None) -> None:
    import json

    path = path or SERVER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"device": device}) + "\n", encoding="utf-8")


def runtime_dir() -> Path | None:
    # No /tmp fallback: another user could squat a shared path
    # (pinned by `test_client.py::test_no_runtime_dir_is_a_connection_error_not_a_tmp_fallback`).
    raw = os.environ.get("XDG_RUNTIME_DIR", "")
    if not os.path.isabs(raw):
        return None
    app = sandbox.flatpak_id()
    # (pinned by `test_flatpak.py::test_the_runtime_dir_is_the_part_every_sandbox_of_the_app_shares`)
    return Path(raw) / "app" / app if app else Path(raw)


def server_socket() -> Path | None:
    directory = runtime_dir()
    return None if directory is None else directory / SERVER_SOCKET_RELPATH


def server_address() -> str:
    path = server_socket()
    if path is None:
        return f"$XDG_RUNTIME_DIR/{SERVER_SOCKET_RELPATH} (XDG_RUNTIME_DIR is not set)"
    return str(path)


SAMPLE_RATE_HZ = 16_000

DEFAULT_SOURCE = "output"

# Hard ceiling: WhisperPipeline's streamer only handles audio under 30s.
MAX_WINDOW_S = 29.5

DICTATION_MAX_S = 300.0
SEGMENT_MIN_S = 5.0
SEGMENT_PAUSE_S = 0.3
SEGMENT_FALLBACK_S = 3.0
PREVIEW_EVERY_S = 1.5
PREVIEW_MIN_S = 1.0
PREVIEW_QUIET_S = 0.3

# Quiet kept around a piece's speech; Whisper writes sentences for noise.
TRIM_PAD_S = 0.3

# A dictation piece's room-noise and speech thresholds: docs/tray.md.
STATIONARY_RATIO = 2.8
STATIONARY_MAX_RMS = 0.04
SPEECH_FRACTION = 0.6
SPEECH_MIN_CHUNKS = 2

WINDOW_S = 12.0

FILE_PIECE_MIN_S = 15.0
# How long a file's piece waits for a caption or dictation decode already running.
BACKGROUND_YIELD_S = 5.0
# While captions or dictation are in use, no file piece costs them more than one window's decode.
FILE_PIECE_BUSY_MIN_S = 6.0
FILE_PIECE_BUSY_MAX_S = WINDOW_S
LIVE_RECENT_S = 10.0

MIN_WINDOW_S = 1.5

STALL_AFTER_S = 4.0

STALL_WINDOW_S = 6.0

MIN_HOP_S = 0.5

MAX_TOKENS_PER_S = 12.0
MAX_TOKENS_MARGIN = 16
# A margin, not a measurement: docs/latency.md, "Repetition loops are capped".
MULTILINGUAL_TOKEN_FACTOR = 1.25


# Whisper's decoder context, which the multilingual cap on a 29.5s window would exceed.
WHISPER_MAX_TOKENS = 448


def max_new_tokens(duration_s: float, multilingual: bool = False) -> int:
    rate = MAX_TOKENS_PER_S * (MULTILINGUAL_TOKEN_FACTOR if multilingual else 1.0)
    return min(int(duration_s * rate) + MAX_TOKENS_MARGIN, WHISPER_MAX_TOKENS)


SILENCE_RMS_THRESHOLD = 0.002
CAPTION_MIN_SPEECH_S = 0.3

NOISE_FLOOR_MARGIN = 2.0
NOISE_FLOOR_MAX_RMS = 0.05
# Mics fade in as capture starts (Intel DMIC: 100-200ms); the opening reads quieter than the room.
NOISE_FLOOR_SETTLE_S = 0.5

HANDS_FREE_MIN_SPEECH_S = 0.5
HANDS_FREE_SILENCE_S = 2.5

TARGET_RMS = 0.05
MAX_GAIN = 20.0

CONNECT_TIMEOUT_S = 5.0
MODEL_LOAD_TIMEOUT_S = 180.0
REQUEST_TIMEOUT_S = 60.0

IDLE_TIMEOUT_S = 5 * 60
# Captions keep the server up for hours; the cleanup model should not ride along.
CLEANUP_IDLE_UNLOAD_S = 5 * 60
# (pinned by `test_server.py::test_no_request_can_hold_the_model_past_the_servers_ceiling`)
CLEANUP_MAX_BUDGET_S = 10.0
IDLE_CHECK_INTERVAL_S = 30.0


CANARY_EXPORT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts/export_canary.py"
CANARY_EXPORT_URL = (
    "https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/scripts/export_canary.py"
)
# A venv for optimum and torch in the Flatpak, whose own Python is read-only.
EXPORT_ENV_DIR = _data_home() / "vinowhisper/export-env"
CONVERT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "convert_model.sh"
# optimum writes a full-size intermediate to $TMPDIR, and /tmp is often tmpfs (RAM).
EXPORT_TMP_ROOT = _cache_home() / "vinowhisper/export-tmp"


def export_command(variant: str, multilingual: bool = False, model: str = DEFAULT_MODEL) -> str:
    if model == CANARY_MODEL:
        # A PEP 723 script with its own transformers, clear of the [export] extra's <5.4 pin.
        if CANARY_EXPORT_SCRIPT.is_file():
            return "uv run scripts/export_canary.py"
        return f"uv run {CANARY_EXPORT_URL}"
    if CONVERT_SCRIPT.is_file():
        extra = f" --model {MULTILINGUAL_MODEL_ID}" if multilingual else ""
        return f"./scripts/convert_model.sh --variant {variant}{extra}"
    return "vinowhisper-setup --language auto" if multilingual else "vinowhisper-setup"
