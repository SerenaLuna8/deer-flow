"""Real HTTP regression for removal of the global setup-agent run entry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def isolated_deer_flow_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_postgres_database_url: str):
    """Stand up an isolated DeerFlow data root + config under tmp_path.

    - Sets ``DEER_FLOW_HOME`` so paths land under tmp_path, not the real
      ``.deer-flow`` directory.
    - Stages a copy of the project's ``config.yaml`` (or ``config.example.yaml``
      on a fresh CI checkout where ``config.yaml`` is gitignored) and pins
      ``DEER_FLOW_CONFIG_PATH`` to it, so lifespan boot doesn't depend on the
      developer's local config layout.
    - Sets a placeholder OPENAI_API_KEY because the config has
      ``$OPENAI_API_KEY`` that gets resolved at parse time; the LLM itself is
      mocked, so any non-empty value works.
    """
    home = tmp_path / "deer-flow-home"
    home.mkdir()
    monkeypatch.setenv("DEER_FLOW_HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-not-used-because-llm-is-mocked")
    monkeypatch.setenv("OPENAI_API_BASE", "https://example.invalid")
    monkeypatch.setenv("POSTGRES_RUNTIME_TEST_URL", migrated_postgres_database_url)

    # Hermetic config: do not depend on whether the dev machine has a real
    # ``config.yaml`` at the repo root. CI's ``actions/checkout`` only ships
    # ``config.example.yaml`` (and its ``models:`` list is commented out, so
    # AppConfig validation would reject it). Write a minimal, self-sufficient
    # config to tmp_path and pin ``DEER_FLOW_CONFIG_PATH`` to it.
    staged_config = tmp_path / "config.yaml"
    staged_config.write_text(_MINIMAL_CONFIG_YAML, encoding="utf-8")
    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(staged_config))

    return home


# Minimal config that satisfies AppConfig + LeadAgent's _resolve_model_name.
# The model `use` path must resolve to a real class for config parsing to
# succeed; the test patches ``create_chat_model`` on the lead agent module,
# so the model is never actually instantiated. SandboxConfig.use is required
# at schema level; LocalSandboxProvider is the only sandbox that runs without
# Docker.
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
database:
  url: $POSTGRES_RUNTIME_TEST_URL
"""


def _reset_process_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset every process-wide cache that would survive across tests.

    This fixture stands up a full FastAPI app + PostgreSQL DB + LangGraph runtime
    inside ``tmp_path``. To get true per-test isolation we have to invalidate
    a handful of module-level caches that production normally never resets,
    so they pick up our test-only ``DEER_FLOW_HOME`` and database URL:

    - ``deerflow.config.app_config`` caches the parsed ``config.yaml``.
    - ``deerflow.config.paths`` caches the ``Paths`` singleton derived from
      ``DEER_FLOW_HOME`` at first access.
    - ``deerflow.persistence.engine`` caches the SQLAlchemy engine and
      session factory after the first call to ``init_engine_from_config``.

    ``raising=False`` keeps the fixture resilient if upstream renames or
    drops one of these attributes — the test will simply skip that reset
    instead of failing with a confusing AttributeError, and the next test
    to call ``get_app_config()``/``get_paths()`` will surface the real
    incompatibility loudly.
    """
    from app.gateway import deps as deps_module
    from deerflow.config import app_config as app_config_module
    from deerflow.config import paths as paths_module
    from deerflow.persistence import engine as engine_module

    for module, attr in (
        (app_config_module, "_app_config"),
        (app_config_module, "_app_config_path"),
        (app_config_module, "_app_config_mtime"),
        (paths_module, "_paths_singleton"),
        (engine_module, "_engine"),
        (engine_module, "_session_factory"),
        (deps_module, "_cached_local_provider"),
        (deps_module, "_cached_repo"),
    ):
        monkeypatch.setattr(module, attr, None, raising=False)


@pytest.fixture
def isolated_app(isolated_deer_flow_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a fresh FastAPI app inside a clean DEER_FLOW_HOME.

    Each test gets its own PostgreSQL DB and checkpoint store,
    with no cross-test contamination.
    """
    _reset_process_singletons(monkeypatch)

    # Re-resolve the config from the test-only DEER_FLOW_HOME so lifespan uses
    # the isolated PostgreSQL URL supplied through the environment placeholder.
    from deerflow.config import app_config as app_config_module

    app_config_module.get_app_config()

    from app.gateway.app import create_app

    return create_app()


@pytest.mark.no_auto_user
def test_real_http_global_setup_agent_entry_is_absent(
    isolated_app: Any,
    isolated_deer_flow_home: Path,
):
    """Deleted global Thread routes are ordinary missing routes after M7."""
    from starlette.testclient import TestClient

    with TestClient(isolated_app) as client:
        register = client.post(
            "/api/v1/auth/register",
            json={"email": "e2e-user@example.com", "password": "very-strong-password-123"},
        )
        assert register.status_code == 201, register.text
        assert client.cookies.get("access_token"), "register endpoint must set session cookie"
        csrf_token = client.cookies.get("csrf_token")
        assert csrf_token, "register endpoint must set csrf_token cookie"

        import uuid as _uuid

        thread_id = str(_uuid.uuid4())
        created = client.post(
            "/api/threads",
            json={"thread_id": thread_id, "metadata": {}},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert created.status_code == 404, created.text
        assert not list(isolated_deer_flow_home.rglob("SOUL.md"))
