import os
from pathlib import Path


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
# {"auto": bool}, written by the settings window's Sessions page (settings::Summaries).
SUMMARIES_FILE = _config_home() / "vinowhisper/summaries.json"
# The summariser's own bookkeeping: failures, declined summaries, and where backfill starts.
SESSIONS_STATE_FILE = _state_home() / "vinowhisper/sessions.json"
# Finished summaries the tray has not announced yet; relative to $XDG_RUNTIME_DIR.
SUMMARIES_READY_RELPATH = "vinowhisper/summaries-ready.json"
# What the summariser is on right now, for the Sessions page; gone when it is idle.
SUMMARISING_RELPATH = "vinowhisper/summarising.json"

MODEL_DIR = MODEL_ROOT / "whisper-small.en-ov"
STATEFUL_MODEL_DIR = MODEL_ROOT / "whisper-small.en-ov-stateful"


# The multilingual sibling. Opt-in (`--language`), because the English-only model is
# the one benchmarked and pinned; this one is the same size and architecture.
MULTILINGUAL_MODEL_ID = "openai/whisper-small"
MULTILINGUAL_MODEL_DIR = MODEL_ROOT / "whisper-small-ov"
MULTILINGUAL_STATEFUL_MODEL_DIR = MODEL_ROOT / "whisper-small-ov-stateful"

# scripts/export_whisper.py: our own encoder + one-token decoder, for a decoder prompt on the
# NPU (#176). fp16: it matched WhisperPipeline word for word on 19 min, INT8 did not.
STEP_MODEL_DIR = MODEL_ROOT / "whisper-small.en-step-ov-fp16"
MULTILINGUAL_STEP_MODEL_DIR = MODEL_ROOT / "whisper-small-step-ov-fp16"


def step_model_dir(multilingual: bool = False) -> Path:
    return MULTILINGUAL_STEP_MODEL_DIR if multilingual else STEP_MODEL_DIR


AUTO_LANGUAGE = "auto"
# Checked on the CPU device 2026-10-06 with the multilingual small: transcription,
# forced language and translation to English. Whisper knows ~99, but these are the
# ones anyone has run here.
LANGUAGES = ("en", "fr", "de", "es")
TASKS = ("transcribe", "translate")
DEFAULT_LANGUAGE = "en"
DEFAULT_TASK = "transcribe"

# The speech model, picked in the settings window (#143). Whisper stays the default: on a
# stitched session it beat Canary on English (4.8% WER against 5.8%, 2026-11-06).
WHISPER_MODEL = "whisper-small"
CANARY_MODEL = "canary-1b-v2"
MODELS = (WHISPER_MODEL, CANARY_MODEL)
DEFAULT_MODEL = WHISPER_MODEL
CANARY_MODEL_ID = "nvidia/canary-1b-v2"
# INT8 weights for the NPU; the CPU plugin cannot compile those (2026-11-06), so fp16 there.
CANARY_MODEL_DIR = MODEL_ROOT / "canary-1b-v2-ov"
CANARY_FP16_MODEL_DIR = MODEL_ROOT / "canary-1b-v2-ov-fp16"
# Canary's own list. Only en, fr, de and es have been run here (2026-11-06). Auto means
# translate whatever is spoken: Canary has no language detection, but into English it
# translates the same with the wrong source named (fr audio named de, de named fr, and en
# named fr all came out right), so the prompt names CANARY_AUTO_SOURCE. Named English
# instead, French audio leaked French words.
CANARY_LANGUAGES = (
    "en", "bg", "hr", "cs", "da", "nl", "et", "fi", "fr", "de", "el", "hu", "it",
    "lv", "lt", "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "ru", "uk",
)  # fmt: skip


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
        # Whisper translates into English, so there is nothing to do for English audio.
        raise ValueError("translate turns speech into English; give the spoken language or auto")


# Shared with vinowhisper-tray, whose tray writes it. Not baked into the systemd unit, so a
# switch needs a server restart and no edited unit. A flag on the server overrides it.
LANGUAGE_FILE = _config_home() / "vinowhisper/language.json"

REPLACEMENTS_FILE = _config_home() / "vinowhisper/replacements.json"

# One term per line, given to Whisper's decoder as a prompt for dictation (issue #161).
VOCABULARY_FILE = _config_home() / "vinowhisper/vocabulary.txt"
VOCABULARY_MAX_TOKENS = 120

# Written by the settings window; the API key is never in it (see cleanup.SECRET_ATTRIBUTES).
CLEANUP_FILE = _config_home() / "vinowhisper/cleanup.json"
CLEANUP_MODEL_ID = "arankays/SmolLM3-3B-int4-cw-ov"
CLEANUP_MODEL_DIR = MODEL_ROOT / "SmolLM3-3B-int4-cw-ov"
# Compiled NPU blobs: a first load compiles for ~25s, a cached one takes a few.
CLEANUP_CACHE_DIR = _cache_home() / "vinowhisper/cleanup-cache"


def load_language(path: Path | None = None) -> tuple[str, str, str]:
    import json

    try:
        raw = json.loads((path or LANGUAGE_FILE).read_text(encoding="utf-8"))
        model = str(raw.get("model", DEFAULT_MODEL))
    except (OSError, ValueError, AttributeError):
        # A missing, hand-broken or contradictory file is English, never a dead server.
        return DEFAULT_LANGUAGE, DEFAULT_TASK, DEFAULT_MODEL
    # An unknown model (a newer version's) is Whisper; the language survives if Whisper has it.
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
    import json

    check_language(language, task, model)
    # resolve(): through a symlink, so a dotfiles-managed link stays a link.
    target = (path or LANGUAGE_FILE).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Renamed over the old file: the server reads a torn one as English, silently.
    partial = target.with_name(f".{target.name}.{os.getpid()}.partial")
    try:
        body = {"language": language, "task": task, "model": model}
        partial.write_text(json.dumps(body, indent=2) + "\n")
        os.replace(partial, target)
    except OSError:
        partial.unlink(missing_ok=True)
        raise


def resolve_language(
    language: str | None, task: str | None, model: str | None = None
) -> tuple[str, str, str]:
    """A flag beats the file; a flag on one half replaces the file's whole choice."""
    saved_language, saved_task, saved_model = load_language()
    model = model or saved_model
    if language is None and task is None:
        if saved_language not in languages(model):
            # `--model` onto a saved language the other model lacks: its default instead.
            return DEFAULT_LANGUAGE, DEFAULT_TASK, model
        return saved_language, saved_task, model
    language = language or saved_language
    # `--language en` must not be undone by a saved translate, which English cannot do.
    return language, task or (saved_task if language == saved_language else DEFAULT_TASK), model


def is_multilingual(language: str, task: str = DEFAULT_TASK) -> bool:
    # English alone keeps the English-only model: smaller vocabulary, better English.
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
PREVIEW_EVERY_S = 1.5
PREVIEW_MIN_S = 1.0
PREVIEW_QUIET_S = 0.3

# Quiet kept around the speech in each piece. Whisper writes sentences for room noise, so
# the long quiet either side of the words, which hands-free adds up to 2.5s of, is cut.
TRIM_PAD_S = 0.3

# A recording whose loudest stretch is no more than STATIONARY_RATIO times its quietest, and
# under STATIONARY_MAX_RMS, is room noise: Whisper writes sentences for it (measured
# 2026-10-11: ambient 0.011-0.028 rms, three decodes, three different sentences, on both
# models). Otherwise a piece needs SPEECH_MIN_CHUNKS of 100ms at SPEECH_FRACTION of the
# recording's loudest level; quiet after a cut is not a second utterance.
STATIONARY_RATIO = 2.8
STATIONARY_MAX_RMS = 0.04
SPEECH_FRACTION = 0.6
SPEECH_MIN_CHUNKS = 2

# vinowhisper-transcribe cuts a file at the quietest pause between these two lengths.
FILE_PIECE_MIN_S = 15.0
# How long a file's piece waits for captions or a dictation already decoding.
BACKGROUND_YIELD_S = 5.0
# While captions or dictation are in use, a file is cut shorter: no piece then costs
# them more than one caption window's decode.
FILE_PIECE_BUSY_MIN_S = 6.0
FILE_PIECE_BUSY_MAX_S = 12.0
# Captions or a dictation within this long count as in use.
LIVE_RECENT_S = 10.0

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
# The server's own deadline when a request names none or a longer one: no request holds
# the model, and every dictation behind it, for longer.
CLEANUP_MAX_BUDGET_S = 10.0
IDLE_CHECK_INTERVAL_S = 30.0


CANARY_EXPORT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts/export_canary.py"
CANARY_EXPORT_URL = (
    "https://raw.githubusercontent.com/karanshukla/vinoWhisper/main/scripts/export_canary.py"
)
CONVERT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "convert_model.sh"
# optimum writes a full-size intermediate to $TMPDIR, and /tmp is often tmpfs (RAM).
EXPORT_TMP_ROOT = _cache_home() / "vinowhisper/export-tmp"


def export_command(variant: str, multilingual: bool = False, model: str = DEFAULT_MODEL) -> str:
    if model == CANARY_MODEL:
        # PEP 723 script with its own transformers, clear of the [export] extra's <5.4 pin.
        if CANARY_EXPORT_SCRIPT.is_file():
            return "uv run scripts/export_canary.py"
        return f"uv run {CANARY_EXPORT_URL}"
    if CONVERT_SCRIPT.is_file():
        extra = f" --model {MULTILINGUAL_MODEL_ID}" if multilingual else ""
        return f"./scripts/convert_model.sh --variant {variant}{extra}"
    return "vinowhisper-setup --language auto" if multilingual else "vinowhisper-setup"
