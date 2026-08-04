from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import deerflow.config.app_config as app_config_module
from deerflow.config.app_config import (
    DATABASE_RUNTIME_YAML_PATH_TOMBSTONES,
    AppConfig,
)
from deerflow.config.auth_config import AuthAppConfig

LEGACY_CONFIG_TOMBSTONES = (
    "agents_api",
    "authorization",
    "run_events",
    "stream_bridge",
    "extensions",
    "extensions_config",
    "mcp_config",
    "mcp_config_path",
    "legacy_run_store",
    "legacy_event_store",
    "recovery",
    "skill_evolution",
    "skill_scan",
)

LEGACY_CONFIG_PATH_TOMBSTONES = (
    "uploads.max_files",
    "uploads.max_file_size",
    "uploads.max_total_size",
    "uploads.auto_convert_documents",
    "scheduler.lease_seconds",
    "worker.default_max_attempts",
    "quotas.max_member_limit",
    "quotas.max_storage_bytes_limit",
    "quotas.max_concurrent_run_limit",
    "quotas.max_mcp_calls_daily_limit",
)


@pytest.mark.parametrize("value", ({}, None))
@pytest.mark.parametrize("key", LEGACY_CONFIG_TOMBSTONES)
def test_legacy_config_tombstones_are_rejected(key: str, value: object) -> None:
    with pytest.raises(ValidationError, match=f"LEGACY_CONFIG_REMOVED: {key}"):
        AppConfig.model_validate(
            {
                "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
                key: value,
            }
        )


@pytest.mark.parametrize("value", (False, None))
@pytest.mark.parametrize("field_path", LEGACY_CONFIG_PATH_TOMBSTONES)
def test_legacy_nested_config_tombstones_are_rejected(field_path: str, value: object) -> None:
    section, key = field_path.split(".", 1)

    with pytest.raises(ValidationError, match=rf"LEGACY_CONFIG_REMOVED: {field_path}"):
        AppConfig.model_validate(
            {
                "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
                section: {key: value},
            }
        )


def test_tombstones_are_exact_and_unknown_final_keys_remain_available() -> None:
    config = AppConfig.model_validate(
        {
            "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            "project_run_events_policy": {"retention_days": 30},
        }
    )

    assert config.model_extra == {
        "project_run_events_policy": {"retention_days": 30},
    }


def test_models_are_rejected_when_loaded_from_yaml(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            (
                "config_version: 35",
                "sandbox:",
                "  use: deerflow.sandbox.local:LocalSandboxProvider",
                "models:",
                "- name: legacy-model",
                "  use: pkg:Model",
                "  model: provider/model",
                "",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValidationError,
        match="LEGACY_CONFIG_REMOVED: models",
    ):
        AppConfig.from_file(str(config_path))


def _nested_path(field_path: str, value: object) -> dict[str, object]:
    result: dict[str, object] = {}
    current = result
    parts = field_path.split(".")
    for part in parts[:-1]:
        nested: dict[str, object] = {}
        current[part] = nested
        current = nested
    current[parts[-1]] = value
    return result


def _path_value(data: dict[str, object], field_path: str) -> object:
    current: object = data
    for part in field_path.split("."):
        assert isinstance(current, dict)
        current = current[part]
    return current


@pytest.mark.parametrize(
    "field_path",
    sorted(DATABASE_RUNTIME_YAML_PATH_TOMBSTONES),
)
def test_database_runtime_policy_leaves_are_yaml_only_tombstones(
    tmp_path: Path,
    field_path: str,
) -> None:
    baseline = AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}).model_dump(mode="python")
    leaf_value = _path_value(baseline, field_path)
    config_data: dict[str, object] = {
        "config_version": 35,
        "sandbox": {
            "use": "deerflow.sandbox.local:LocalSandboxProvider",
        },
    }
    config_data.update(_nested_path(field_path, leaf_value))
    config_path = tmp_path / "config.yaml"
    import yaml

    config_path.write_text(
        yaml.safe_dump(config_data, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(
        ValidationError,
        match=rf"LEGACY_CONFIG_REMOVED: {field_path}",
    ):
        AppConfig.from_file(str(config_path))

    # The same fields remain legal for trusted programmatic policy overlays.
    AppConfig.model_validate(
        {
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
            },
            **_nested_path(field_path, leaf_value),
        }
    )


def test_deployment_owned_runtime_siblings_remain_in_yaml(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """config_version: 35
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
title:
  prompt_template: 'custom {max_words} {user_msg} {assistant_msg}'
summarization:
  summary_prompt: 'custom summary: {messages}'
tool_output:
  storage_subdir: .custom-results
subagents:
  timeout_seconds: 123
""",
        encoding="utf-8",
    )

    config = AppConfig.from_file(str(config_path))

    assert config.title.prompt_template.startswith("custom")
    assert config.summarization.summary_prompt == "custom summary: {messages}"
    assert config.tool_output.storage_subdir == ".custom-results"
    assert config.subagents.timeout_seconds == 123


@pytest.mark.parametrize(
    "auth_config",
    (
        {"local": {"allow_registraton": False}},
        {"local_auth": {"allow_registration": False}},
        {"oidc": {"enable": True}},
        {
            "oidc": {
                "providers": {
                    "example": {
                        "display_name": "Example",
                        "issuer": "https://issuer.example",
                        "client_id": "client",
                        "unexpected_authority": True,
                    }
                }
            }
        },
    ),
)
def test_auth_security_config_rejects_unknown_keys(auth_config: dict) -> None:
    """Security-sensitive typos must never silently restore permissive defaults."""

    with pytest.raises(ValidationError, match="extra_forbidden"):
        AuthAppConfig.model_validate(auth_config)


def test_default_config_path_is_only_the_repository_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_repo = tmp_path / "repo"
    fake_backend = fake_repo / "backend"
    fake_backend.mkdir(parents=True)
    canonical = fake_repo / "config.yaml"
    backend_legacy = fake_backend / "config.yaml"
    canonical.write_text("sandbox:\n  use: test:Provider\n", encoding="utf-8")
    backend_legacy.write_text("sandbox:\n  use: legacy:Provider\n", encoding="utf-8")
    monkeypatch.delenv("DEER_FLOW_CONFIG_PATH", raising=False)
    monkeypatch.setattr(app_config_module, "REPO_ROOT", fake_repo)
    monkeypatch.chdir(fake_backend)

    assert AppConfig.resolve_config_path() == canonical


def test_explicit_config_environment_precedes_repository_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    canonical = fake_repo / "config.yaml"
    selected = tmp_path / "selected.yaml"
    canonical.write_text("sandbox:\n  use: root:Provider\n", encoding="utf-8")
    selected.write_text("sandbox:\n  use: selected:Provider\n", encoding="utf-8")
    monkeypatch.setattr(app_config_module, "REPO_ROOT", fake_repo)
    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(selected))

    assert AppConfig.resolve_config_path() == selected


def test_missing_canonical_config_fails_without_cwd_or_home_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_repo = tmp_path / "repo"
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    fake_repo.mkdir()
    cwd.mkdir()
    home.mkdir()
    (cwd / "config.yaml").write_text("sandbox: {}\n", encoding="utf-8")
    (home / "config.yaml").write_text("sandbox: {}\n", encoding="utf-8")
    monkeypatch.setattr(app_config_module, "REPO_ROOT", fake_repo)
    monkeypatch.delenv("DEER_FLOW_CONFIG_PATH", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)

    with pytest.raises(FileNotFoundError, match="repository root"):
        AppConfig.resolve_config_path()


@pytest.mark.parametrize(
    "module_name",
    (
        "deerflow.config.extensions_config",
        "deerflow.config.agents_api_config",
        "deerflow.config.run_events_config",
        "deerflow.config.stream_bridge_config",
        "deerflow.config.recovery_config",
        "deerflow.config.skill_evolution_config",
        "deerflow.config.skill_scan_config",
        "deerflow.runtime.events.store.memory",
        "deerflow.runtime.runs.store.memory",
        "deerflow.runtime.stream_bridge",
    ),
)
def test_removed_runtime_and_config_modules_are_not_importable(module_name: str) -> None:
    assert importlib.util.find_spec(module_name) is None


def test_docker_configuration_has_no_extensions_or_redis_stream_authority() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    compose_text = "\n".join((repo_root / relative).read_text(encoding="utf-8") for relative in ("docker/docker-compose.yaml", "docker/docker-compose-dev.yaml"))

    assert "DEER_FLOW_EXTENSIONS_CONFIG_PATH" not in compose_text
    assert "DEER_FLOW_STREAM_BRIDGE_REDIS_URL" not in compose_text
    assert "extensions_config.json" not in compose_text
    assert "redis://redis" not in compose_text


@pytest.mark.parametrize(
    "relative_path",
    ("scripts/serve.sh", "scripts/config-upgrade.sh"),
)
def test_local_launch_scripts_never_probe_backend_config(relative_path: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / relative_path).read_text(encoding="utf-8")

    assert "backend/config.yaml" not in source
    assert '"$REPO_ROOT/config.yaml"' in source


def test_serve_exports_valid_explicit_or_repo_root_config() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "scripts/serve.sh").read_text(encoding="utf-8")

    assert 'export DEER_FLOW_CONFIG_PATH="$REPO_ROOT/config.yaml"' in source
    assert "DEER_FLOW_CONFIG_PATH does not name a file" in source


def test_serve_does_not_upgrade_config_implicitly() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "scripts/serve.sh").read_text(encoding="utf-8")

    assert "config-upgrade.sh" not in source


def test_config_upgrade_uses_explicit_path_before_repo_root() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "scripts/config-upgrade.sh").read_text(encoding="utf-8")

    explicit_branch = source.index('if [ -n "${DEER_FLOW_CONFIG_PATH:-}" ]')
    repo_root_fallback = source.index('CONFIG="$REPO_ROOT/config.yaml"')
    assert explicit_branch < repo_root_fallback
    assert "DEER_FLOW_CONFIG_PATH does not name a file" in source


def test_config_upgrade_removes_retired_recovery_section() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "scripts/config-upgrade.sh").read_text(encoding="utf-8")

    assert "24: {" in source
    assert "'remove_keys': ['recovery']" in source
    assert "user.pop(key)" in source


def test_config_upgrade_removes_retired_nested_fields() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "scripts/config-upgrade.sh").read_text(encoding="utf-8")

    assert "25: {" in source
    assert "'remove_paths': [" in source
    assert "'remove_keys': ['skill_evolution', 'skill_scan']" in source
    for field_path in LEGACY_CONFIG_PATH_TOMBSTONES:
        assert repr(field_path) in source


def test_config_upgrade_removes_unsupported_authorization_section() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "scripts/config-upgrade.sh").read_text(encoding="utf-8")

    assert "32: {" in source
    assert "'remove_keys': ['authorization']" in source


def test_config_upgrade_removes_database_backed_model_section() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "scripts/config-upgrade.sh").read_text(encoding="utf-8")

    assert "33: {" in source
    assert "'remove_keys': ['models']" in source


def test_config_upgrade_removes_database_backed_runtime_policy_leaves() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "scripts/config-upgrade.sh").read_text(encoding="utf-8")

    assert "34: {" in source
    assert "DATABASE_RUNTIME_YAML_PATH_TOMBSTONES" in source
    assert "container.pop(child_key)" in source


def test_config_upgrade_replaces_retired_mcp_endpoint_allowlist_with_network_policy() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "scripts/config-upgrade.sh").read_text(encoding="utf-8")

    assert "35: {" in source
    assert "mcp_security.project_remote_allowed_endpoints" in source
    assert "retired_endpoints != []" in source
    assert "if 'mcp_security' not in user:" in source
    assert "if 'project_remote_allowed_networks' not in mcp_security:" in source
    assert "mcp_security['project_remote_allowed_networks'] = []" in source
    assert "v34 implicit project MCP deny-all" in source
    assert "No files were changed." in source
    assert "project_remote_allowed_networks:" in (repo_root / "config.example.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "mcp_security",
    (
        "",
        "mcp_security:\n  project_remote_allowed_endpoints: []\n  require_egress_proxy: false\n",
    ),
)
def test_config_upgrade_preserves_v34_implicit_or_explicit_mcp_deny_all(
    tmp_path: Path,
    mcp_security: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"config_version: 34\nsandbox:\n  use: test\n{mcp_security}",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(repo_root / "scripts/config-upgrade.sh")],
        cwd=repo_root,
        env={**os.environ, "DEER_FLOW_CONFIG_PATH": str(config_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    upgraded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert upgraded["config_version"] == 35
    assert upgraded["mcp_security"]["project_remote_allowed_networks"] == []
    assert "project_remote_allowed_endpoints" not in upgraded["mcp_security"]


def test_config_upgrade_refuses_to_widen_a_nonempty_v34_mcp_endpoint_list(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config_path = tmp_path / "config.yaml"
    original = """config_version: 34
sandbox:
  use: test
mcp_security:
  project_remote_allowed_endpoints:
    - http://localhost:8771/api/mcp
  require_egress_proxy: false
"""
    config_path.write_text(original, encoding="utf-8")

    result = subprocess.run(
        ["bash", str(repo_root / "scripts/config-upgrade.sh")],
        cwd=repo_root,
        env={**os.environ, "DEER_FLOW_CONFIG_PATH": str(config_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1
    assert "Cannot migrate nonempty mcp_security.project_remote_allowed_endpoints" in result.stdout
    assert "No files were changed." in result.stdout
    assert config_path.read_text(encoding="utf-8") == original
    assert not config_path.with_suffix(".yaml.bak").exists()


def test_example_and_deployment_config_do_not_define_models() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    sources = [
        (repo_root / "config.example.yaml").read_text(encoding="utf-8"),
        (repo_root / "deploy/helm/deer-flow/values.yaml").read_text(encoding="utf-8"),
    ]

    for source in sources:
        assert "\nmodels:" not in f"\n{source}"


def test_example_and_helm_define_only_deployment_owned_runtime_siblings() -> None:
    import yaml

    repo_root = Path(__file__).resolve().parents[2]
    example = yaml.safe_load((repo_root / "config.example.yaml").read_text(encoding="utf-8"))
    values = yaml.safe_load((repo_root / "deploy/helm/deer-flow/values.yaml").read_text(encoding="utf-8"))
    helm_config = yaml.safe_load(values["config"])

    for config in (example, helm_config):
        for field_path in DATABASE_RUNTIME_YAML_PATH_TOMBSTONES:
            current: object = config
            for part in field_path.split("."):
                if not isinstance(current, dict) or part not in current:
                    break
                current = current[part]
            else:
                raise AssertionError(f"database-owned policy leaf remains in YAML: {field_path}")
        assert config["tool_output"]["storage_subdir"] == ".tool-results"
        assert "summary_prompt" in config["summarization"]
