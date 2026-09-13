"""The model export as an optional extra, and the wizard's way into it.

Only the one-time export uses optimum and transformers, and optimum brings
torch. Until 2026-09-12 they were runtime dependencies, so every install
carried torch and resolved transformers 5.5.4, whose NPU export fails at
generate(), which made `vinowhisper-setup` fail at the model step on a fresh
machine.
"""

import sys
import tomllib
from pathlib import Path

from vinowhisper import config, devices, wizard

ROOT = Path(__file__).resolve().parent.parent


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_optimum_is_not_a_runtime_dependency():
    assert not [
        dep for dep in _project()["dependencies"] if dep.startswith(("optimum", "transformers"))
    ]


def test_characterization_the_export_extra_holds_transformers_below_5_4():
    """characterization: transformers is held back, on purpose, in the export extra.

    Bisected on hardware 2026-09-04: from 5.4.0 on, the decoder graph no longer
    names the internal cache_position tensor that the NPU static pipeline looks
    up, so the export builds and generate() fails. Removing the cap makes a
    fresh `vinowhisper-setup` export a model the digest check calls known_bad.
    Lift it when a transformers release exports a working decoder again.
    """
    extra = _project()["optional-dependencies"]["export"]
    assert "optimum[openvino]" in extra
    assert "transformers<5.4" in extra


def use_cpu(monkeypatch, tmp_path):
    cpu = devices.Device(name="CPU", kind="CPU", full_name="fake")
    monkeypatch.setattr(devices, "available", lambda: [cpu])
    monkeypatch.setattr(config, "STATEFUL_MODEL_DIR", tmp_path / "stateful")


def test_a_missing_optimum_offers_the_extra_instead_of_a_doomed_export(
    monkeypatch, tmp_path, capsys
):
    use_cpu(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "optimum_cli", lambda: None)
    outcome = wizard.Wizard(dry_run=True).check_model()
    out = capsys.readouterr().out
    assert outcome.ok is None
    assert "export" in outcome.summary
    assert "vinowhisper[export]" in out or "--extra export" in out
    assert "optimum-cli export" not in out


def test_the_export_runs_the_optimum_cli_it_found(monkeypatch, tmp_path, capsys):
    use_cpu(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "optimum_cli", lambda: "/venv/bin/optimum-cli")
    wizard.Wizard(dry_run=True).check_model()
    assert "$ /venv/bin/optimum-cli export openvino" in capsys.readouterr().out


def test_a_checkout_installs_the_extra_with_uv_sync(monkeypatch, tmp_path):
    script = tmp_path / "convert_model.sh"
    script.write_text("")
    monkeypatch.setattr(config, "CONVERT_SCRIPT", script)
    monkeypatch.setattr(wizard.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert wizard.export_extra_argv()[1:4] == ["sync", "--extra", "export"]


def test_a_wheel_install_pins_the_extra_to_this_version(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONVERT_SCRIPT", tmp_path / "absent.sh")
    monkeypatch.setattr(wizard, "_has_pip", lambda: True)
    argv = wizard.export_extra_argv()
    assert argv[:4] == [sys.executable, "-m", "pip", "install"]
    assert argv[-1] == f"vinowhisper[export]=={wizard.__version__}"


def test_a_venv_without_pip_goes_through_uv(monkeypatch, tmp_path):
    """uv-created venvs have no pip, and `python -m pip` would just fail."""
    monkeypatch.setattr(config, "CONVERT_SCRIPT", tmp_path / "absent.sh")
    monkeypatch.setattr(wizard, "_has_pip", lambda: False)
    monkeypatch.setattr(wizard.shutil, "which", lambda name: f"/usr/bin/{name}")
    argv = wizard.export_extra_argv()
    assert argv[1:5] == ["pip", "install", "--python", sys.executable]


def test_optimum_cli_is_found_beside_the_interpreter_before_path(monkeypatch, tmp_path):
    """A symlinked vinowhisper-setup runs without the venv's bin/ on PATH."""
    (tmp_path / "python").write_text("")
    (tmp_path / "optimum-cli").write_text("")
    monkeypatch.setattr(wizard.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(wizard.shutil, "which", lambda name: None)
    assert wizard.optimum_cli() == str(tmp_path / "optimum-cli")
