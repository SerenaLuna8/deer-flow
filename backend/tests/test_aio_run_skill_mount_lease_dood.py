"""Real Compose DooD P-03 read-only mount and cross-Worker reconciliation."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE_CONFIG = _REPOSITORY_ROOT / "config.example.yaml"
_PRODUCTION_COMPOSE = _REPOSITORY_ROOT / "docker" / "docker-compose.yaml"
_DEV_COMPOSE = _REPOSITORY_ROOT / "docker" / "docker-compose-dev.yaml"
_DOOD_COMPOSE = _REPOSITORY_ROOT / "docker" / "docker-compose.dood.yaml"
_OWNER_LABEL = "io.actweave.run-mount-owner"


def test_compose_dood_maps_the_same_dedicated_root_into_worker_and_daemon_views() -> None:
    example = yaml.safe_load(_EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    production = yaml.safe_load(
        _PRODUCTION_COMPOSE.read_text(encoding="utf-8"),
    )
    dev = yaml.safe_load(_DEV_COMPOSE.read_text(encoding="utf-8"))
    dood = yaml.safe_load(_DOOD_COMPOSE.read_text(encoding="utf-8"))
    production_worker = production["services"]["worker"]
    worker = dev["services"]["worker"]

    assert example["sandbox"]["compose_dood_p03_v1_verified"] is False
    assert "ACT_WEAVE_HOME=/app/backend/.deer-flow" in production_worker["environment"]
    assert "ACT_WEAVE_HOST_BASE_DIR=${ACT_WEAVE_HOME}" in production_worker["environment"]
    assert "${ACT_WEAVE_HOME}:/app/backend/.deer-flow" in production_worker["volumes"]
    assert "ACT_WEAVE_HOME=/app/backend/.deer-flow" in worker["environment"]
    assert "ACT_WEAVE_HOST_BASE_DIR=${ACT_WEAVE_ROOT}/backend/.deer-flow" in worker["environment"]
    assert "../backend/:/app/backend/" in worker["volumes"]
    assert set(dood["services"]) == {"worker"}
    assert dood["services"]["worker"]["volumes"] == [
        "${ACT_WEAVE_DOCKER_SOCKET:-/var/run/docker.sock}:/var/run/docker.sock",
    ]


def _require_integration_config() -> Path:
    raw = os.environ.get("ACT_WEAVE_CONFIG_PATH", "")
    if not raw:
        pytest.fail("ACT_WEAVE_CONFIG_PATH is required")
    config = Path(raw).resolve()
    if not config.is_file():
        pytest.fail("Compose DooD test config is unavailable")
    try:
        config.relative_to(_REPOSITORY_ROOT)
    except ValueError:
        pytest.fail("Compose DooD test config must be inside the checkout")
    return config


def _compose_probe_command(
    *,
    project_name: str,
    config: Path,
    owner_id: uuid.UUID,
    operation: str,
) -> tuple[list[str], dict[str, str]]:
    relative_config = config.relative_to(_REPOSITORY_ROOT).as_posix()
    command = [
        "docker",
        "compose",
        "-p",
        project_name,
        "-f",
        str(_DEV_COMPOSE),
        "-f",
        str(_DOOD_COMPOSE),
        "run",
        "--rm",
        "--no-deps",
        "-e",
        f"ACT_WEAVE_CONFIG_PATH=/app/project/{relative_config}",
        "-e",
        f"ACTWEAVE_P03_OWNER_ID={owner_id.hex}",
        "worker",
        "sh",
        "-lc",
        (f"cd /app/backend && PYTHONPATH=. uv run python tests/support/p03_compose_dood_probe.py {operation}"),
    ]
    environment = dict(os.environ)
    environment["ACT_WEAVE_ROOT"] = str(_REPOSITORY_ROOT)
    return command, environment


def _run_probe(
    *,
    project_name: str,
    config: Path,
    owner_id: uuid.UUID,
    operation: str,
) -> None:
    command, environment = _compose_probe_command(
        project_name=project_name,
        config=config,
        owner_id=owner_id,
        operation=operation,
    )
    try:
        result = subprocess.run(
            command,
            cwd=_REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail(f"P-03 Compose probe is unavailable during {operation}")
    if result.returncode != 0:
        pytest.fail(f"P-03 Compose probe failed during {operation}")


def _force_remove_exact_owner(owner_id: uuid.UUID) -> None:
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label={_OWNER_LABEL}={owner_id.hex}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if result.returncode != 0:
        return
    container_ids = [value.strip() for value in result.stdout.splitlines() if value.strip()]
    if container_ids:
        try:
            subprocess.run(
                ["docker", "rm", "-f", *container_ids],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return


def _remove_compose_probe_project(project_name: str) -> None:
    try:
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                project_name,
                "-f",
                str(_DEV_COMPOSE),
                "-f",
                str(_DOOD_COMPOSE),
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            cwd=_REPOSITORY_ROOT,
            env={**os.environ, "ACT_WEAVE_ROOT": str(_REPOSITORY_ROOT)},
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


@pytest.mark.provider_integration
@pytest.mark.p03_compose_dood
def test_compose_dood_dual_view_guest_probe_and_cross_worker_reconcile() -> None:
    if os.environ.get("ACTWEAVE_REQUIRE_PROVIDER_INTEGRATION") != "1":
        pytest.skip("requires the disposable Compose DooD integration profile")
    config = _require_integration_config()
    owner_id = uuid.uuid4()
    project_name = f"actweave-p03-{owner_id.hex[:12]}"

    try:
        _run_probe(
            project_name=project_name,
            config=config,
            owner_id=owner_id,
            operation="acquire",
        )
        _run_probe(
            project_name=project_name,
            config=config,
            owner_id=owner_id,
            operation="reconcile",
        )
    finally:
        try:
            _run_probe(
                project_name=project_name,
                config=config,
                owner_id=owner_id,
                operation="cleanup",
            )
        except BaseException:
            _force_remove_exact_owner(owner_id)
        finally:
            _remove_compose_probe_project(project_name)
