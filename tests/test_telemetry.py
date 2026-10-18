"""OpenVINO's bundled telemetry never runs: importing the package blocks it,
and the model export runs with it switched off."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import vinowhisper  # noqa: F401 - the import is what blocks it
from vinowhisper import wizard

ROOT = Path(__file__).resolve().parent.parent


def test_importing_vinowhisper_blocks_openvino_telemetry():
    assert "openvino_telemetry" in sys.modules
    assert sys.modules["openvino_telemetry"] is None


def test_the_export_runs_with_telemetry_off():
    assert wizard._EXPORT_ENV["CI"] == "true"
    script = (ROOT / "scripts/convert_model.sh").read_text(encoding="utf-8")
    assert script.count("CI=true") >= 2  # pinned and unpinned exports


def test_real_openvino_writes_nothing_to_intel(tmp_path):
    """A fresh process with no consent file: no GA client id, no stats."""
    pytest.importorskip("openvino")
    env = {**os.environ, "HOME": str(tmp_path), "https_proxy": "http://127.0.0.1:9"}
    env.pop("CI", None)  # the point is that it doesn't need CI=true
    code = "import vinowhisper, openvino, openvino_genai; openvino.Core()"
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, check=True, timeout=120)
    assert not (tmp_path / "intel").exists()
