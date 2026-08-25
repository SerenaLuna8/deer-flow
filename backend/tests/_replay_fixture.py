"""Shared process helpers for the deterministic replay browser test."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, Protocol

from starlette.requests import Request

_REPLAY_ADMIN_ID = uuid.UUID("5fb66f7d-5655-54df-a7da-66066c114f17")
_REPLAY_WORKER_FRESH_FOR_SECONDS = 3
_REPLAY_DATABASE_NAME = re.compile(r"deerflow_test_replay_[0-9]+_[0-9a-f]{32}\Z")
_REPLAY_FAULT_BARRIER_ROOT_ENV = "ACT_WEAVE_REPLAY_FAULT_BARRIER_ROOT"
ReplayFault = Literal["model", "claim", "begin_execution"]
_REPLAY_FAULTS: tuple[ReplayFault, ...] = (
    "model",
    "claim",
    "begin_execution",
)


class _StopEvent(Protocol):
    def is_set(self) -> bool: ...


class ReplayFaultBarriers:
    """Task-local file barriers shared by the replay Gateway and Worker."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _marker(self, fault: ReplayFault) -> Path:
        if fault not in _REPLAY_FAULTS:
            raise ValueError("unsupported replay fault")
        return self.root / f"{fault}.hold"

    def hold(self, fault: ReplayFault) -> None:
        self._marker(fault).touch(exist_ok=True)

    def release(self, fault: ReplayFault) -> None:
        self._marker(fault).unlink(missing_ok=True)

    def release_all(self) -> None:
        for fault in _REPLAY_FAULTS:
            self.release(fault)

    def is_held(self, fault: ReplayFault) -> bool:
        return self._marker(fault).is_file()

    def wait(
        self,
        fault: ReplayFault,
        *,
        poll_seconds: float = 0.02,
    ) -> None:
        while self.is_held(fault):
            time.sleep(poll_seconds)

    async def wait_async(
        self,
        fault: ReplayFault,
        *,
        poll_seconds: float = 0.02,
        stop_event: _StopEvent | None = None,
    ) -> bool:
        import asyncio

        while self.is_held(fault):
            if stop_event is not None and stop_event.is_set():
                return False
            await asyncio.sleep(poll_seconds)
        return True

    def snapshot(self) -> dict[str, bool]:
        return {
            "held_model": self.is_held("model"),
            "held_claim": self.is_held("claim"),
            "held_begin_execution": self.is_held("begin_execution"),
        }


def replay_fault_barriers_from_environment() -> ReplayFaultBarriers | None:
    raw_root = os.environ.get(_REPLAY_FAULT_BARRIER_ROOT_ENV, "").strip()
    if not raw_root:
        return None
    root = Path(raw_root).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError("replay fault barrier root is unavailable")
    return ReplayFaultBarriers(root)


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
worker:
  enabled: true
  poll_interval_seconds: 0.1
  lease_seconds: 15
  heartbeat_seconds: 1
  max_concurrent_jobs: 8
  shutdown_grace_seconds: 5
  retry_initial_seconds: 1
  retry_max_seconds: 5
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


@dataclass(slots=True)
class ReplayTestDatabase:
    database_url: str
    database_name: str
    dropped: bool = False


def _development_database_coordinates(
    database_url: str | None,
) -> tuple[str, str, str]:
    from sqlalchemy.engine import make_url

    if not database_url:
        raise RuntimeError("development DATABASE_URL is required for replay database setup")
    try:
        parsed = make_url(database_url)
    except Exception:
        raise RuntimeError("replay database setup requires a valid development PostgreSQL URL") from None
    if parsed.get_backend_name() != "postgresql" or parsed.host not in {"127.0.0.1", "localhost", "::1"} or not parsed.database or parsed.database in {"postgres", "template0", "template1"} or parsed.database.startswith("deerflow_test_"):
        raise RuntimeError("replay database setup requires a loopback development PostgreSQL database")

    database_name = f"deerflow_test_replay_{os.getpid()}_{uuid.uuid4().hex}"
    if _REPLAY_DATABASE_NAME.fullmatch(database_name) is None:
        raise RuntimeError("invalid replay test database name")
    maintenance_url = parsed.set(
        drivername="postgresql",
        database="postgres",
    ).render_as_string(hide_password=False)
    replay_url = parsed.set(
        drivername="postgresql+asyncpg",
        database=database_name,
    ).render_as_string(hide_password=False)
    return maintenance_url, replay_url, database_name


@contextmanager
def replay_test_database_from_development(
    database_url: str | None,
) -> Iterator[ReplayTestDatabase]:
    """Create and always drop one random loopback replay database."""

    import psycopg
    from psycopg import sql

    maintenance_url, replay_url, database_name = _development_database_coordinates(database_url)
    with psycopg.connect(maintenance_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(database_name)))
    try:
        database = ReplayTestDatabase(
            database_url=replay_url,
            database_name=database_name,
        )
        yield database
    finally:
        with psycopg.connect(maintenance_url, autocommit=True) as connection:
            connection.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname=%s AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
        if "database" in locals():
            database.dropped = True


def install_replay_model_adapter() -> None:
    """Point test-only ``openai`` execution at credential-free ReplayChatModel.

    The override lives only in replay Gateway/Worker processes. The database
    retains a provider engineering profile supported by the production request
    guard, while the process-local descriptor prevents any network/API-key use.
    """
    from app.system_settings import validation

    validation.PROVIDER_ADAPTERS["openai"] = validation.ProviderAdapterSpec(
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
        matches = tuple(item for item in catalog.items if item.display_name == "Scenario Model" and item.provider_adapter == "openai" and item.provider_model == "replay")
        if len(matches) > 1:
            raise RuntimeError("replay scenario model catalog is ambiguous")
        if matches:
            model = matches[0]
            if model.status != "active" or model.provider_adapter != "openai" or model.provider_model != "replay" or model.settings or not model.supports_thinking or model.api_key_configured:
                raise RuntimeError("existing scenario-model is not replay-compatible")
        else:
            model = await model_catalog.create_model(
                audit_context,
                CreateSystemModel(
                    display_name="Scenario Model",
                    status="active",
                    provider_adapter="openai",
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


def _sync_postgres_url(database_url: str) -> str:
    from sqlalchemy.engine import make_url

    parsed = make_url(database_url)
    return parsed.set(drivername="postgresql").render_as_string(
        hide_password=False,
    )


def _replay_worker_registry_is_fresh(
    *,
    database_url: str,
    fresh_for_seconds: int,
) -> bool:
    import psycopg

    with psycopg.connect(_sync_postgres_url(database_url)) as connection:
        row = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM worker_nodes
                WHERE draining IS FALSE
                  AND heartbeat_at >= now() - make_interval(secs => %s)
                  AND capabilities_json::jsonb @> '["private_run"]'::jsonb
            )
            """,
            (fresh_for_seconds,),
        ).fetchone()
    return bool(row and row[0] is True)


def _start_replay_worker_process(
    *,
    database_url: str,
    barrier_root: Path,
) -> subprocess.Popen[str]:
    backend_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    environment[_REPLAY_FAULT_BARRIER_ROOT_ENV] = str(barrier_root)
    python_paths = (
        str(backend_root),
        str(backend_root / "tests"),
        str(backend_root / "packages" / "harness"),
        environment.get("PYTHONPATH", ""),
    )
    environment["PYTHONPATH"] = os.pathsep.join(path for path in python_paths if path)
    return subprocess.Popen(
        [
            sys.executable,
            str(backend_root / "tests" / "replay_worker_process.py"),
        ],
        cwd=backend_root,
        env=environment,
        stdout=None,
        stderr=subprocess.STDOUT,
        text=True,
    )


class ReplayWorkerController:
    """Own one real replay Worker inside an exact disposable database."""

    def __init__(
        self,
        *,
        database_url: str,
        mode: Literal["immediate", "delayed"],
        worker_fresh_for_seconds: int = _REPLAY_WORKER_FRESH_FOR_SECONDS,
        readiness_timeout_seconds: float = 20,
        barrier_parent: Path | None = None,
    ) -> None:
        if mode not in {"immediate", "delayed"}:
            raise ValueError("unsupported replay Worker mode")
        self._database_url = _validated_replay_database_url(
            database_url,
            required_prefix="deerflow_test_replay_",
        )
        if worker_fresh_for_seconds < 1 or readiness_timeout_seconds <= 0:
            raise ValueError("invalid replay Worker readiness policy")
        self._mode = mode
        self._worker_fresh_for_seconds = worker_fresh_for_seconds
        self._readiness_timeout_seconds = readiness_timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._starts = 0
        self._stops = 0
        self._crashes = 0
        self._barrier_tempdir = tempfile.TemporaryDirectory(
            prefix="actweave-replay-faults-",
            dir=barrier_parent,
        )
        self._barriers = ReplayFaultBarriers(
            Path(self._barrier_tempdir.name),
        )

    def _fresh(self) -> bool:
        return _replay_worker_registry_is_fresh(
            database_url=self._database_url,
            fresh_for_seconds=self._worker_fresh_for_seconds,
        )

    def _snapshot(self) -> dict[str, object]:
        process = self._process
        snapshot: dict[str, object] = {
            "mode": self._mode,
            "running": process is not None and process.poll() is None,
            "fresh": self._fresh(),
        }
        snapshot.update(self._barriers.snapshot())
        return snapshot

    def status(self) -> dict[str, object]:
        with self._lock:
            return self._snapshot()

    def _wait_for_freshness(self, expected: bool) -> None:
        deadline = time.monotonic() + self._readiness_timeout_seconds
        while True:
            process = self._process
            if expected and (process is None or process.poll() is not None):
                status = None if process is None else process.returncode
                raise RuntimeError(f"replay Worker exited before readiness: status={status}")
            if self._fresh() is expected:
                return
            if time.monotonic() >= deadline:
                state = "fresh" if expected else "stale"
                raise TimeoutError(f"replay Worker registry did not become {state}")
            time.sleep(0.05)

    def start(self) -> dict[str, object]:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                self._wait_for_freshness(True)
                return self._snapshot()
            if self._fresh():
                raise RuntimeError("replay Worker registry is fresh without controller ownership")
            self._process = _start_replay_worker_process(
                database_url=self._database_url,
                barrier_root=self._barriers.root,
            )
            self._starts += 1
            try:
                self._wait_for_freshness(True)
            except BaseException:
                self._stop_process()
                raise
            return self._snapshot()

    def _stop_process(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            self._process = None
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        finally:
            self._process = None
            self._stops += 1

    def stop(self) -> dict[str, object]:
        with self._lock:
            self._barriers.release_all()
            self._stop_process()
            self._wait_for_freshness(False)
            return self._snapshot()

    def crash(self) -> dict[str, object]:
        """SIGKILL the owned Worker without removing its registry row."""

        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise RuntimeError("replay Worker is not running")
            process.kill()
            process.wait(timeout=5)
            self._process = None
            self._crashes += 1
            return self._snapshot()

    def hold(self, fault: ReplayFault) -> dict[str, object]:
        with self._lock:
            self._barriers.hold(fault)
            return self._snapshot()

    def release(self, fault: ReplayFault) -> dict[str, object]:
        with self._lock:
            self._barriers.release(fault)
            return self._snapshot()

    def close(self) -> None:
        try:
            self.stop()
        finally:
            self._barriers.release_all()
            self._barrier_tempdir.cleanup()

    def lifecycle_readback(self) -> dict[str, object]:
        with self._lock:
            snapshot = self._snapshot()
            snapshot.update(
                {
                    "starts": self._starts,
                    "stops": self._stops,
                    "crashes": self._crashes,
                }
            )
            return snapshot


@contextmanager
def replay_worker() -> Iterator[None]:
    """Run the real independent Worker and wait for durable readiness."""
    controller = ReplayWorkerController(
        database_url=os.environ.get("DATABASE_URL", ""),
        mode="immediate",
    )
    try:
        controller.start()
        yield
    finally:
        controller.close()
