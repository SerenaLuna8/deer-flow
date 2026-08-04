"""Shared config + gateway-drive helpers for the record/replay e2e.

Record (``scripts/record_gateway.py`` + ``scripts/build_fixture_from_jsonl.py``)
and replay (``tests/test_replay_golden.py``) MUST drive the gateway through an
identical prompt-affecting process config. Model definitions and Agent runtime
policy are PostgreSQL-owned; the replay setup below seeds those authorities in
the disposable test database rather than reviving removed YAML settings.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace

from support.project_agent_factory import create_project_agent_from_design

from app.shared_assets.models import AgentPayload
from deerflow.persistence.engine import get_session_factory

# mode -> (thinking_enabled, is_plan_mode, subagent_enabled). Mirrors the
# frontend mapping in core/threads/hooks.ts.
MODE_CONTEXT: dict[str, tuple[bool, bool, bool]] = {
    "flash": (False, False, False),
    "thinking": (True, False, False),
    "pro": (True, True, False),
    # thinking_enabled mirrors the frontend `context.mode !== "flash"` (hooks.ts),
    # so ultra is thinking-enabled too.
    "ultra": (True, True, True),
}

_REPLAY_ADMIN_ID = uuid.UUID("5fb66f7d-5655-54df-a7da-66066c114f17")


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
    """Point the credential-free ``codex_cli`` test model at ReplayChatModel.

    The override lives only in replay Gateway/Worker processes. The database
    still contains a supported, credential-free provider adapter, so the test
    harness does not add a test implementation to the production allowlist.
    """
    from app.system_settings import validation

    validation.PROVIDER_ADAPTERS["codex_cli"] = validation.ProviderAdapterSpec(
        "replay_provider:ReplayChatModel",
        False,
    )


@contextmanager
def replay_model_adapter() -> Iterator[None]:
    """Temporarily install the replay adapter in an in-process test."""
    from app.system_settings import validation

    original = validation.PROVIDER_ADAPTERS["codex_cli"]
    install_replay_model_adapter()
    try:
        yield
    finally:
        validation.PROVIDER_ADAPTERS["codex_cli"] = original


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
        matches = tuple(item for item in catalog.items if item.logical_name == "scenario-model")
        if len(matches) > 1:
            raise RuntimeError("replay scenario model catalog is ambiguous")
        if matches:
            model = matches[0]
            version = model.current_version
            if model.status != "active" or version.provider_adapter != "codex_cli" or version.provider_model != "replay" or version.settings or not version.supports_thinking or version.credential_id is not None:
                raise RuntimeError("existing scenario-model is not replay-compatible")
        else:
            model = await model_catalog.create_model(
                audit_context,
                CreateSystemModel(
                    logical_name="scenario-model",
                    display_name="Scenario Model",
                    description="Deterministic record/replay test model",
                    status="active",
                    provider_adapter="codex_cli",
                    provider_model="replay",
                    settings={},
                    supports_thinking=True,
                    supports_reasoning_effort=False,
                    supports_vision=False,
                    credential_id=None,
                    credential_version_id=None,
                    credential_env_key=None,
                ),
            )
            catalog = await model_catalog.list_models(audit_context)

        if catalog.default_model_config_id != model.id:
            await model_catalog.set_default(
                audit_context,
                model.id,
                expected_catalog_revision=catalog.catalog_revision,
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
                    update={
                        "enabled": False,
                        "search_enabled": False,
                        "injection_enabled": False,
                    },
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
    """Install the test schema only into an explicitly named replay database."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from deerflow.persistence.bootstrap import bootstrap_schema

    resolved_url = _validated_replay_database_url(
        database_url,
        required_prefix="deerflow_test_replay_",
    )
    engine = create_async_engine(resolved_url)
    try:
        await bootstrap_schema(engine)
    finally:
        await engine.dispose()


def prepare_hermetic_skills(home: Path) -> None:
    """Create an empty skills tree so the prompt has no host-dependent skills."""
    (home / "skills" / "public").mkdir(parents=True, exist_ok=True)
    (home / "skills" / "custom").mkdir(parents=True, exist_ok=True)


@contextmanager
def replay_worker(*, replay_adapter: bool = True) -> Iterator[None]:
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
    command = [sys.executable, "-m", "app.worker.app"]
    if replay_adapter:
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


def sse_event_shapes(resp) -> list[dict]:
    """Reduce an SSE stream to (event name, sorted top-level data keys).

    Snapshots the *shape* of the stream, not volatile values, so the golden is
    stable across runs while still catching event-sequence / payload-shape drift.
    """
    events: list[dict] = []
    current: str | None = None
    for line in resp.iter_lines():
        if line.startswith("event:"):
            current = line[len("event:") :].strip()
        elif line.startswith("data:"):
            raw = line[len("data:") :].strip()
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"_raw": raw[:200]}
            events.append({"event": current, "keys": sorted(data.keys()) if isinstance(data, dict) else None})
    return events


def drive_gateway(app, *, prompt: str, context: dict) -> list[dict]:
    with replay_worker():
        return _drive_gateway(app, prompt=prompt, context=context)


def _drive_gateway(app, *, prompt: str, context: dict) -> list[dict]:
    """Register -> create project Agent/thread -> stream a private run.

    Auth, project creation, activation, Thread creation, and Run admission use
    the real Gateway wire path. Deterministic setup creates the complete Agent
    through the same atomic service seam as Builder commit, avoiding both the
    removed manual-version API and an extra model call that would alter replay.
    """
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        reg = client.post(
            "/api/v1/auth/register",
            json={"email": f"e2e-{uuid.uuid4().hex[:8]}@example.com", "password": "very-strong-password-123"},
        )
        assert reg.status_code == 201, reg.text
        csrf = client.cookies.get("csrf_token")
        assert csrf, "register must set csrf_token cookie"
        headers = {"X-CSRF-Token": csrf}

        suffix = uuid.uuid4().hex[:10]
        project = client.post(
            "/api/projects",
            json={"slug": f"replay-{suffix}", "display_name": "Replay project"},
            headers=headers,
        )
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]

        assert client.portal is not None
        created = client.portal.call(
            partial(
                create_project_agent_from_design,
                get_session_factory(),
                user_id=uuid.UUID(reg.json()["id"]),
                project_id=uuid.UUID(project_id),
                slug=f"replay-agent-{suffix}",
                display_name="Replay Agent",
                payload=AgentPayload(
                    description="Deterministic gateway replay",
                    soul="Use the exact project tools to complete the request.",
                    model_ref="scenario-model",
                    tool_groups=("file:read", "file:write"),
                    skill_version_ids=(),
                    mcp_version_ids=(),
                ),
                request_id="replay-agent-setup",
            ),
        )
        asset_id = str(created.asset.id)
        activated = client.post(
            f"/api/projects/{project_id}/agents/{asset_id}/activate",
            json={"expected_asset_version": created.asset.version},
            headers=headers,
        )
        assert activated.status_code == 200, activated.text

        thread_id = str(uuid.uuid4())
        created = client.post(
            f"/api/projects/{project_id}/private-work/threads",
            json={
                "thread_id": thread_id,
                "agent_asset_id": asset_id,
                "agent_scope": "project",
                "metadata": {},
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text

        body = {
            "assistant_id": "lead_agent",
            "input": {"messages": [{"role": "user", "content": prompt}]},
            "config": {"recursion_limit": 50},
            "context": context,
        }
        with client.stream(
            "POST",
            f"/api/projects/{project_id}/private-work/threads/{thread_id}/runs/stream",
            json=body,
            headers=headers,
        ) as resp:
            assert resp.status_code == 200, resp.read().decode()
            return sse_event_shapes(resp)
