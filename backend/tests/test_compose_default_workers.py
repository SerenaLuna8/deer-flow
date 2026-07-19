"""Regression tests for Docker Compose Gateway worker configuration.

Gateway no longer owns in-process Run state; project Runs execute in the
independent Worker and reconnect through PostgreSQL. These tests retain the
conservative deployment default while proving operators can override it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker" / "docker-compose.yaml"


def _gateway_command() -> str:
    """Return the gateway service command as a single string."""
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    command = compose["services"]["gateway"]["command"]
    # ``command`` may load as a scalar string or a list depending on YAML style.
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    return command


def _gateway_environment() -> list[str]:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return compose["services"]["gateway"]["environment"]


def test_gateway_defaults_to_single_worker():
    """With GATEWAY_WORKERS unset, the worker count must default to 1."""
    command = _gateway_command()
    match = re.search(r"GATEWAY_WORKERS:-(\d+)", command)
    assert match is not None, f"gateway command must set a GATEWAY_WORKERS default; got: {command}"
    assert match.group(1) == "1", f"default Gateway worker count must be 1, got {match.group(1)}"


def test_gateway_worker_count_remains_overridable():
    """The worker count must stay configurable, not hard-coded to 1."""
    command = _gateway_command()
    assert "${GATEWAY_WORKERS:-1}" in command, f"worker count must use ${{GATEWAY_WORKERS:-1}} so operators can override it; got: {command}"


def test_gateway_container_receives_the_same_authoritative_worker_count():
    assert "GATEWAY_WORKERS=${GATEWAY_WORKERS:-1}" in _gateway_environment()


def test_compose_resolves_host_worker_override_identically_in_command_and_environment(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("docker CLI is unavailable")
    version = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, check=False)
    if version.returncode != 0:
        pytest.skip("docker compose is unavailable")

    root = tmp_path / "repo"
    compose_dir = root / "docker"
    compose_dir.mkdir(parents=True)
    (root / "frontend").mkdir()
    (root / ".env").write_text("", encoding="utf-8")
    (root / "frontend" / ".env").write_text("", encoding="utf-8")
    copied_compose = compose_dir / "docker-compose.yaml"
    shutil.copyfile(COMPOSE_PATH, copied_compose)

    environment = os.environ.copy()
    environment.update(
        {
            "GATEWAY_WORKERS": "8",
            "DEER_FLOW_CONFIG_PATH": "/tmp/config.yaml",
            "DEER_FLOW_HOME": "/tmp/deer-flow-home",
            "DEER_FLOW_REPO_ROOT": "/tmp/deer-flow-repo",
        }
    )
    result = subprocess.run(
        ["docker", "compose", "-f", str(copied_compose), "config", "--format", "json"],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )
    gateway = json.loads(result.stdout)["services"]["gateway"]
    assert gateway["environment"]["GATEWAY_WORKERS"] == "8"
    command = gateway["command"]
    if isinstance(command, list):
        command = " ".join(command)
    assert "--workers 8" in command


def test_supported_gateway_launchers_set_authoritative_worker_environment():
    dev_compose = (REPO_ROOT / "docker" / "docker-compose-dev.yaml").read_text(encoding="utf-8")
    dev_entrypoint = (REPO_ROOT / "docker" / "dev-entrypoint.sh").read_text(encoding="utf-8")
    helm = (REPO_ROOT / "deploy" / "helm" / "deer-flow" / "templates" / "gateway-deployment.yaml").read_text(encoding="utf-8")
    makefile = (REPO_ROOT / "backend" / "Makefile").read_text(encoding="utf-8")
    dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    serve = (REPO_ROOT / "scripts" / "serve.sh").read_text(encoding="utf-8")

    assert "GATEWAY_WORKERS=1" in dev_compose
    assert "GATEWAY_WORKERS=1\nexport GATEWAY_WORKERS" in dev_entrypoint
    assert "--workers 1" in helm
    assert 'name: GATEWAY_WORKERS\n              value: "1"' in helm
    assert "GATEWAY_WORKERS=$(GATEWAY_WORKERS)" in makefile
    assert "--workers $(GATEWAY_WORKERS)" in makefile
    assert dockerfile.count("export GATEWAY_WORKERS;") == 2
    assert dockerfile.count('--workers \\"$GATEWAY_WORKERS\\"') == 2
    assert 'GATEWAY_EXTRA_FLAGS="--workers $GATEWAY_WORKERS"' in serve
    assert "export GATEWAY_WORKERS" in serve
