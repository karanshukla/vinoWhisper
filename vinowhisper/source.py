import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import requests

from . import config
from .integrity import sha256_file

SOURCES_PATH = Path(__file__).resolve().parent / "model_sources.json"
SOURCE_ROOT = config.MODEL_ROOT / "source"

HUB_URL = "https://huggingface.co"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNPINNED = 2

_CHUNK = 1 << 20
_TIMEOUT = (10.0, 60.0)
_PART = ".part"


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    model_id: str
    revision: str
    recorded: str = ""
    sha256: dict[str, str] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return SOURCE_ROOT / f"{self.model_id.replace('/', '--')}@{self.revision}"

    def url(self, name: str) -> str:
        return f"{HUB_URL}/{self.model_id}/resolve/{self.revision}/{name}"


def load(model_id: str = config.MODEL_ID, path: Path | None = None) -> Source | None:
    try:
        raw = json.loads((path or SOURCES_PATH).read_text(encoding="utf-8"))
        entry = raw["sources"][model_id]
        files = entry["files"]
        return Source(
            model_id=model_id,
            revision=str(entry["revision"]),
            recorded=str(entry.get("recorded", "")),
            sha256={name: str(meta["sha256"]) for name, meta in files.items()},
            sizes={name: int(meta.get("size", 0)) for name, meta in files.items()},
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def problems(source: Source, directory: Path) -> list[str]:
    found: list[str] = []
    for name, expected in sorted(source.sha256.items()):
        path = directory / name
        if not path.is_file():
            found.append(f"{name}: missing")
        elif sha256_file(path) != expected:
            found.append(f"{name}: sha256 differs from the pin")
    # The exporter reads whatever is in the directory, not just the pinned names.
    found += [
        f"{path.name}: not in the pin"
        for path in sorted(directory.iterdir())
        if path.name not in source.sha256
    ]
    return found


def fetch(
    source: Source,
    directory: Path | None = None,
    session: requests.Session | None = None,
    say: Callable[[str], None] = lambda line: print(line, file=sys.stderr, flush=True),
) -> Path:
    target_dir = directory or source.directory
    target_dir.mkdir(parents=True, exist_ok=True)
    for leftover in target_dir.glob(f"*{_PART}"):
        leftover.unlink()
    http = session or requests.Session()

    for name, expected in sorted(source.sha256.items()):
        target = target_dir / name
        if target.is_file() and sha256_file(target) == expected:
            continue
        size = source.sizes.get(name, 0)
        say(f"downloading {name}" + (f" ({size / 1e6:.0f}MB)" if size >= 10**7 else ""))
        part = target.with_name(name + _PART)
        digest = hashlib.sha256()
        with http.get(source.url(name), stream=True, timeout=_TIMEOUT) as response:
            response.raise_for_status()
            with part.open("wb") as handle:
                for chunk in response.iter_content(_CHUNK):
                    digest.update(chunk)
                    handle.write(chunk)
        if digest.hexdigest() != expected:
            part.unlink()
            raise SourceError(
                f"{source.model_id}@{source.revision[:12]}: {name} does not match its pinned "
                f"sha256 (got {digest.hexdigest()}, pinned {expected}). Nothing was exported. "
                "Retry once; if it still differs, open an issue rather than working around it."
            )
        part.replace(target)

    found = problems(source, target_dir)
    if found:
        raise SourceError(
            f"{target_dir} does not match the pinned source: {'; '.join(found)}. "
            f"Remove it and re-run: rm -rf {target_dir}"
        )
    return target_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vinowhisper.source",
        description="Download the pinned Hugging Face revision of a model and check every "
        "file's sha256. Prints the directory to export from.",
        epilog=f"Exit codes: {EXIT_OK} verified, {EXIT_FAILED} download or digest failure, "
        f"{EXIT_UNPINNED} no pinned source for this model.",
    )
    parser.add_argument("--model", default=config.MODEL_ID, help="Hugging Face model id.")
    args = parser.parse_args(argv)

    source = load(args.model)
    if source is None:
        print(f"no pinned source for {args.model} in {SOURCES_PATH}", file=sys.stderr)
        return EXIT_UNPINNED
    try:
        directory = fetch(source)
    except SourceError as exc:
        print(f"source check failed: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except (requests.RequestException, OSError) as exc:
        print(f"download failed: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(directory)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
