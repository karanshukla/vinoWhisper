import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version

# Makes OpenVINO's telemetry unimportable, before anything imports openvino (SECURITY.md).
sys.modules.setdefault("openvino_telemetry", None)  # type: ignore[arg-type]

# The package's own number, for pins and requirements. What the app shows is `version.VERSION`.
try:
    __version__ = _metadata_version("vinowhisper")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
