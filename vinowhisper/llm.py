import contextlib
import json
import re
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import config, devices, ovfetch, source
from .integrity import sha256_file

PINS_PATH = Path(__file__).resolve().parent / "cleanup_model.json"
REVISION_MARKER = ".vinowhisper-revision"
# Size, mtime and inode of each file when it last hashed clean: a load re-hashes only
# when one of them moved. Anything running as the user can rewrite this too; it catches
# corruption and careless edits, not a determined same-user attacker.
VERIFIED_STAMP = ".vinowhisper-verified"

# The NPU compiles for a fixed window. 3072 holds the cleanup prompt and five minutes of
# dictation; 1024 holds the edited copy of that dictation coming back.
MAX_PROMPT_LEN = 3072
MIN_RESPONSE_LEN = 1024

_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


class ModelMissing(RuntimeError):
    pass


def pinned(
    model_id: str = config.CLEANUP_MODEL_ID, path: Path | None = None
) -> source.Source | None:
    try:
        raw = json.loads((path or PINS_PATH).read_text(encoding="utf-8"))
        entry = raw["models"][model_id]
        files = entry["files"]
        return source.Source(
            model_id=model_id,
            revision=str(entry["revision"]),
            recorded=str(entry.get("recorded", "")),
            sha256={name: str(meta["sha256"]) for name, meta in files.items()},
            sizes={name: int(meta["size"]) for name, meta in files.items()},
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _bytes_in(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def is_present(pin: source.Source, directory: Path | None = None) -> bool:
    directory = directory or config.CLEANUP_MODEL_DIR
    # Cheap: the marker is written only after every file matched its sha256.
    try:
        marked = (directory / REVISION_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if marked != pin.revision:
        return False
    return all(
        (directory / name).is_file() and (directory / name).stat().st_size == size
        for name, size in pin.sizes.items()
    )


def _fingerprint(pin: source.Source, directory: Path) -> str:
    lines = []
    for name in sorted(pin.sha256):
        stat = (directory / name).stat()
        lines.append(f"{name} {stat.st_size} {stat.st_mtime_ns} {stat.st_ino}")
    return "\n".join(lines) + "\n"


def verify(pin: source.Source, directory: Path | None = None) -> list[str]:
    directory = directory or config.CLEANUP_MODEL_DIR
    stamp = directory / VERIFIED_STAMP
    try:
        if stamp.read_text(encoding="utf-8") == _fingerprint(pin, directory):
            return []
    except OSError:
        pass
    bad = [
        name
        for name, expected in sorted(pin.sha256.items())
        if not (directory / name).is_file() or sha256_file(directory / name) != expected
    ]
    if bad:
        # Not downloaded any more, as far as the settings window and dictation can tell.
        (directory / REVISION_MARKER).unlink(missing_ok=True)
        stamp.unlink(missing_ok=True)
    else:
        stamp.write_text(_fingerprint(pin, directory), encoding="utf-8")
    return bad


def status(
    pin: source.Source | None = None,
    directory: Path | None = None,
    cache: Path | None = None,
) -> dict:
    directory = directory or config.CLEANUP_MODEL_DIR
    cache = cache or config.CLEANUP_CACHE_DIR
    pin = pin or pinned()
    if pin is None:
        raise ModelMissing(f"{PINS_PATH} is missing or unreadable; reinstall vinowhisper")
    present = is_present(pin, directory)
    return {
        "label": "SmolLM3-3B",
        "repo": pin.model_id,
        "revision": pin.revision,
        "download_bytes": sum(pin.sizes.values()),
        "present": present,
        "bytes": _bytes_in(directory) if present else 0,
        "cache_bytes": _bytes_in(cache),
        "dir": str(directory),
    }


def fetch(
    progress: Callable[[int, int], None] = lambda done, total: None,
    pin: source.Source | None = None,
    directory: Path | None = None,
    session=None,
) -> Path:
    directory = directory or config.CLEANUP_MODEL_DIR
    pin = pin or pinned()
    if pin is None:
        raise ModelMissing(f"{PINS_PATH} is missing or unreadable; reinstall vinowhisper")
    total = sum(pin.sizes.values())
    marker = directory / REVISION_MARKER
    marker.unlink(missing_ok=True)
    # source.fetch refuses a directory holding files the pin does not name.
    (directory / VERIFIED_STAMP).unlink(missing_ok=True)
    source.fetch(
        pin,
        directory,
        session=session,
        say=lambda line: None,
        progress=lambda done: progress(done, total),
    )
    marker.write_text(pin.revision + "\n", encoding="utf-8")
    (directory / VERIFIED_STAMP).write_text(_fingerprint(pin, directory), encoding="utf-8")
    return directory


def delete(directory: Path | None = None, cache: Path | None = None) -> None:
    shutil.rmtree(directory or config.CLEANUP_MODEL_DIR, ignore_errors=True)
    shutil.rmtree(cache or config.CLEANUP_CACHE_DIR, ignore_errors=True)


def _candidates(requested: str) -> list[str]:
    found = devices.available()
    if requested.lower() != "auto":
        wanted = requested.upper()
        return [d.name for d in found if d.name.upper() == wanted or d.kind == wanted]
    return [d.name for kind in devices.PREFERENCE for d in found if d.kind == kind]


class CleanupModel:
    def __init__(
        self,
        device: str = config.DEFAULT_DEVICE,
        directory: Path | None = None,
        cache: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._requested = device
        self._directory = directory or config.CLEANUP_MODEL_DIR
        self._cache = cache or config.CLEANUP_CACHE_DIR
        self._clock = clock
        self._pipe: object | None = None
        self.device: str | None = None
        self.error: str | None = None
        self._lock = threading.Lock()
        self._loading = threading.Lock()
        self._last_used = clock()

    @property
    def loaded(self) -> bool:
        return self._pipe is not None

    def describe(self) -> dict:
        return {
            "loaded": self.loaded,
            "loading": self._loading.locked(),
            "device": self.device,
            "error": self.error,
        }

    def _build(self, device: str) -> object:
        import openvino_genai as ov_genai

        props: dict[str, object] = {"CACHE_DIR": str(self._cache / device)}
        if devices.kind_of(device) == "NPU":
            props.update(MAX_PROMPT_LEN=MAX_PROMPT_LEN, MIN_RESPONSE_LEN=MIN_RESPONSE_LEN)
        return ov_genai.LLMPipeline(str(self._directory), device, **props)

    def load(self) -> None:
        with self._loading:
            if self._pipe is not None:
                return
            pin = pinned()
            if pin is None or not is_present(pin, self._directory):
                raise ModelMissing("the cleanup model isn't downloaded")
            bad = verify(pin, self._directory)
            if bad:
                raise ModelMissing(
                    f"the cleanup model's {', '.join(bad)} no longer match the pinned sha256; "
                    "download it again in Settings"
                )
            # A bad ovfetch prefix only costs the NPU, and the loop below moves on to the GPU.
            with contextlib.suppress(ovfetch.OvfetchError):
                ovfetch.preload()
            failures: list[str] = []
            for device in _candidates(self._requested):
                try:
                    pipe = self._build(device)
                except Exception as exc:
                    failures.append(f"{device}: {exc}".splitlines()[0])
                    continue
                self._pipe, self.device, self.error = pipe, device, None
                self._last_used = self._clock()
                return
            self.error = "; ".join(failures) or f"no device matches {self._requested!r}"
            raise RuntimeError(f"could not load the cleanup model ({self.error})")

    def warm(self) -> None:
        if self._pipe is not None or self._loading.locked():
            return

        def run() -> None:
            try:
                self.load()
            except Exception as exc:
                self.error = str(exc)

        threading.Thread(target=run, name="cleanup-load", daemon=True).start()

    def generate(
        self,
        system: str,
        user: str,
        max_new_tokens: int,
        repetition_penalty: float = 1.0,
        budget_s: float | None = None,
    ) -> tuple[str, bool]:
        self.load()
        with self._lock:
            try:
                return self._generate(system, user, max_new_tokens, repetition_penalty, budget_s)
            finally:
                self._last_used = self._clock()

    def _generate(
        self,
        system: str,
        user: str,
        max_new_tokens: int,
        repetition_penalty: float,
        budget_s: float | None,
    ) -> tuple[str, bool]:
        import openvino_genai as ov_genai

        pipe = self._pipe
        if pipe is None:
            raise ModelMissing("the cleanup model was unloaded")
        tokenizer = pipe.get_tokenizer()  # type: ignore[attr-defined]
        # SmolLM3 reasons out loud unless told not to, and the reasoning would be typed.
        history = [
            {"role": "system", "content": system + " /no_think"},
            {"role": "user", "content": user},
        ]
        prompt = tokenizer.apply_chat_template(history, add_generation_prompt=True)
        settings = ov_genai.GenerationConfig()
        settings.max_new_tokens = max_new_tokens
        settings.do_sample = False
        settings.apply_chat_template = False
        settings.repetition_penalty = repetition_penalty
        deadline = None if budget_s is None else self._clock() + budget_s
        late = threading.Event()

        def streamer(_piece: str) -> object:
            # The caller has already typed the raw text by then; stop holding the lock.
            if deadline is not None and self._clock() > deadline:
                late.set()
                return ov_genai.StreamingStatus.CANCEL
            return ov_genai.StreamingStatus.RUNNING

        answer = str(pipe.generate(prompt, generation_config=settings, streamer=streamer))  # type: ignore[attr-defined]
        if late.is_set():
            raise TimeoutError("past the time limit")
        produced = len(tokenizer.encode(answer).input_ids.data[0])
        return _THINK.sub("", answer).strip(), produced >= max_new_tokens

    def unload_if_idle(self, idle_s: float) -> bool:
        if self._pipe is None or not self._lock.acquire(blocking=False):
            return False
        try:
            if self._pipe is None or self._clock() - self._last_used < idle_s:
                return False
            self._pipe, self.device = None, None
            return True
        finally:
            self._lock.release()
