import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import config

CLEAR_HINT = "vinowhisper-doctor clears the mark"

_ERROR_LIMIT = 200
# OpenVINO nests these above the message it is actually raising.
_LOCATION = re.compile(r"^Exception from .*:\d+:$")


@dataclass(frozen=True)
class Failure:
    device: str
    failed_at: str
    error: str

    def __str__(self) -> str:
        return f"{self.device} failed on {self.failed_at}: {self.error}"


def load(path: Path | None = None) -> dict[str, Failure]:
    try:
        raw = json.loads((path or config.FAILED_DEVICES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}

    failures = {}
    for device, entry in raw.items():
        try:
            failures[device] = Failure(
                device=str(device), failed_at=str(entry["failed_at"]), error=str(entry["error"])
            )
        except (KeyError, TypeError):
            continue
    return failures


def record(device: str, error: str, path: Path | None = None) -> Failure:
    failure = Failure(
        device=device,
        failed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        error=_summary(error),
    )
    failures = load(path)
    failures[device] = failure
    _write(failures, path)
    return failure


def forget(device: str, path: Path | None = None) -> None:
    failures = load(path)
    if failures.pop(device, None) is not None:
        _write(failures, path)


def clear(path: Path | None = None) -> list[Failure]:
    failures = load(path)
    if failures:
        _write({}, path)
    return list(failures.values())


def _summary(error: str) -> str:
    lines = [line.strip() for line in error.splitlines() if line.strip()]
    message = [line for line in lines if not _LOCATION.match(line)]
    return (message or lines or ["no error message"])[0][:_ERROR_LIMIT]


def _write(failures: Mapping[str, Failure], path: Path | None) -> None:
    target = path or config.FAILED_DEVICES_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {device: asdict(failure) for device, failure in failures.items()}
    # Renamed into place: a truncated file would read as "nothing failed".
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
