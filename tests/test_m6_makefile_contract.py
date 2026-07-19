from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_root_and_backend_makefiles_expose_all_m6_roles_and_operator_commands() -> None:
    root_make = (ROOT / "Makefile").read_text(encoding="utf-8")
    backend_make = (ROOT / "backend" / "Makefile").read_text(encoding="utf-8")
    for target in ("worker", "scheduler", "migrate-reliability", "reconcile-usage"):
        assert f"{target}:" in root_make
        assert f"{target}:" in backend_make
    assert "python -m app.worker.app" in backend_make
    assert "python -m app.scheduler.app" in backend_make
    assert "scripts/migrate_reliability.py" in backend_make
    assert "scripts/reconcile_usage.py" in backend_make


def test_local_launcher_owns_gateway_worker_and_optional_scheduler_processes() -> None:
    source = (ROOT / "scripts" / "serve.sh").read_text(encoding="utf-8")
    assert "app.worker.app" in source
    assert "app.scheduler.app" in source
    assert "STARTED_PIDS" in source
    assert "kill -0" in source
    assert source.index("app.worker.app") < source.index("Frontend")
    assert "scheduler.enabled" in source
    assert (
        "cd backend && exec env PYTHONPATH=. uv run python -m app.worker.app" in source
    )
    assert "startup_failure 1" in source
    assert "kill_process_tree" in source


def test_shell_launchers_parse_and_backend_role_exec_works_with_stub(
    tmp_path: Path,
) -> None:
    for name in ("serve.sh", "deploy.sh", "docker.sh"):
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "scripts" / name)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    binary = tmp_path / "bin"
    binary.mkdir()
    uv = binary / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$*"\n', encoding="utf-8")
    uv.chmod(0o755)
    result = subprocess.run(
        [
            "sh",
            "-c",
            "cd backend && exec env PYTHONPATH=. uv run python -m app.worker.app",
        ],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{binary}:{os.environ['PATH']}"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "run python -m app.worker.app"


def test_docker_compose_has_independent_worker_and_optional_scheduler_health() -> None:
    for name in ("docker-compose.yaml", "docker-compose-dev.yaml"):
        compose = yaml.safe_load((ROOT / "docker" / name).read_text(encoding="utf-8"))
        services = compose["services"]
        assert {"gateway", "worker", "scheduler"} <= set(services)
        worker = services["worker"]
        scheduler = services["scheduler"]
        assert "app.worker.app" in str(worker["command"])
        assert "app.scheduler.app" in str(scheduler["command"])
        assert "healthcheck" in worker
        assert "healthcheck" in scheduler
        assert "profiles" in scheduler
        assert not worker.get("ports")
        assert not scheduler.get("ports")
        assert services["gateway"].get("depends_on", {}).get("worker") is None


def test_docker_launchers_enable_scheduler_profile_only_from_config_flag() -> None:
    for name in ("deploy.sh", "docker.sh"):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "detect_scheduler_enabled" in source
        assert "--profile scheduler" in source
        assert re.search(
            r'^\s*services="frontend gateway worker nginx"$',
            source,
            re.MULTILINE,
        )
        assert 'services="$services scheduler"' in source
        assert not re.search(r'^\s*services="[^"]*\bredis\b', source, re.MULTILINE)
