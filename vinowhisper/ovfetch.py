import json
import re
import shutil
import subprocess
from typing import Any

from .devices import Note

_TIMEOUT_S = 10
INSTALL_HINT = "cargo install ovfetch --locked"


class OvfetchError(Exception):
    pass


def detect() -> dict[str, Any] | None:
    exe = shutil.which("ovfetch")
    if exe is None:
        return None
    try:
        completed = subprocess.run(
            [exe, "detect"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OvfetchError(f"{exe} detect: {exc}") from exc
    if completed.returncode != 0:
        tail = (completed.stderr.strip().splitlines() or ["no output"])[-1]
        raise OvfetchError(f"{exe} detect exited {completed.returncode}: {tail}")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise OvfetchError(f"{exe} detect printed something other than JSON") from exc
    if not isinstance(payload, dict):
        raise OvfetchError(f"{exe} detect printed something other than a JSON object")
    return payload


def _minor(text: str | None) -> tuple[int, int] | None:
    match = re.match(r"(\d+)\.(\d+)", text or "")
    return (int(match[1]), int(match[2])) if match else None


def _dotted(version: tuple[int, int]) -> str:
    return f"{version[0]}.{version[1]}"


def notes(status: dict[str, Any], openvino_version: str | None) -> list[Note]:
    if "openvino_ceiling" not in status:
        return [
            Note(
                None,
                "ovfetch",
                f"this ovfetch predates platform data in `detect`; {INSTALL_HINT}",
            )
        ]
    if not any(device.get("kind") == "npu" for device in status.get("devices", [])):
        return [Note(True, "ovfetch", "no Intel NPU, so no driver bounds apply")]

    driver = status.get("npu_driver") or "none"
    result = [
        Note(
            True,
            "npu: platform",
            f"{platform.get('codename') or 'unnamed'} ({platform.get('pci_id')}), "
            f"Intel verifies driver {platform.get('min_npu_driver') or '?'} and newer",
        )
        for platform in status.get("platforms", [])
    ]
    result += [Note(False, "npu: ovfetch", str(warning)) for warning in status.get("warnings", [])]

    installed = _minor(openvino_version)
    ceiling = _minor(status.get("openvino_ceiling"))
    floors = [_minor(p.get("min_openvino")) for p in status.get("platforms", [])]
    floor = max((f for f in floors if f is not None), default=None)
    if installed is None:
        return result
    if ceiling is None:
        result.append(
            Note(
                None,
                "npu: openvino range",
                f"no OpenVINO is recorded as working with NPU driver {driver}",
            )
        )
    elif installed > ceiling:
        result.append(
            Note(
                False,
                "npu: openvino range",
                f"OpenVINO {_dotted(installed)} is newer than {_dotted(ceiling)}, the newest "
                f"recorded as working with NPU driver {driver}. Untested rather than known "
                "to fail; if NPU compiles fail, suspect this first.",
            )
        )
    elif floor is not None and installed < floor:
        result.append(
            Note(
                False,
                "npu: openvino range",
                f"OpenVINO {_dotted(installed)} is older than {_dotted(floor)}, the oldest "
                "recorded as working on this platform",
            )
        )
    else:
        low = f"{_dotted(floor)} to " if floor else "up to "
        result.append(
            Note(
                True,
                "npu: openvino range",
                f"OpenVINO {_dotted(installed)}, inside {low}{_dotted(ceiling)} "
                f"for NPU driver {driver}",
            )
        )
    return result
