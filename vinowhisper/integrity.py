"""Digest verification for the model export, against a pinned set of hashes.

`scripts/convert_model.sh` and `vinowhisper-setup` download ~1GB from Hugging
Face and hand the result to a pipeline that runs on your hardware. Until this
module there was nothing between "the network returned some bytes" and that.
Prior art is OpenWhispr's `whisperVulkanManager.js`, which pins the digests of
the binaries it fetches so untested future ones never auto-ship; the same
argument applies to weights, plus the integrity one.

Three decisions worth stating, because each has an obvious-looking alternative:

**The exported IR is what gets digested, not the upstream safetensors.** The
IR is what `WhisperPipeline` actually loads, and the export is not a pure
function of the weights: optimum-intel, transformers, torch and OpenVINO all
leave fingerprints in it. Digesting upstream would verify a file this program
never opens. The cost is that the pin has to be regenerated whenever the
export toolchain moves, which is why `scripts/update_digests.py` exists rather
than a paragraph in CONTRIBUTING telling someone to paste hashes by hand.

**Toolchain drift is reported differently from a real mismatch, and the
export tells us which it is.** Every OpenVINO IR carries an `rt_info` block
naming the runtime and the optimum/transformers/torch versions that produced
it, so a differing export can be compared against the toolchain the pin was
made with. Same toolchain and different bytes is the alarming case. Different
toolchain and different bytes is Tuesday. Without that split, the first
optimum release after a pin would train everybody to ignore the warning.

**An unpinned export warns and continues.** A hard failure on an unrecognised
(model, variant) would make `--model openai/whisper-base.en` unusable the
first time anyone tried it, and this project ships exactly one pinned export.
Verification is a check on the pinned path, not a gate on every path.

Deliberately not wired into `transcriber.load()`. Hashing 1.5GB costs ~1.2s,
which is small against a 10-30s NPU model load but sits on the socket-
activated cold-start path, which is the one latency figure this project
actually defends. Verification runs where the bytes arrive (the wizard, the
convert script) and on demand (`vinowhisper-doctor`).
"""

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config

# Where the generated pins live. JSON rather than a Python literal so the
# regeneration script writes data instead of rewriting source, and so a diff
# on it reads as a diff on hashes.
PINS_PATH = Path(__file__).resolve().parent / "model_digests.json"

SCHEMA = 1

VERIFIED = "verified"
UNPINNED = "unpinned"
DRIFT = "drift"
MISMATCH = "mismatch"
INCOMPLETE = "incomplete"
KNOWN_BAD = "known_bad"

# Statuses that mean "stop and look at this". The other three are informational:
# UNPINNED is the documented default for an export nobody has pinned, and DRIFT
# is what a toolchain upgrade looks like.
SEVERE = (MISMATCH, INCOMPLETE, KNOWN_BAD)

_CHUNK = 1 << 20

# rt_info sits at the end of the .xml, after the whole graph. Pulled with a
# regex rather than an XML parse for two reasons: the decoder graph is 600KB of
# XML to reach five attributes, and ElementTree expands entities, which is a
# needless thing to do to a file whose provenance this module exists to doubt.
_RT_INFO = re.compile(r"<rt_info>(.*?)</rt_info>", re.DOTALL)
_INFO_VALUE = re.compile(r'<info name="([^"]+)" value="([^"]*)"')
_TAG_VALUE = re.compile(r"<(\w+) value=\"([^\"]*)\" */>")

# Read far enough back to catch rt_info without loading the whole graph.
_TAIL_BYTES = 16 << 10


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def digest_export(directory: Path) -> dict[str, str]:
    """Every file in an export directory, name -> sha256, sorted by name.

    Flat and non-recursive because optimum's export is: one directory of
    .xml/.bin pairs plus the tokenizer and config JSON. All of it is loaded,
    so all of it is digested. `generation_config.json` decides how decoding
    behaves and `tokenizer.json` decides what the text comes out as, so
    "just the weights" would be a smaller claim than it sounds.
    """
    return {
        path.name: sha256_file(path)
        for path in sorted(directory.iterdir(), key=lambda item: item.name)
        if path.is_file() and not path.name.startswith(".")
    }


def read_toolchain(directory: Path) -> dict[str, str]:
    """The versions that produced this export, read out of its own rt_info.

    Empty if there is no readable IR in the directory, which callers treat as
    "unknown" rather than as a mismatch: an unknown toolchain cannot be
    compared, so it cannot excuse differing bytes either.
    """
    for path in sorted(directory.glob("*.xml")):
        found = _rt_info(path)
        if found:
            return found
    return {}


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
    # The <optimum> child names its versions as tags rather than as info
    # elements: <optimum_version value="2.2.0" />. Same data, different shape.
    found.update(
        {
            name: value
            for name, value in _TAG_VALUE.findall(body)
            if name.endswith("_version") and name not in found
        }
    )
    # <Runtime_version> repeats <info name="OpenVINO Runtime"> verbatim, and it
    # matches the *_version tag pattern above, so it has to be dropped after
    # that merge rather than before it. Carrying both doubles the length of
    # every drift message for no information.
    found.pop("Runtime_version", None)
    return found


@dataclass(frozen=True)
class Pin:
    """One pinned export. `files` empty means present-but-unpinned."""

    model_id: str
    variant: str
    recorded: str = ""
    note: str = ""
    toolchain: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    # Export toolchains measured to produce a *broken* export, as
    # [{"match": {version key: value, ...}, "reason": "..."}]. Not an integrity
    # concern and kept here anyway, because this file is already the record of
    # which toolchain the pinned export came from, and a second file saying
    # which ones not to use would drift away from it. Regeneration preserves
    # these: scripts/update_digests.py writes hashes, never this list.
    known_bad: list[dict[str, Any]] = field(default_factory=list)

    @property
    def pinned(self) -> bool:
        return bool(self.files)

    def bad_toolchain(self, toolchain: dict[str, str]) -> str:
        """The reason this toolchain is known bad, or "" if it is not listed.

        Every key in an entry's `match` has to equal the local value, so a
        partial overlap (same optimum-intel, different transformers) does not
        fire. Conservative on purpose: a false alarm here is worse than a
        missed one, since the digest comparison still reports the drift.
        """
        for entry in self.known_bad:
            match = entry.get("match") or {}
            if match and all(toolchain.get(key) == value for key, value in match.items()):
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
        """One line, which is all the doctor and the wizard have room for."""
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
        """The longer form, for the convert script and `--verify` on its own."""
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


def _short(version: str | None) -> str:
    """`2026.3.1-22476-759c5a6ab8c-releases/2026/3` -> `2026.3.1`, for messages.

    The build hash stays in the pin file, where it is evidence. It just has no
    business in a one-line summary next to five other versions.
    """
    return (version or "?").split("-", 1)[0]


def _key(model_id: str, variant: str) -> str:
    return f"{model_id}/{variant}"


def load_pins(path: Path | None = None) -> dict[str, Pin]:
    """Read the pin file. A missing or unreadable one means "nothing pinned".

    Never raises. A wheel built without the data file, or a truncated one,
    should downgrade verification to UNPINNED rather than break every export.
    """
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
    """Compare an export directory against its pin."""
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
        # Checked ahead of DRIFT, not ahead of MISMATCH: "these are the bytes a
        # broken toolchain makes" is the more useful answer than "your versions
        # moved", but "the pinned toolchain made different bytes" is still the
        # more alarming one and keeps priority.
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
    """Build a pin from an export that is already on disk.

    Carries the existing entry's known-bad toolchain list forward. That list is
    hand-written from measurement and regenerating hashes is not a reason to
    lose it, which it silently would be if this built a Pin from scratch.
    """
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
    """Merge one pin into the pin file, leaving the other entries alone."""
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
