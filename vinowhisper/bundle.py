import hashlib
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__, config, llm, source

FORMAT = "vinowhisper-session/1"
MIMETYPE = b"application/vnd.vinowhisper.session+zip"
SUFFIX = ".vwsession"
MIMETYPE_NAME = "mimetype"
TRANSCRIPT = "transcript.txt"
SUMMARY = "summary.md"
ATTESTATION = "attestation.json"
LAYOUT = (MIMETYPE_NAME, TRANSCRIPT, SUMMARY, ATTESTATION)

MADE_BY_TRANSCRIPT = "speech-to-text"
MADE_BY_SUMMARY = "ai-generated-summary"

# Fixed entry times keep the bytes a function of the content; real times are in the attestation.
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_MAX_PART_BYTES = 64 << 20
_STAMP = re.compile(r"^\[(\d{2,}):(\d{2})\] ", re.M)
_HEADER = re.compile(r"^vinoWhisper transcript, (\d{4}-\d{2}-\d{2} \d{2}:\d{2}), (.*), ([^,]*)$")
_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6})")


class BundleError(ValueError):
    pass


@dataclass
class Bundle:
    path: Path
    transcript: str
    attestation: dict
    summary: str | None = None
    extra: dict[str, bytes] = field(default_factory=dict)

    @property
    def session(self) -> dict:
        return self.attestation.get("session", {})

    @property
    def summary_title(self) -> str | None:
        if self.summary is None:
            return None
        for line in self.summary.splitlines():
            if line.startswith("# "):
                return line[2:].strip() or None
        return None


def is_bundle_name(path: Path) -> bool:
    return path.suffix == SUFFIX and not path.name.startswith(".")


def bundle_path_for(transcript: Path) -> Path:
    return transcript.with_suffix(SUFFIX)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _whisper_entry(model_id: str | None, device: str | None) -> dict:
    pin = source.load(model_id) if model_id else None
    return {
        "made_by": MADE_BY_TRANSCRIPT,
        "model": model_id,
        "revision": pin.revision if pin else None,
        "device": device,
    }


def new_attestation(
    started: datetime | None,
    duration_s: int | None,
    source_label: str | None,
    language: str | None,
    task: str | None,
    device: str | None,
    model_id: str | None,
) -> dict:
    return {
        "format": FORMAT,
        "generator": {"name": "vinoWhisper", "version": __version__},
        "session": {
            "started": started.isoformat(timespec="seconds") if started else None,
            "duration_s": duration_s,
            "source": source_label,
            "language": language or None,
            "task": task or None,
        },
        "files": {TRANSCRIPT: _whisper_entry(model_id, device)},
    }


def summary_entry(device: str | None, generated: str | None = None) -> dict:
    pin = llm.pinned()
    base = None
    if pin is not None:
        try:
            raw = json.loads(llm.PINS_PATH.read_text(encoding="utf-8"))
            base = raw["models"][pin.model_id].get("base")
        except (OSError, ValueError, KeyError):
            base = None
    return {
        "made_by": MADE_BY_SUMMARY,
        "model": base.split("@")[0] if base else config.CLEANUP_MODEL_ID,
        "export": f"{pin.model_id}@{pin.revision}" if pin else None,
        "device": device,
        "generated": generated or now(),
    }


def read(path: Path) -> Bundle:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if not names or names[0] != MIMETYPE_NAME:
                raise BundleError(f"{path.name}: the first entry is not {MIMETYPE_NAME}")
            if archive.read(MIMETYPE_NAME) != MIMETYPE:
                raise BundleError(f"{path.name}: not a vinoWhisper session")
            parts: dict[str, bytes] = {}
            for info in archive.infolist():
                if info.file_size > _MAX_PART_BYTES:
                    raise BundleError(f"{path.name}: {info.filename} is too large")
                parts[info.filename] = archive.read(info)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BundleError(f"{path.name}: {exc}") from exc
    try:
        attestation = json.loads(parts[ATTESTATION])
        transcript = parts[TRANSCRIPT].decode("utf-8")
    except KeyError as exc:
        raise BundleError(f"{path.name}: no {exc.args[0]}") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise BundleError(f"{path.name}: {exc}") from exc
    if not isinstance(attestation, dict) or attestation.get("format") != FORMAT:
        found = attestation.get("format") if isinstance(attestation, dict) else None
        raise BundleError(f"{path.name}: unknown format {found!r}, this version reads {FORMAT}")
    summary = parts[SUMMARY].decode("utf-8") if SUMMARY in parts else None
    extra = {name: data for name, data in parts.items() if name not in LAYOUT}
    return Bundle(path, transcript, attestation, summary, extra)


def write(bundle: Bundle) -> None:
    files = bundle.attestation.setdefault("files", {})
    parts = {TRANSCRIPT: bundle.transcript.encode("utf-8")}
    if bundle.summary is not None:
        parts[SUMMARY] = bundle.summary.encode("utf-8")
    else:
        files.pop(SUMMARY, None)
    for name, data in parts.items():
        files.setdefault(name, {})["sha256"] = sha256(data)
    attestation = json.dumps(bundle.attestation, indent=2, ensure_ascii=False) + "\n"

    directory = bundle.path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{bundle.path.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            with zipfile.ZipFile(handle, "w") as archive:
                _add(archive, MIMETYPE_NAME, MIMETYPE, zipfile.ZIP_STORED)
                for name, data in parts.items():
                    _add(archive, name, data)
                for name, data in bundle.extra.items():
                    _add(archive, name, data)
                _add(archive, ATTESTATION, attestation.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, bundle.path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _add(
    archive: zipfile.ZipFile, name: str, data: bytes, method: int = zipfile.ZIP_DEFLATED
) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
    info.compress_type = method
    info.external_attr = 0o600 << 16
    archive.writestr(info, data)


def set_summary(bundle: Bundle, summary: str, device: str | None) -> None:
    bundle.summary = summary
    bundle.attestation.setdefault("files", {})[SUMMARY] = summary_entry(device)
    write(bundle)


def drop_summary(bundle: Bundle) -> None:
    bundle.summary = None
    write(bundle)


def verify(bundle: Bundle) -> list[str]:
    files = bundle.attestation.get("files", {})
    actual = {TRANSCRIPT: bundle.transcript}
    if bundle.summary is not None:
        actual[SUMMARY] = bundle.summary
    bad = [
        name
        for name, text in actual.items()
        if files.get(name, {}).get("sha256") != sha256(text.encode("utf-8"))
    ]
    bad += [name for name in files if name not in actual]
    return sorted(bad)


def stamps(text: str) -> list[tuple[int, int]]:
    return [
        (match.start(), int(match.group(1)) * 60 + int(match.group(2)))
        for match in _STAMP.finditer(text)
    ]


def last_stamp_s(text: str) -> int | None:
    found = stamps(text)
    return found[-1][1] if found else None


def from_transcript(
    transcript: Path,
    started: datetime | None = None,
    duration_s: int | None = None,
    language: str | None = None,
    task: str | None = None,
    device: str | None = None,
    model_id: str | None = None,
) -> Bundle:
    text = transcript.read_text(encoding="utf-8")
    header = _HEADER.match(text.split("\n", 1)[0])
    source_label = header.group(2) if header else None
    if header and device is None and header.group(3) not in ("", "?"):
        device = header.group(3)
    if started is None:
        started = _started_from(transcript, header)
    if duration_s is None:
        duration_s = last_stamp_s(text)
    attestation = new_attestation(
        started, duration_s, source_label, language, task, device, model_id
    )
    return Bundle(bundle_path_for(transcript), text, attestation)


def _started_from(transcript: Path, header: re.Match | None) -> datetime | None:
    match = _NAME.match(transcript.name)
    try:
        if match:
            return datetime.strptime(match.group(1), "%Y-%m-%d-%H%M%S").astimezone()
        if header:
            return datetime.strptime(header.group(1), "%Y-%m-%d %H:%M").astimezone()
    except ValueError:
        return None
    return None


def bundle_transcript(transcript: Path, **meta) -> Path:
    bundle = from_transcript(transcript, **meta)
    if bundle.path.exists():
        raise BundleError(f"{bundle.path.name} already exists")
    write(bundle)
    transcript.unlink()
    return bundle.path


def listing(directory: Path | None = None) -> list[Path]:
    directory = directory or config.TRANSCRIPT_DIR
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.iterdir() if is_bundle_name(p)), reverse=True)
