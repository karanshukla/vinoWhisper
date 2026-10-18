import sys
from importlib.metadata import PackageNotFoundError, version

# OpenVINO's bundled telemetry (openvino_telemetry, loaded through ovc when
# openvino is imported) takes a missing ~/intel/openvino_telemetry consent
# file as consent and reports usage to Google Analytics. A None entry makes
# it unimportable in this process, so ovc falls back to its no-op stub. Here
# because every entry point imports the package before openvino. The consent
# file is left alone: it is the user's choice for their other OpenVINO tools.
sys.modules.setdefault("openvino_telemetry", None)  # type: ignore[arg-type]

try:
    __version__ = version("vinowhisper")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
