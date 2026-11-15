import sys
from importlib.metadata import PackageNotFoundError, version

# Makes OpenVINO's telemetry unimportable, before anything imports openvino (SECURITY.md).
sys.modules.setdefault("openvino_telemetry", None)  # type: ignore[arg-type]

try:
    __version__ = version("vinowhisper")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
