import json
import subprocess
import threading
import time
from collections.abc import Callable

from . import capture

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"

SAMPLE_EVERY_S = 30.0
MAX_ENTRIES = 12
MAX_FIELD_CHARS = 200
_CALL_TIMEOUT_S = 2.0
# Only these leave the player: never xesam:url, mpris:artUrl or the track id.
KEYS = ("app", "title", "artist")

Entry = dict[str, str]


def _busctl(*args: str) -> object:
    if capture._which("busctl") is None:
        return None
    argv = ["busctl", "--user", "--json=short", "--timeout=1", *args]
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=_CALL_TIMEOUT_S, check=True
        )
        return json.loads(done.stdout).get("data")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


def _clean(value: object) -> str:
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value if item)
    if not isinstance(value, str):
        return ""
    text = " ".join("".join(c for c in value if c.isprintable()).split())
    return text[:MAX_FIELD_CHARS].rstrip()


def _variant(properties: object, key: str) -> object:
    if not isinstance(properties, dict):
        return None
    held = properties.get(key)
    return held.get("data") if isinstance(held, dict) else None


def _entry(app: object, title: object = "", artist: object = "") -> Entry:
    found = {"app": _clean(app), "title": _clean(title), "artist": _clean(artist)}
    return {key: value for key, value in found.items() if value}


def mpris_playing() -> list[Entry]:
    names = _busctl(
        "call", "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames"
    )
    if not isinstance(names, list) or not names or not isinstance(names[0], list):
        return []
    entries = []
    for name in names[0]:
        if not isinstance(name, str) or not name.startswith(MPRIS_PREFIX):
            continue
        got = _busctl(
            "call", name, MPRIS_PATH, "org.freedesktop.DBus.Properties", "GetAll", "s", PLAYER
        )
        properties = got[0] if isinstance(got, list) and got else None
        if _variant(properties, "PlaybackStatus") != "Playing":
            continue
        metadata = _variant(properties, "Metadata")
        identity = _busctl("get-property", name, MPRIS_PATH, "org.mpris.MediaPlayer2", "Identity")
        app = identity if isinstance(identity, str) and identity else name[len(MPRIS_PREFIX) :]
        entry = _entry(app, _variant(metadata, "xesam:title"), _variant(metadata, "xesam:artist"))
        if entry.get("app"):
            entries.append(entry)
    return entries


def stream_apps(target: str | None = None) -> list[str]:
    try:
        if not capture.backend().supports_app_capture:
            return []
    except capture.CaptureError:
        return []
    apps = []
    for obj in capture._pw_dump():
        info = obj.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        if target is not None:
            if str(props.get("object.serial")) != target:
                continue
        elif info.get("state") != "running":
            continue
        app = _clean(props.get("application.name") or "")
        if app and app not in apps:
            apps.append(app)
    return apps


def same_app(one: str, other: str) -> bool:
    a, b = one.casefold(), other.casefold()
    return bool(a and b) and (a in b or b in a)


def now_playing(target: str | None = None) -> list[Entry]:
    players = mpris_playing()
    apps = stream_apps(target)
    if target is not None:
        # One application is captured; a player counts only if it is that application.
        players = [p for p in players if any(same_app(p["app"], app) for app in apps)]
    silent = [app for app in apps if not any(same_app(p["app"], app) for p in players)]
    return players + [_entry(app) for app in silent]


def merge(entries: list[Entry], found: list[Entry]) -> list[Entry]:
    for entry in found:
        if entry in entries or len(entries) >= MAX_ENTRIES:
            continue
        titled = "title" in entry
        if not titled and any(same_app(e["app"], entry["app"]) for e in entries):
            continue
        if titled:
            # The app alone was a placeholder for this.
            entries[:] = [
                e for e in entries if "title" in e or not same_app(e["app"], entry["app"])
            ]
        entries.append(entry)
    return entries


def valid(raw: object) -> list[Entry]:
    if not isinstance(raw, list):
        return []
    entries = []
    for item in raw[:MAX_ENTRIES]:
        if isinstance(item, dict):
            entry = _entry(*(item.get(key) or "" for key in KEYS))
            if entry.get("app"):
                entries.append(entry)
    return entries


def describe(entry: Entry) -> str:
    title = entry.get("title")
    if not title:
        return entry["app"]
    artist = entry.get("artist")
    by = f" by {artist}" if artist else ""
    return f"{title}{by} ({entry['app']})"


def label(entries: list[Entry], limit: int = 3) -> str:
    shown = "; ".join(describe(entry) for entry in entries[:limit])
    more = len(entries) - limit
    return f"{shown}; and {more} more" if more > 0 else shown


class Tracker:
    def __init__(
        self,
        target: str | None = None,
        sample: Callable[[str | None], list[Entry]] = now_playing,
        clock: Callable[[], float] = time.monotonic,
        every_s: float = SAMPLE_EVERY_S,
        threaded: bool = True,
    ) -> None:
        self._target = target
        self._sample = sample
        self._clock = clock
        self._every_s = every_s
        self._threaded = threaded
        self._lock = threading.Lock()
        self._entries: list[Entry] = []
        self._last: float | None = None
        self._thread: threading.Thread | None = None

    def poke(self) -> None:
        now = self._clock()
        if self._last is not None and now - self._last < self._every_s:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._last = now
        if not self._threaded:
            self._run()
            return
        # Off the caption loop: a player slow to answer must not hold a cycle up.
        self._thread = threading.Thread(target=self._run, name="playing", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            found = self._sample(self._target)
        except Exception:
            return
        with self._lock:
            merge(self._entries, found)

    def entries(self, wait_s: float = _CALL_TIMEOUT_S) -> list[Entry]:
        if self._thread is not None:
            self._thread.join(wait_s)
        with self._lock:
            return [dict(entry) for entry in self._entries]
