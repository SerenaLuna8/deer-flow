"""Repository contract for the supported application and Sandbox runtimes."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_application_container_deployment_is_not_shipped() -> None:
    forbidden = (
        ".dockerignore",
        "backend/Dockerfile",
        "frontend/Dockerfile",
        "docker",
        "scripts/docker.sh",
        "scripts/deploy.sh",
    )

    assert [path for path in forbidden if (_ROOT / path).exists()] == []


def test_local_nginx_and_optional_sandbox_assets_are_retained() -> None:
    required = (
        "nginx/nginx.conf",
        "sandbox/provisioner/Dockerfile",
        "sandbox/provisioner/README.md",
        "sandbox/provisioner/app.py",
        "scripts/setup-sandbox.sh",
        "scripts/cleanup-containers.sh",
    )

    assert [path for path in required if not (_ROOT / path).is_file()] == []


def test_makefile_exposes_local_runtime_and_sandbox_setup_only() -> None:
    source = (_ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([A-Za-z0-9_.-]+):", source, flags=re.MULTILINE))
    removed_targets = {
        "docker-init",
        "docker-start",
        "docker-stop",
        "docker-logs",
        "docker-logs-frontend",
        "docker-logs-gateway",
        "up",
        "down",
    }

    assert "setup-sandbox" in targets
    assert targets.isdisjoint(removed_targets)


def test_local_runtime_scripts_use_the_owned_nginx_config() -> None:
    for relative_path in ("scripts/serve.sh", "scripts/nginx.sh"):
        source = (_ROOT / relative_path).read_text(encoding="utf-8")
        assert "nginx/nginx.conf" in source
        assert "docker/nginx" not in source
