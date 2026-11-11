import contextlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import config, source

LABEL = "Whisper small"
SERVER_UNIT = "vinowhisper-server.service"


def export_dirs() -> tuple[Path, ...]:
    # Every directory config.model_dir can name for Whisper: either variant, either language set.
    return (
        config.MODEL_DIR,
        config.STATEFUL_MODEL_DIR,
        config.MULTILINGUAL_MODEL_DIR,
        config.MULTILINGUAL_STATEFUL_MODEL_DIR,
    )


def step_dirs() -> tuple[Path, ...]:
    # The loop's fp16 pair, and the INT8 siblings scripts/export_whisper.py writes beside them.
    fp16 = (config.STEP_MODEL_DIR, config.MULTILINGUAL_STEP_MODEL_DIR)
    int8 = tuple(d.with_name(d.name.removesuffix("-fp16")) for d in fp16)
    return tuple(dict.fromkeys(fp16 + int8))


def source_dirs() -> list[Path]:
    # Only Whisper's snapshots: export_canary.py keeps Canary's source beside them.
    root = source.SOURCE_ROOT
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.name.startswith("openai--whisper-small"))


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def _exported(directory: Path) -> bool:
    return directory.is_dir() and any(directory.glob("*.xml"))


def in_use() -> bool:
    # The server loads Whisper unless Canary is picked; then deleting Whisper leaves it running.
    return config.load_language()[2] == config.WHISPER_MODEL


def model_status() -> dict:
    exports = [d for d in export_dirs() if _exported(d)]
    steps = [d for d in step_dirs() if _exported(d)]
    sources = source_dirs()
    return {
        "label": LABEL,
        # A step export decodes nothing without the pipeline's export beside it.
        "present": bool(exports),
        "bytes": sum(_size(d) for d in exports + steps),
        "download_bytes": 0,
        "cache_bytes": 0,
        "source_bytes": sum(_size(d) for d in sources),
        "in_use": in_use(),
        "exports": [d.name for d in exports],
        "step_exports": [d.name for d in steps],
        "dir": str(config.MODEL_ROOT),
    }


def stop_server() -> bool:
    # As a language switch does (docs/tray.md); the socket stays, so the next use starts it.
    try:
        done = subprocess.run(
            ["systemctl", "--user", "stop", SERVER_UNIT], check=False, capture_output=True
        )
    except OSError:
        return False
    return done.returncode == 0


def _remove(directory: Path) -> None:
    # Not ignore_errors: a delete that failed must not report success.
    if directory.is_symlink() or directory.is_file():
        directory.unlink()
    elif directory.exists():
        shutil.rmtree(directory)


def delete_source() -> None:
    for directory in source_dirs():
        _remove(directory)
    # Left empty, it is ours; Canary's snapshot keeps it otherwise.
    with contextlib.suppress(OSError):
        source.SOURCE_ROOT.rmdir()


def model_delete() -> bool:
    stopped = False
    if in_use() and any(_exported(d) for d in export_dirs()):
        stopped = stop_server()
    for directory in export_dirs() + step_dirs():
        _remove(directory)
    delete_source()
    return stopped


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="vinowhisper-whisper",
        description="The Whisper exports vinowhisper-setup built, and their source download. "
        "Each command prints JSON lines. delete removes them all and stops the server if it "
        "runs Whisper: captions and dictation stay off until vinowhisper-setup exports it again. "
        "delete-source removes only the download the export was built from.",
    )
    parser.add_argument("what", choices=("model",))
    parser.add_argument("action", choices=("status", "delete", "delete-source"))
    action = parser.parse_args(sys.argv[1:] if argv is None else argv).action

    def emit(record: dict) -> None:
        print(json.dumps(record), flush=True)

    try:
        if action == "status":
            emit(model_status())
        elif action == "delete-source":
            delete_source()
            emit({"event": "deleted"})
        else:
            stopped = model_delete()
            emit({"event": "deleted", "server_stopped": stopped})
    except OSError as exc:
        emit({"event": "error", "message": str(exc).splitlines()[0]})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
