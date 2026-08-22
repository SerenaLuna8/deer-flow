"""Configuration v1 baseline and upgrade-script contracts."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from deerflow.config.app_config import CURRENT_CONFIG_VERSION, AppConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"
CONFIG_UPGRADE = REPO_ROOT / "scripts/config-upgrade.sh"
WIZARD_WRITER = REPO_ROOT / "scripts/wizard/writer.py"
DOCTOR = REPO_ROOT / "scripts/doctor.py"


def _load_wizard_writer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "actweave_test_wizard_writer",
        WIZARD_WRITER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_doctor() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "actweave_test_doctor",
        DOCTOR,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_config_upgrade(config_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CONFIG_UPGRADE)],
        cwd=REPO_ROOT,
        env={**os.environ, "ACT_WEAVE_CONFIG_PATH": str(config_path)},
        check=False,
        capture_output=True,
        text=True,
    )


def test_checked_in_and_wizard_configuration_start_at_public_v1() -> None:
    example = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    assert example["config_version"] == CURRENT_CONFIG_VERSION == 1

    writer = _load_wizard_writer()
    generated = yaml.safe_load(
        writer.build_minimal_config(
            base_config={
                "sandbox": {
                    "use": "deerflow.sandbox.local:LocalSandboxProvider",
                }
            }
        )
    )
    assert generated["config_version"] == 1


def test_config_upgrade_normalizes_unversioned_pre_release_file_to_v1(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    original = """\
sandbox:
  use: src.sandbox.local:LocalSandboxProvider
authorization: {}
models: []
scheduler: {}
memory_document: {}
agent_middlewares: []
uploads:
  max_files: 5
auth:
  local:
    allow_registration: true
mcp_security:
  project_remote_allowed_endpoints: []
"""
    config_path.write_text(original, encoding="utf-8")

    completed = _run_config_upgrade(config_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    upgraded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert upgraded["config_version"] == 1
    assert upgraded["sandbox"]["use"] == ("deerflow.sandbox.local:LocalSandboxProvider")
    assert upgraded["sandbox"]["host_execution_approval"]["mode"] == "disabled"
    assert upgraded["mcp_security"]["project_remote_allowed_networks"] == []
    assert "project_remote_allowed_endpoints" not in upgraded["mcp_security"]
    assert "authorization" not in upgraded
    assert "models" not in upgraded
    assert "scheduler" not in upgraded
    assert "memory_document" not in upgraded
    assert "agent_middlewares" not in upgraded
    assert "uploads" not in upgraded or "max_files" not in upgraded["uploads"]
    assert "auth" not in upgraded or "allow_registration" not in upgraded["auth"].get("local", {})
    assert config_path.with_suffix(".yaml.bak").read_text(encoding="utf-8") == original


def test_config_upgrade_refuses_to_downgrade_a_newer_configuration(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    original = "config_version: 2\nsandbox:\n  use: future:Provider\n"
    config_path.write_text(original, encoding="utf-8")

    completed = _run_config_upgrade(config_path)

    assert completed.returncode == 1
    assert "newer than the supported version 1" in completed.stdout
    assert config_path.read_text(encoding="utf-8") == original
    assert not config_path.with_suffix(".yaml.bak").exists()


def test_runtime_and_doctor_reject_a_configuration_from_a_future_schema(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("config_version: 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="CONFIG_VERSION_UNSUPPORTED"):
        AppConfig._check_config_version(
            {"config_version": 2},
            config_path,
        )

    (tmp_path / "config.example.yaml").write_text(
        "config_version: 1\n",
        encoding="utf-8",
    )
    result = _load_doctor().check_config_version(config_path, tmp_path)
    assert result.status == "fail"
    assert result.detail == "v2 > v1 (supported)"
