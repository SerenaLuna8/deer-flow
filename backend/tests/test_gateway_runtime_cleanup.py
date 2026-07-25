"""Positive deployment contracts for the Gateway topology."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_docker_dev_mounts_mutable_configs_through_project_directory() -> None:
    compose = _read("docker/docker-compose-dev.yaml")

    assert re.search(r"^\s*-\s*\.\./:/app/project(?:\:\S+)?\s*$", compose, re.M)
    assert not re.search(r"^\s*-\s*[^\n#]*config\.yaml\s*:\s*[^\n#]*$", compose, re.M)
    assert not re.search(r"^\s*-\s*[^\n#]*extensions_config\.json\s*:\s*[^\n#]*$", compose, re.M)
    assert "DEER_FLOW_CONFIG_PATH=/app/project/config.yaml" in compose


def test_local_dev_gateway_reload_uses_absolute_runtime_directories() -> None:
    serve_sh = _read("scripts/serve.sh")

    assert 'export DEER_FLOW_PROJECT_ROOT="$REPO_ROOT"' in serve_sh
    assert 'BACKEND_RUNTIME_HOME="$REPO_ROOT/backend/.deer-flow"' in serve_sh
    assert 'export DEER_FLOW_HOME="$BACKEND_RUNTIME_HOME"' in serve_sh
    assert 'mkdir -p "$DEER_FLOW_HOME" "$BACKEND_RUNTIME_HOME" "$REPO_ROOT/backend/sandbox"' in serve_sh
    assert "--reload-exclude='$DEER_FLOW_HOME'" in serve_sh
    assert "--reload-exclude='$BACKEND_RUNTIME_HOME'" in serve_sh


def test_backend_container_exposes_gateway_port() -> None:
    dockerfile = _read("backend/Dockerfile")

    assert re.search(r"^EXPOSE\s+8001\b", dockerfile, re.M)


def test_nginx_defers_cors_to_gateway_allowlist() -> None:
    for path in ("docker/nginx/nginx.local.conf", "docker/nginx/nginx.conf"):
        content = _read(path)

        assert "Access-Control-Allow-Origin" not in content
        assert "Access-Control-Allow-Methods" not in content
        assert "Access-Control-Allow-Headers" not in content
        assert "Access-Control-Allow-Credentials" not in content
        assert "proxy_hide_header 'Access-Control-Allow-" not in content
        assert "if ($request_method = 'OPTIONS')" not in content


def test_gateway_cors_configuration_uses_gateway_allowlist() -> None:
    gateway_config = _read("backend/app/gateway/config.py")
    gateway_app = _read("backend/app/gateway/app.py")
    csrf_middleware = _read("backend/app/gateway/csrf_middleware.py")

    assert not re.search(r"(?<!GATEWAY_)[\"']CORS_ORIGINS[\"']", gateway_config)
    assert "cors_origins" not in gateway_config
    assert "get_configured_cors_origins" in gateway_app
    assert "GATEWAY_CORS_ORIGINS" in csrf_middleware
