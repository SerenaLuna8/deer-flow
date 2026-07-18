"""Gateway lifespan regression for the unified PostgreSQL runtime store."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.no_auto_user


_MINIMAL_CONFIG_YAML = """\
log_level: info
models:
  - name: fake-test-model
    display_name: Fake Test Model
    use: langchain_openai:ChatOpenAI
    model: gpt-4o-mini
    api_key: $OPENAI_API_KEY
    base_url: $OPENAI_API_BASE
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
agents_api:
  enabled: true
title:
  enabled: false
memory:
  enabled: false
database:
  url: $POSTGRES_RUNTIME_TEST_URL
run_events:
  backend: memory
"""


@pytest.fixture
def isolated_deer_flow_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_postgres_database_url: str) -> Path:
    home = tmp_path / "deer-flow-home"
    home.mkdir()
    monkeypatch.setenv("DEER_FLOW_HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-not-used")
    monkeypatch.setenv("OPENAI_API_BASE", "https://example.invalid")
    monkeypatch.setenv("POSTGRES_RUNTIME_TEST_URL", migrated_postgres_database_url)

    staged_config = tmp_path / "config.yaml"
    staged_config.write_text(_MINIMAL_CONFIG_YAML, encoding="utf-8")
    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(staged_config))

    staged_extensions_config = tmp_path / "extensions_config.json"
    staged_extensions_config.write_text('{"mcpServers": {}, "skills": {}}', encoding="utf-8")
    monkeypatch.setenv("DEER_FLOW_EXTENSIONS_CONFIG_PATH", str(staged_extensions_config))
    return home


def _reset_process_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear runtime singletons that depend on this test's temporary config.

    The Gateway app/lifespan path reads process-wide caches before wiring
    request-scoped dependencies. These E2E tests stage a temporary
    ``config.yaml``/``extensions_config.json`` and ``DEER_FLOW_HOME``, so the
    caches below must be reset before app creation:

    - app_config / extensions_config: parsed config file caches.
    - paths: ``DEER_FLOW_HOME``-derived filesystem paths.
    - persistence.engine: SQLAlchemy engine/session factory for PostgreSQL.
    - app.gateway.deps: cached local auth provider/repository.

    A shared public reset helper would be cleaner long-term; this test keeps
    the reset boundary explicit because the PR is focused on runtime lifecycle
    coverage rather than config-cache API cleanup.
    """

    from app.gateway import deps as deps_module
    from deerflow.config import app_config as app_config_module
    from deerflow.config import extensions_config as extensions_config_module
    from deerflow.config import paths as paths_module
    from deerflow.persistence import engine as engine_module

    for module, attr, value in (
        (app_config_module, "_app_config", None),
        (app_config_module, "_app_config_path", None),
        (app_config_module, "_app_config_mtime", None),
        (app_config_module, "_app_config_is_custom", False),
        (extensions_config_module, "_extensions_config", None),
        (paths_module, "_paths_singleton", None),
        (paths_module, "_paths", None),
        (engine_module, "_engine", None),
        (engine_module, "_session_factory", None),
        (deps_module, "_cached_local_provider", None),
        (deps_module, "_cached_repo", None),
    ):
        monkeypatch.setattr(module, attr, value, raising=False)


def _preserve_process_config_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore config singletons mutated as a side effect of AppConfig loading.

    ``AppConfig.from_file()`` calls ``_apply_singleton_configs()``, which pushes
    nested config sections into module-level caches used by middlewares, tool
    selection, and runtime providers. Snapshotting those attributes with
    ``monkeypatch`` lets pytest restore the pre-test values during teardown, so
    loading the isolated test config does not leak into later tests.
    """

    from deerflow.config import (
        acp_config,
        agents_api_config,
        guardrails_config,
        memory_config,
        stream_bridge_config,
        subagents_config,
        summarization_config,
        title_config,
        tool_search_config,
    )

    for module, attr in (
        (title_config, "_title_config"),
        (summarization_config, "_summarization_config"),
        (memory_config, "_memory_config"),
        (agents_api_config, "_agents_api_config"),
        (subagents_config, "_subagents_config"),
        (tool_search_config, "_tool_search_config"),
        (guardrails_config, "_guardrails_config"),
        (stream_bridge_config, "_stream_bridge_config"),
        (acp_config, "_acp_agents"),
    ):
        monkeypatch.setattr(module, attr, getattr(module, attr), raising=False)


@pytest.fixture
def isolated_app(isolated_deer_flow_home: Path, monkeypatch: pytest.MonkeyPatch):
    _preserve_process_config_singletons(monkeypatch)
    _reset_process_singletons(monkeypatch)

    from deerflow.config import app_config as app_config_module

    app_config_module.get_app_config()

    from app.gateway.app import create_app

    return create_app()


def test_lifespan_uses_postgres_store_from_database_config(isolated_app):
    """Gateway startup must bind LangGraph Store to the unified database backend."""
    from langgraph.store.postgres.aio import AsyncPostgresStore
    from starlette.testclient import TestClient

    with TestClient(isolated_app):
        assert isinstance(isolated_app.state.store, AsyncPostgresStore)
