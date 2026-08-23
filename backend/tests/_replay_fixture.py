"""Shared process helpers for the deterministic replay browser test."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

_REPLAY_ADMIN_ID = uuid.UUID("5fb66f7d-5655-54df-a7da-66066c114f17")


def replay_gateway_admin_user() -> SimpleNamespace:
    """Return the persisted admin identity used by replay Gateway requests."""

    return SimpleNamespace(
        id=_REPLAY_ADMIN_ID,
        email="replay-runtime-admin@example.invalid",
        username="replay-runtime-admin",
        password_hash=None,
        system_role="system_admin",
        needs_setup=False,
        token_version=0,
        oauth_provider=None,
    )


async def replay_gateway_user(request: Request):
    """Map only auth-disabled replay requests to the persisted admin.

    Session and internal requests must retain the identity already resolved by
    ``AuthMiddleware``.  Replacing those identities would split middleware,
    ``/auth/me``, and FastAPI dependency authority inside one request.
    """

    from app.gateway.auth_disabled import (
        AUTH_SOURCE_AUTH_DISABLED,
        AUTH_SOURCE_INTERNAL,
        AUTH_SOURCE_SESSION,
    )

    state = getattr(request, "state", None)
    source = getattr(state, "auth_source", None)
    state_user = getattr(state, "user", None)
    if source == AUTH_SOURCE_AUTH_DISABLED:
        return replay_gateway_admin_user()
    if state_user is not None and source in {
        AUTH_SOURCE_SESSION,
        AUTH_SOURCE_INTERNAL,
    }:
        return state_user

    from app.gateway.deps import get_current_user_from_request

    return await get_current_user_from_request(request)


def build_config_yaml(*, home: Path) -> str:
    """Build the current, non-database process config for record/replay.

    Everything that shapes the system prompt is pinned so record, replay, and CI
    produce byte-identical prompts regardless of the machine:
    - sandbox / tool_groups / tools — fixed here
    - skills — pointed at an empty ``<home>/skills`` so filesystem skills (incl.
      gitignored custom skills present only on a dev box) never leak into the
      prompt. Project MCP assets are absent from the admitted test Run.
    - model catalog / memory / summarization — PostgreSQL-owned and prepared by
      ``prepare_replay_runtime_catalog`` for replay.
    """
    return f"""\
log_level: warning
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
skills:
  path: {home / "skills"}
  container_path: /mnt/skills
tool_groups:
  - name: file:read
  - name: file:write
tools:
  - name: ls
    group: file:read
    use: deerflow.sandbox.tools:ls_tool
  - name: read_file
    group: file:read
    use: deerflow.sandbox.tools:read_file_tool
  - name: write_file
    group: file:write
    use: deerflow.sandbox.tools:write_file_tool
database:
  url: $DATABASE_URL
"""


def _validated_replay_database_url(
    database_url: str | None,
    *,
    required_prefix: str = "deerflow_test_",
) -> str:
    """Resolve one disposable PostgreSQL URL before any replay mutation."""
    from sqlalchemy.engine import make_url

    resolved_url = database_url or os.environ.get("DATABASE_URL")
    if not resolved_url:
        raise RuntimeError("DATABASE_URL is required for replay database setup")
    try:
        parsed = make_url(resolved_url)
    except Exception:
        raise RuntimeError("replay database setup requires a valid PostgreSQL URL") from None
    if parsed.get_backend_name() != "postgresql" or not parsed.database or not parsed.database.startswith(required_prefix):
        raise RuntimeError(f"replay database setup requires {required_prefix}* PostgreSQL database")
    if resolved_url.startswith("postgresql://"):
        return resolved_url.replace(
            "postgresql://",
            "postgresql+asyncpg://",
            1,
        )
    return resolved_url


def install_replay_model_adapter() -> None:
    """Point the credential-free ``vision_bridge_fake`` test model at ReplayChatModel.

    The override lives only in replay Gateway/Worker processes. The database
    still contains a supported, credential-free provider adapter, so the test
    harness does not add a test implementation to the production allowlist.
    """
    from app.system_settings import validation

    validation.PROVIDER_ADAPTERS["vision_bridge_fake"] = validation.ProviderAdapterSpec(
        "replay_provider:ReplayChatModel",
        False,
    )


async def prepare_replay_runtime_catalog(
    database_url: str | None = None,
) -> None:
    """Idempotently seed the PostgreSQL authorities needed by replay.

    The caller must point at a disposable, fully initialized ActWeave database.
    ``scenario-model`` becomes the default model, while memory and summarization
    are disabled to remove background/debounced model calls from the fixture.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.audit.models import resolve_system_audit_context
    from app.audit.service import AuditService
    from app.reliability.owner_refs import AuditHmacKeyring
    from app.system_runtime_settings.bootstrap import (
        bootstrap_system_runtime_policies,
    )
    from app.system_runtime_settings.models import (
        AgentRuntimePolicyValue,
        RuntimePolicySection,
    )
    from app.system_runtime_settings.service import SystemRuntimePolicyService
    from app.system_settings.models import CreateSystemModel
    from app.system_settings.service import SystemModelCatalogService
    from deerflow.persistence.user.model import UserRow

    resolved_url = _validated_replay_database_url(database_url)

    engine = create_async_engine(resolved_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await bootstrap_system_runtime_policies(session_factory)
        async with session_factory() as session, session.begin():
            admin = await session.get(
                UserRow,
                str(_REPLAY_ADMIN_ID),
                with_for_update=True,
            )
            if admin is None:
                session.add(
                    UserRow(
                        id=str(_REPLAY_ADMIN_ID),
                        email="replay-runtime-admin@example.invalid",
                        password_hash=None,
                        system_role="system_admin",
                        oauth_provider=None,
                        oauth_id=None,
                        needs_setup=False,
                        token_version=0,
                    )
                )
                await session.flush()
            elif admin.system_role != "system_admin":
                raise RuntimeError("replay runtime principal is not a system admin")

        audit_context = resolve_system_audit_context(
            SimpleNamespace(
                id=_REPLAY_ADMIN_ID,
                system_role="system_admin",
            ),
            request_id="replay-runtime-catalog",
        )
        model_catalog = SystemModelCatalogService(session_factory)
        catalog = await model_catalog.list_models(audit_context)
        matches = tuple(item for item in catalog.items if item.display_name == "Scenario Model" and item.provider_adapter == "vision_bridge_fake" and item.provider_model == "replay")
        if len(matches) > 1:
            raise RuntimeError("replay scenario model catalog is ambiguous")
        if matches:
            model = matches[0]
            if model.status != "active" or model.provider_adapter != "vision_bridge_fake" or model.provider_model != "replay" or model.settings or not model.supports_thinking or model.api_key_configured:
                raise RuntimeError("existing scenario-model is not replay-compatible")
        else:
            model = await model_catalog.create_model(
                audit_context,
                CreateSystemModel(
                    display_name="Scenario Model",
                    status="active",
                    provider_adapter="vision_bridge_fake",
                    provider_model="replay",
                    max_input_tokens=64_000,
                    settings={},
                    supports_thinking=True,
                    supports_reasoning_effort=False,
                    supports_vision=False,
                    api_key=None,
                ),
            )
            catalog = await model_catalog.list_models(audit_context)

        if catalog.default_model_config_id != model.id:
            await model_catalog.set_default(
                audit_context,
                model.id,
            )

        runtime_policy = SystemRuntimePolicyService(
            session_factory,
            AuditService(
                session_factory,
                AuditHmacKeyring.from_environment(),
            ),
        )
        policies = await runtime_policy.list_policies(audit_context)
        current = policies.sections[RuntimePolicySection.AGENT_RUNTIME]
        value = current.value
        if not isinstance(value, AgentRuntimePolicyValue):
            raise RuntimeError("agent runtime policy is unavailable for replay")
        desired = value.model_copy(
            update={
                "summarization": value.summarization.model_copy(
                    update={"enabled": False},
                ),
                "memory": value.memory.model_copy(
                    update={"enabled": False},
                ),
                "vision_bridge": value.vision_bridge.model_copy(
                    update={"model_name": None},
                ),
            },
        )
        if desired != value:
            await runtime_policy.update_policy(
                audit_context,
                RuntimePolicySection.AGENT_RUNTIME,
                expected_revision=current.revision,
                value=desired,
            )
    finally:
        await engine.dispose()


async def bootstrap_replay_test_database(
    database_url: str | None = None,
) -> None:
    """Install the complete test schema into an explicitly named replay database."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from deerflow.persistence.bootstrap import bootstrap_schema
    from scripts.setup_postgres import _bootstrap_langgraph_schemas

    resolved_url = _validated_replay_database_url(
        database_url,
        required_prefix="deerflow_test_replay_",
    )
    engine = create_async_engine(resolved_url)
    try:
        await bootstrap_schema(engine)
    finally:
        await engine.dispose()
    # Gateway and Worker both enforce the complete catalog before startup.
    # Install and document the optional LangGraph-owned tables before either
    # process begins so concurrent startup cannot observe a partial catalog.
    await _bootstrap_langgraph_schemas(resolved_url)


def prepare_hermetic_skills(home: Path) -> None:
    """Create an empty skills tree so the prompt has no host-dependent skills."""
    (home / "skills" / "public").mkdir(parents=True, exist_ok=True)
    (home / "skills" / "custom").mkdir(parents=True, exist_ok=True)


@contextmanager
def replay_worker() -> Iterator[None]:
    """Run the real independent Worker and wait for durable readiness."""
    backend_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    python_paths = (
        str(backend_root),
        str(backend_root / "tests"),
        str(backend_root / "packages" / "harness"),
        environment.get("PYTHONPATH", ""),
    )
    environment["PYTHONPATH"] = os.pathsep.join(path for path in python_paths if path)
    command = [
        sys.executable,
        str(backend_root / "tests" / "replay_worker_process.py"),
    ]
    process = subprocess.Popen(
        command,
        cwd=backend_root,
        env=environment,
        stdout=None,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        database_url = environment["DATABASE_URL"].replace(
            "postgresql+asyncpg://",
            "postgresql://",
            1,
        )
        import psycopg

        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"replay Worker exited before readiness: status={process.returncode}")
            with psycopg.connect(database_url) as connection:
                ready = connection.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM worker_nodes
                        WHERE draining IS FALSE
                          AND heartbeat_at >= now() - interval '10 seconds'
                          AND capabilities_json::jsonb @> '["private_run"]'::jsonb
                    )
                    """
                ).fetchone()[0]
            if ready:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("replay Worker did not publish readiness")
            time.sleep(0.05)
        yield
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
