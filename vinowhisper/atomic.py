import json
import os
import tempfile
from pathlib import Path


def _open_temp(path: Path, private: bool, temp_suffix: str) -> tuple[int, Path]:
    if private:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=temp_suffix, dir=path.parent)
        return fd, Path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}{temp_suffix}")
    return os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666), temp


def write_json(
    path: Path, value: object, *, private: bool = False, temp_suffix: str = ".tmp"
) -> None:
    # Renamed into place: every reader treats a torn file as "nothing saved", silently.
    fd, temp = _open_temp(path, private, temp_suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
