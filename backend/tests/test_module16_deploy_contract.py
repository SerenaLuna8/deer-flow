from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import yaml

from deerflow.config.app_config import AppConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "deploy" / "helm" / "deer-flow"


def _render_chart(*arguments: str) -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "deer-flow",
            str(CHART),
            "--include-crds",
            "--set-string",
            "postgresql.external.databaseUrl=postgresql://postgres:test@db:5432/deerflow",
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [document for document in yaml.safe_load_all(result.stdout) if isinstance(document, dict)]


def _component(document: dict) -> str | None:
    return document.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")


def _deployment(documents: list[dict], component: str) -> dict:
    matches = [document for document in documents if document.get("kind") == "Deployment" and _component(document) == component]
    assert len(matches) == 1
    return matches[0]


def _container(deployment: dict, name: str) -> dict:
    matches = [container for container in deployment["spec"]["template"]["spec"]["containers"] if container["name"] == name]
    assert len(matches) == 1
    return matches[0]


def _env_names(container: dict) -> set[str]:
    return {item["name"] for item in container.get("env", [])}


def test_chart_refuses_an_uninitialized_database_default() -> None:
    result = subprocess.run(
        ["helm", "template", "deer-flow", str(CHART), "--include-crds"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "database" in result.stderr.lower() or "postgres" in result.stderr.lower()


def test_chart_renders_worker_only_execution_topology() -> None:
    documents = _render_chart()
    gateway = _container(_deployment(documents, "gateway"), "gateway")
    worker = _container(_deployment(documents, "worker"), "worker")

    assert "app.gateway.app:app" in " ".join(gateway.get("args", []))
    assert "app.worker.app" not in " ".join(gateway.get("args", []))
    assert "app.worker.app" in " ".join(worker.get("args", []))
    assert not [document for document in documents if document.get("kind") == "Deployment" and _component(document) == "scheduler"]

    enabled_documents = _render_chart("--set", "scheduler.enabled=true")
    scheduler = _container(_deployment(enabled_documents, "scheduler"), "scheduler")
    assert "app.scheduler.app" in " ".join(scheduler.get("args", []))
    assert {
        "AUTH_JWT_SECRET",
        "DATABASE_URL",
        "DEER_FLOW_AUDIT_ACTIVE_KEY_ID",
        "DEER_FLOW_AUDIT_KEYRING_JSON",
        "PROVISIONER_API_KEY",
    } <= _env_names(scheduler)


def test_chart_wires_process_secrets_and_secure_provisioner_defaults() -> None:
    documents = _render_chart()
    secret = next(document for document in documents if document.get("kind") == "Secret" and document.get("metadata", {}).get("name") == "deer-flow-deer-flow-app")
    assert {
        "AUTH_JWT_SECRET",
        "BETTER_AUTH_SECRET",
        "DEER_FLOW_AUDIT_ACTIVE_KEY_ID",
        "DEER_FLOW_AUDIT_KEYRING_JSON",
        "DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID",
        "DEER_FLOW_CREDENTIAL_KEYRING_JSON",
        "DEER_FLOW_INTERNAL_AUTH_TOKEN",
        "DEER_FLOW_PROXY_AUTH_TOKEN",
        "PROVISIONER_API_KEY",
    } <= set(secret["stringData"])
    secret_template = (CHART / "templates" / "secret-app.yaml").read_text(encoding="utf-8")
    assert 'lookup "v1" "Secret"' in secret_template

    audit_keyring = json.loads(secret["stringData"]["DEER_FLOW_AUDIT_KEYRING_JSON"])
    credential_keyring = json.loads(secret["stringData"]["DEER_FLOW_CREDENTIAL_KEYRING_JSON"])
    assert set(audit_keyring) == {secret["stringData"]["DEER_FLOW_AUDIT_ACTIVE_KEY_ID"]}
    assert set(credential_keyring) == {secret["stringData"]["DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID"]}
    audit_key = base64.b64decode(next(iter(audit_keyring.values())), validate=True)
    credential_key = base64.b64decode(next(iter(credential_keyring.values())), validate=True)
    assert len(audit_key) == len(credential_key) == 32
    assert audit_key != credential_key

    for component in ("gateway", "worker"):
        container = _container(_deployment(documents, component), component)
        assert {
            "AUTH_JWT_SECRET",
            "DATABASE_URL",
            "DEER_FLOW_AUDIT_ACTIVE_KEY_ID",
            "DEER_FLOW_AUDIT_KEYRING_JSON",
            "PROVISIONER_API_KEY",
        } <= _env_names(container)

    provisioner = _container(_deployment(documents, "provisioner"), "provisioner")
    provisioner_env = {item["name"]: item for item in provisioner["env"]}
    assert provisioner_env["SANDBOX_SERVICE_TYPE"]["value"] == "ClusterIP"
    assert "PROVISIONER_API_KEY" in provisioner_env
    assert "NODE_HOST" not in provisioner_env

    nodeport_documents = _render_chart("--set", "provisioner.sandboxServiceType=NodePort")
    nodeport = _container(_deployment(nodeport_documents, "provisioner"), "provisioner")
    nodeport_env = {item["name"]: item for item in nodeport["env"]}
    assert nodeport_env["SANDBOX_SERVICE_TYPE"]["value"] == "NodePort"
    assert "NODE_HOST" in nodeport_env


def test_provisioner_rbac_matches_the_control_api_calls() -> None:
    documents = _render_chart()
    role = next(document for document in documents if document.get("kind") == "Role" and _component(document) == "provisioner")
    pod_service_rule = next(rule for rule in role["rules"] if set(rule["resources"]) == {"pods", "services"})
    assert {"get", "list", "create", "delete"} <= set(pod_service_rule["verbs"])
    assert not {"watch", "update", "patch"} & set(pod_service_rule["verbs"])


def test_chart_config_version_and_schema_match_current_example(
    monkeypatch,
) -> None:
    example = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    chart_config = yaml.safe_load(values["config"])

    assert chart_config["config_version"] == example["config_version"]
    expected_mcp_security = {
        "project_remote_allowed_networks": [
            "127.0.0.0/8",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "::1/128",
            "fc00::/7",
        ],
        "require_egress_proxy": False,
        "egress_proxy_url": None,
        "discovery_timeout_seconds": 15,
        "tool_call_timeout_seconds": 60,
    }
    assert example["mcp_security"] == expected_mcp_security
    assert chart_config["mcp_security"] == expected_mcp_security
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:secret@db.invalid/deerflow")
    monkeypatch.setenv("PROVISIONER_API_KEY", "test-provisioner-key")
    AppConfig.model_validate(AppConfig.resolve_env_variables(chart_config))


def test_nginx_uses_conditional_upgrade_and_large_project_api_body() -> None:
    paths = (
        REPO_ROOT / "docker" / "nginx" / "nginx.local.conf",
        REPO_ROOT / "docker" / "nginx" / "nginx.conf",
        CHART / "templates" / "configmap-nginx.yaml",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "map $http_upgrade $connection_upgrade" in source
        assert "''      '';" in source
        assert "proxy_set_header Connection $connection_upgrade;" in source
        assert "proxy_set_header Connection 'upgrade';" not in source
        assert "client_max_body_size 100M;" in source
        assert "proxy_request_buffering off;" in source


def test_release_container_does_not_request_removed_postgres_extra() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "container.yaml").read_text(encoding="utf-8")

    assert "UV_EXTRAS=postgres" not in workflow


def test_release_and_chart_workflows_cover_the_dev_runtime_contract() -> None:
    release_workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "project-saas-release-gates.yml").read_text(encoding="utf-8"))
    assert "dev" in release_workflow[True]["push"]["branches"]

    chart_workflow = (REPO_ROOT / ".github" / "workflows" / "chart.yaml").read_text(encoding="utf-8")
    for script in (
        "scripts/check_config_version.sh",
        "scripts/check_chart_sandbox_service.sh",
        "scripts/check_chart_runtime_topology.sh",
    ):
        assert (REPO_ROOT / script).is_file()
        assert script in chart_workflow


def test_sensitive_runtime_overlays_mount_only_the_worker() -> None:
    for filename in ("docker-compose.dood.yaml", "docker-compose.cli-auth.yaml"):
        document = yaml.safe_load((REPO_ROOT / "docker" / filename).read_text(encoding="utf-8"))
        assert "worker" in document["services"]
        assert "gateway" not in document["services"]


def test_compose_explicitly_selects_nodeport_and_wires_provisioner_auth() -> None:
    for filename in ("docker-compose.yaml", "docker-compose-dev.yaml"):
        document = yaml.safe_load((REPO_ROOT / "docker" / filename).read_text(encoding="utf-8"))
        services = document["services"]
        provisioner_environment = services["provisioner"]["environment"]

        assert any(item.startswith("SANDBOX_SERVICE_TYPE=NodePort") for item in provisioner_environment)
        assert any(item.startswith("PROVISIONER_API_KEY=") for item in provisioner_environment)
        for component in ("gateway", "worker", "scheduler"):
            assert any(item.startswith("PROVISIONER_API_KEY=") for item in services[component]["environment"])


def test_fresh_checkout_tracks_local_runtime_dependencies() -> None:
    required = (
        "docker/nginx/nginx.local.conf",
        "docker/nginx/nginx.conf",
        "docker/provisioner/app.py",
        "docker/docker-compose.yaml",
        "docker/docker-compose-dev.yaml",
    )
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", *required],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    assert result.returncode == 0, result.stderr
