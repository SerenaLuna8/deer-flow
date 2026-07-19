from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

import deerflow.config.app_config as app_config_module
from deerflow.config.app_config import AppConfig

LEGACY_CONFIG_TOMBSTONES = (
    "agents_api",
    "run_events",
    "stream_bridge",
    "extensions",
    "extensions_config",
    "mcp_config",
    "mcp_config_path",
    "legacy_run_store",
    "legacy_event_store",
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


def test_config_upgrade_uses_explicit_path_before_repo_root() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "scripts/config-upgrade.sh").read_text(encoding="utf-8")

    explicit_branch = source.index('if [ -n "${DEER_FLOW_CONFIG_PATH:-}" ]')
    repo_root_fallback = source.index('CONFIG="$REPO_ROOT/config.yaml"')
    assert explicit_branch < repo_root_fallback
    assert "DEER_FLOW_CONFIG_PATH does not name a file" in source
