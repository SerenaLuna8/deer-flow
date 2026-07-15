"""Real HTTP end-to-end verification for issue #2862's setup_agent path.

This test drives the **entire** FastAPI gateway through ``starlette.testclient.TestClient``:

  starlette.testclient.TestClient (real ASGI stack)
    -> AuthMiddleware (real cookie parsing, real JWT decode)
    -> /api/v1/auth/register endpoint (real password hash + PostgreSQL write)
    -> /api/threads/{id}/runs/stream endpoint (real start_run config-assembly)
    -> background asyncio.create_task(run_agent) (real worker, real Runtime)
    -> langchain.agents.create_agent graph (real, with fake LLM)
    -> ToolNode dispatch (real)
    -> setup_agent tool (real file I/O)

The only mock is the LLM (no API key needed). Every layer that participates
in ``user_id`` propagation — auth, ContextVar, ``inject_authenticated_user_context``,
``worker._build_runtime_context``, ``Runtime.merge`` — is the real production
code path. If the chain is broken at any layer, this test fails.

This is what "真实验证" looks like for a server that lives behind authentication:
register a user, log in (cookie), POST to /runs/stream, wait for the run to
finish, then read the filesystem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _agent_e2e_helpers import FakeToolCallingModel, build_single_tool_call_model


def _build_fake_create_chat_model(agent_name: str):
    """Return a callable matching the real ``create_chat_model`` signature.

    Whenever the lead agent constructs a chat model during the bootstrap flow,
    we hand it a fake that emits a single setup_agent tool_call on its first
    turn, then a benign final answer on its second turn.
    """

    def fake_create_chat_model(*args: Any, **kwargs: Any) -> FakeToolCallingModel:
        return build_single_tool_call_model(
            tool_name="setup_agent",
            tool_args={
                "soul": f"# Real HTTP E2E SOUL for {agent_name}",
                "description": "real-http-e2e agent",
            },
            tool_call_id="call_real_http_1",
            final_text=f"Agent {agent_name} created via real HTTP e2e.",
        )

    return fake_create_chat_model


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


def _drain_stream(response, *, timeout: float = 30.0, max_bytes: int = 4 * 1024 * 1024) -> str:
    """Consume an SSE response body until the run terminates and return the text.

    Bounded to keep the test fail-fast:
      - Stops as soon as an ``event: end`` SSE frame is observed (the gateway
        sends this when the background run finishes — see ``services.format_sse``
        and ``StreamBridge.publish_end``).
      - Stops at ``timeout`` seconds wall-clock so a stuck run / runaway heartbeat
        loop surfaces a real failure instead of hanging pytest.
      - Stops at ``max_bytes`` so a runaway producer can't OOM the test process.
    """
    import time as _time

    deadline = _time.monotonic() + timeout
    body = b""
    for chunk in response.iter_bytes():
        body += chunk
        if b"event: end" in body:
            break
        if len(body) >= max_bytes:
            break
        if _time.monotonic() >= deadline:
            break
    return body.decode("utf-8", errors="replace")


def _wait_for_file(path: Path, *, timeout: float = 10.0) -> bool:
    """Block until *path* exists or *timeout* elapses.

    The run completes inside ``asyncio.create_task`` after start_run returns,
    so the test must wait for the background task to flush its writes.
    """
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if path.exists():
            return True
        _time.sleep(0.05)
    return False


@pytest.mark.no_auto_user
def test_real_http_legacy_setup_agent_entry_is_closed_after_private_cutover(
    isolated_app: Any,
    isolated_deer_flow_home: Path,
):
    """M4 asset authoring must not be reachable through the legacy run API."""
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
        assert created.status_code == 409, created.text
        assert created.json()["detail"]["code"] == "PRIVATE_WORK_CUTOVER"
        assert not list(isolated_deer_flow_home.rglob("SOUL.md"))
