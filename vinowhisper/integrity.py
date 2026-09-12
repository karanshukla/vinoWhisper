import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config

PINS_PATH = Path(__file__).resolve().parent / "model_digests.json"

SCHEMA = 1

VERIFIED = "verified"
UNPINNED = "unpinned"
DRIFT = "drift"
MISMATCH = "mismatch"
INCOMPLETE = "incomplete"
KNOWN_BAD = "known_bad"

SEVERE = (MISMATCH, INCOMPLETE, KNOWN_BAD)

_CHUNK = 1 << 20

# Regex, not ElementTree: 600KB of XML to reach five attributes, and no entity expansion on an untrusted file.
_RT_INFO = re.compile(r"<rt_info>(.*?)</rt_info>", re.DOTALL)
_INFO_VALUE = re.compile(r'<info name="([^"]+)" value="([^"]*)"')
_TAG_VALUE = re.compile(r"<(\w+) value=\"([^\"]*)\" */>")

_TAIL_BYTES = 16 << 10


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def digest_export(directory: Path) -> dict[str, str]:
    return {
        path.name: sha256_file(path)
        for path in sorted(directory.iterdir(), key=lambda item: item.name)
        if path.is_file() and not path.name.startswith(".")
    }


def read_toolchain(directory: Path) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in sorted(directory.glob("*.xml")):
        for key, value in _rt_info(path).items():
            if merged.setdefault(key, value) != value:
                return {}
    return merged


def _rt_info(path: Path) -> dict[str, str]:
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(max(0, size - _TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return {}

    block = _RT_INFO.search(tail)
    if not block:
        return {}
    body = block.group(1)
    found = dict(_INFO_VALUE.findall(body))
    found.update(
        {
            name: value
            for name, value in _TAG_VALUE.findall(body)
            if name.endswith("_version") and name not in found
        }
    )
    # After the tag merge: it also matches the *_version pattern.
    found.pop("Runtime_version", None)
    return found


@dataclass(frozen=True)
class Pin:
    model_id: str
    variant: str
    recorded: str = ""
    note: str = ""
    toolchain: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    known_bad: list[dict[str, Any]] = field(default_factory=list)

    @property
    def pinned(self) -> bool:
        return bool(self.files)

    def bad_toolchain(self, toolchain: dict[str, str]) -> str:
        for entry in self.known_bad:
            match = entry.get("match") or {}
            floors = entry.get("at_least") or {}
            if not match and not floors:
                continue
            if any(toolchain.get(key) != value for key, value in match.items()):
                continue
            if any(not _at_least(toolchain.get(key), floor) for key, floor in floors.items()):
                continue
            return str(entry.get("reason", "known-bad export toolchain"))
        return ""

    def as_json(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "variant": self.variant,
            "recorded": self.recorded,
            "note": self.note,
            "toolchain": dict(self.toolchain),
            "known_bad": list(self.known_bad),
            "files": dict(self.files),
        }


@dataclass(frozen=True)
class Verification:
    status: str
    model_id: str
    variant: str
    directory: Path
    matched: tuple[str, ...] = ()
    differing: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unpinned_files: tuple[str, ...] = ()
    pinned_toolchain: dict[str, str] = field(default_factory=dict)
    local_toolchain: dict[str, str] = field(default_factory=dict)
    recorded: str = ""
    known_bad_reason: str = ""

    @property
    def severe(self) -> bool:
        return self.status in SEVERE

    def summary(self) -> str:
        if self.status == VERIFIED:
            return f"{len(self.matched)} files match the pin recorded {self.recorded or 'earlier'}"
        if self.status == UNPINNED:
            return (
                f"no pinned digests for {self.model_id} ({self.variant}) — "
                f"exported bytes not verified"
            )
        if self.status == INCOMPLETE:
            return f"{len(self.missing)} pinned file(s) missing: {', '.join(self.missing[:3])}"
        if self.status == KNOWN_BAD:
            return (
                f"exported by a toolchain known to produce a broken export: {self.known_bad_reason}"
            )
        changed = self._changed_kinds()
        if self.status == DRIFT:
            return (
                f"{len(self.differing)} file(s) differ ({changed}), and so does the export "
                f"toolchain — {self._toolchain_delta()}"
            )
        return (
            f"{len(self.differing)} file(s) differ ({changed}) on the SAME toolchain: "
            f"{', '.join(self.differing[:3])}"
        )

    def lines(self) -> list[str]:
        out = [f"{self.status}: {self.directory}", f"  {self.summary()}"]
        if self.status == MISMATCH:
            out += [
                "  The pinned toolchain produced different bytes for these files.",
                "  Re-download from a clean cache before trusting this export:",
                f"    rm -rf {self.directory} ~/.cache/huggingface/hub",
                f"    {config.export_command(self.variant)}",
                "  If it still differs, open an issue rather than using it.",
            ]
        elif self.status == DRIFT:
            out += [
                "  This is what a toolchain upgrade looks like, not necessarily tampering.",
                "  Re-pin once you have satisfied yourself the export is good:",
                f"    python scripts/update_digests.py --variant {self.variant}",
            ]
        elif self.status == INCOMPLETE:
            out += [
                "  The export is missing files it was pinned with, so it is partial.",
                f"    {config.export_command(self.variant)}",
            ]
        elif self.status == KNOWN_BAD:
            out += [
                "  This is not an integrity problem. The bytes are consistent with what",
                "  this toolchain produces; that toolchain is the problem.",
                "  The pinned export was produced by:",
                *[f"    {key} {value}" for key, value in sorted(self.pinned_toolchain.items())],
            ]
        elif self.status == UNPINNED:
            out += [
                "  Nothing is wrong; this export is simply one nobody has pinned.",
                f"    python scripts/update_digests.py --variant {self.variant}"
                f" --model {self.model_id}",
            ]
        if self.unpinned_files:
            out.append(f"  not in the pin, ignored: {', '.join(self.unpinned_files)}")
        return out

    def _changed_kinds(self) -> str:
        suffixes = {Path(name).suffix or "?" for name in self.differing}
        if suffixes == {".xml"}:
            return "graph only"
        if suffixes == {".bin"}:
            return "weights"
        return ", ".join(sorted(suffixes))

    def _toolchain_delta(self) -> str:
        keys = sorted(set(self.pinned_toolchain) | set(self.local_toolchain))
        deltas = [
            f"{key}: {_short(self.pinned_toolchain.get(key))} -> "
            f"{_short(self.local_toolchain.get(key))}"
            for key in keys
            if self.pinned_toolchain.get(key) != self.local_toolchain.get(key)
        ]
        return "; ".join(deltas[:2]) or "unknown versions"


# Not packaging.version: the wizard imports this before anything else is installed.
def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in _short(version).split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def _at_least(version: str | None, floor: str) -> bool:
    if not version:
        return False
    return _version_tuple(version) >= _version_tuple(floor)


def _short(version: str | None) -> str:
    return (version or "?").split("-", 1)[0]


def _key(model_id: str, variant: str) -> str:
    return f"{model_id}/{variant}"


def load_pins(path: Path | None = None) -> dict[str, Pin]:
    source = path or PINS_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        exports = raw["exports"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}

    pins = {}
    for key, entry in exports.items():
        if not isinstance(entry, dict):
            continue
        pins[key] = Pin(
            model_id=entry.get("model_id", ""),
            variant=entry.get("variant", ""),
            recorded=entry.get("recorded", ""),
            note=entry.get("note", ""),
            toolchain=entry.get("toolchain") or {},
            files=entry.get("files") or {},
            known_bad=entry.get("known_bad") or [],
        )
    return pins


def verify(
    directory: Path,
    variant: str,
    model_id: str = config.MODEL_ID,
    pins: dict[str, Pin] | None = None,
) -> Verification:
    table = load_pins() if pins is None else pins
    pin = table.get(_key(model_id, variant))
    local_toolchain = read_toolchain(directory)

    if pin is None or not pin.pinned:
        reason = pin.bad_toolchain(local_toolchain) if pin else ""
        return Verification(
            status=KNOWN_BAD if reason else UNPINNED,
            model_id=model_id,
            variant=variant,
            directory=directory,
            local_toolchain=local_toolchain,
            pinned_toolchain=dict(pin.toolchain) if pin else {},
            known_bad_reason=reason,
        )

    actual = digest_export(directory)
    matched, differing, missing = [], [], []
    for name, expected in sorted(pin.files.items()):
        if name not in actual:
            missing.append(name)
        elif actual[name] == expected:
            matched.append(name)
        else:
            differing.append(name)
    extra = sorted(set(actual) - set(pin.files))

    reason = pin.bad_toolchain(local_toolchain)
    if missing:
        status = INCOMPLETE
    elif not differing:
        status = VERIFIED
    elif reason:
        status = KNOWN_BAD
    elif local_toolchain and pin.toolchain and local_toolchain != pin.toolchain:
        status = DRIFT
    else:
        status = MISMATCH

    return Verification(
        status=status,
        model_id=model_id,
        variant=variant,
        directory=directory,
        matched=tuple(matched),
        differing=tuple(differing),
        missing=tuple(missing),
        unpinned_files=tuple(extra),
        pinned_toolchain=dict(pin.toolchain),
        local_toolchain=local_toolchain,
        recorded=pin.recorded,
        known_bad_reason=reason,
    )


def record(
    directory: Path,
    variant: str,
    model_id: str,
    recorded: str,
    note: str = "",
    pins: dict[str, Pin] | None = None,
) -> Pin:
    table = load_pins() if pins is None else pins
    existing = table.get(_key(model_id, variant))
    return Pin(
        model_id=model_id,
        variant=variant,
        recorded=recorded,
        note=note,
        toolchain=read_toolchain(directory),
        files=digest_export(directory),
        known_bad=list(existing.known_bad) if existing else [],
    )


def write_pin(pin: Pin, path: Path | None = None) -> Path:
    target = path or PINS_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw.setdefault("schema", SCHEMA)
    raw.setdefault(
        "generated_by",
        "scripts/update_digests.py — generated data, do not edit by hand",
    )
    raw.setdefault("exports", {})
    raw["exports"][_key(pin.model_id, pin.variant)] = pin.as_json()
    raw["exports"] = dict(sorted(raw["exports"].items()))
    target.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vinowhisper.integrity",
        description="Verify a model export against the pinned digests.",
        epilog=(
            "Exit codes: 0 verified, unpinned or toolchain drift; "
            "1 the pinned toolchain produced different bytes, or the export is partial."
        ),
    )
    parser.add_argument(
        "--variant",
        default="npu",
        choices=("npu", "stateful"),
        help="Which export to check (default: npu).",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Export directory (default: the one this variant lives in).",
    )
    parser.add_argument("--model", default=config.MODEL_ID, help="Hugging Face model id.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args(argv)

    directory = args.dir or config.model_dir("NPU" if args.variant == "npu" else "CPU")
    if not directory.is_dir():
        print(f"no export at {directory}", file=sys.stderr)
        return 1

    result = verify(directory, args.variant, args.model)
    if args.json:
        print(
            json.dumps(
                {
                    "status": result.status,
                    "model_id": result.model_id,
                    "variant": result.variant,
                    "directory": str(result.directory),
                    "summary": result.summary(),
                    "matched": list(result.matched),
                    "differing": list(result.differing),
                    "missing": list(result.missing),
                    "pinned_toolchain": result.pinned_toolchain,
                    "local_toolchain": result.local_toolchain,
                },
                indent=2,
            )
        )
    else:
        for line in result.lines():
            print(line)
    return 1 if result.severe else 0


if __name__ == "__main__":
    sys.exit(main())
