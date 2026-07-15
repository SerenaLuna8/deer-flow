"""Gateway startup recovery for stale persisted runs."""

from __future__ import annotations

import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from fastapi import FastAPI
from support.m4_private_threads import seed_m4_thread_database

import deerflow.runtime as runtime_module
from app.automations.errors import AutomationUnavailable
from app.automations.occurrences import deterministic_run_id, deterministic_thread_id
from app.gateway import deps as gateway_deps
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.persistence import engine as engine_module
from deerflow.persistence import thread_meta as thread_meta_module
from deerflow.persistence.scheduled_task_runs import (
    ScheduledTaskRunCreate,
    ScheduledTaskRunRepository,
)
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskCreate,
    ScheduledTaskRepository,
)
from deerflow.runtime.checkpointer import async_provider as checkpointer_module
from deerflow.runtime.events import store as event_store_module

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


@asynccontextmanager
async def _fake_context(value):
    yield value


class _FakeRunManager:
    """RunManager double that records startup reconciliation calls."""

    instances: list[_FakeRunManager] = []
    recovered_runs = [SimpleNamespace(run_id="run-1", thread_id="thread-1")]
    latest_by_thread: dict[str, list[SimpleNamespace]] = {}

    def __init__(self, *, store):
        self.store = store
        self.reconcile_calls: list[dict] = []
        self.list_by_thread_calls: list[dict] = []
        self.shutdown_calls: int = 0
        _FakeRunManager.instances.append(self)

    async def reconcile_orphaned_inflight_runs(self, *, error: str, before: str | None = None):
        self.reconcile_calls.append({"error": error, "before": before})
        return self.recovered_runs

    async def list_by_thread(self, thread_id: str, *, user_id=None, limit: int = 100):
        self.list_by_thread_calls.append({"thread_id": thread_id, "user_id": user_id, "limit": limit})
        return self.latest_by_thread.get(thread_id, self.recovered_runs[:limit])

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        # No in-flight tasks in these startup-recovery tests; langgraph_runtime
        # drains the manager on teardown, so the double must accept the call.
        self.shutdown_calls += 1


class _FakeThreadStore:
    def __init__(self) -> None:
        self.status_updates: list[tuple[str, str, str | None]] = []

    async def update_status(self, thread_id: str, status: str, *, user_id=None) -> None:
        self.status_updates.append((thread_id, status, user_id))


class _FakeStreamBridge:
    def __init__(self, *, existing_streams: set[str] | None = None) -> None:
        self.publish_end_calls: list[str] = []
        self.cleanup_calls: list[tuple[str, float]] = []
        self._existing_streams: set[str] = existing_streams if existing_streams is not None else set()

    async def stream_exists(self, run_id: str) -> bool:
        return run_id in self._existing_streams

    async def publish_end(self, run_id: str) -> None:
        self.publish_end_calls.append(run_id)

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        self.cleanup_calls.append((run_id, delay))


@pytest.mark.anyio
async def test_recovered_run_stream_end_skips_expired_stream():
    """Startup recovery should not recreate an already-expired retained stream."""
    stream_bridge = _FakeStreamBridge(existing_streams=set())

    await gateway_deps._publish_recovered_run_stream_end(
        stream_bridge,
        [SimpleNamespace(run_id="expired-run", thread_id="thread-1")],
    )

    assert stream_bridge.publish_end_calls == []
    assert stream_bridge.cleanup_calls == []


@pytest.mark.anyio
async def test_postgres_runtime_reconciles_orphaned_runs_on_startup(monkeypatch):
    """PostgreSQL startup should recover stale active runs before serving requests."""
    app = FastAPI()
    monkeypatch.setenv("GATEWAY_WORKERS", "1")
    config = SimpleNamespace(
        database=SimpleNamespace(),
        run_events=SimpleNamespace(backend="memory"),
        stream_bridge=SimpleNamespace(recovered_stream_cleanup_delay_seconds=60.0),
    )
    thread_store = _FakeThreadStore()
    stream_bridge = _FakeStreamBridge(existing_streams={"run-1"})
    _FakeRunManager.instances.clear()
    _FakeRunManager.recovered_runs = [SimpleNamespace(run_id="run-1", thread_id="thread-1")]
    _FakeRunManager.latest_by_thread = {}

    class DisabledOwnership:
        instances: list[DisabledOwnership] = []

        def __init__(self, _engine):
            self.is_acquired = False
            self.hold_calls = 0
            self.instances.append(self)

        @asynccontextmanager
        async def hold(self):
            self.hold_calls += 1
            yield self

    async def fake_init_engine_from_config(_database):
        return None

    async def fake_close_engine():
        return None

    monkeypatch.setattr(engine_module, "init_engine_from_config", fake_init_engine_from_config)
    monkeypatch.setattr(engine_module, "get_session_factory", lambda: object())
    monkeypatch.setattr(engine_module, "get_engine", lambda: object())
    monkeypatch.setattr(engine_module, "close_engine", fake_close_engine)
    monkeypatch.setattr(runtime_module, "make_stream_bridge", lambda _config: _fake_context(stream_bridge))
    monkeypatch.setattr(checkpointer_module, "make_checkpointer", lambda _config: _fake_context(object()))
    monkeypatch.setattr(runtime_module, "make_store", lambda _config: _fake_context(object()))
    monkeypatch.setattr(thread_meta_module, "make_thread_store", lambda _sf: thread_store)
    monkeypatch.setattr(event_store_module, "make_run_event_store", lambda _config: object())
    monkeypatch.setattr(gateway_deps, "RunManager", _FakeRunManager)
    monkeypatch.setattr(
        "app.automations.ownership.AutomationSchedulerOwnership",
        DisabledOwnership,
    )

    async with gateway_deps.langgraph_runtime(app, config):
        pass
    await anyio.sleep(0)

    assert len(_FakeRunManager.instances) == 1
    assert len(DisabledOwnership.instances) == 1
    assert DisabledOwnership.instances[0].is_acquired is False
    assert DisabledOwnership.instances[0].hold_calls == 0
    assert _FakeRunManager.instances[0].reconcile_calls
    assert _FakeRunManager.instances[0].reconcile_calls[0]["error"]
    assert _FakeRunManager.instances[0].list_by_thread_calls == [{"thread_id": "thread-1", "user_id": None, "limit": 1}]
    assert thread_store.status_updates == [("thread-1", "error", None)]
    assert stream_bridge.publish_end_calls == ["run-1"]
    assert stream_bridge.cleanup_calls == [("run-1", 60.0)]


@pytest.mark.anyio
async def test_postgres_runtime_does_not_mark_thread_error_when_newer_run_is_success(monkeypatch):
    """Startup recovery should not let an old orphaned run overwrite a newer terminal thread state."""
    app = FastAPI()
    monkeypatch.setenv("GATEWAY_WORKERS", "1")
    config = SimpleNamespace(
        database=SimpleNamespace(),
        run_events=SimpleNamespace(backend="memory"),
        stream_bridge=SimpleNamespace(recovered_stream_cleanup_delay_seconds=60.0),
    )
    thread_store = _FakeThreadStore()
    stream_bridge = _FakeStreamBridge(existing_streams={"old-running"})
    _FakeRunManager.instances.clear()
    _FakeRunManager.recovered_runs = [SimpleNamespace(run_id="old-running", thread_id="thread-1")]
    _FakeRunManager.latest_by_thread = {"thread-1": [SimpleNamespace(run_id="newer-success", thread_id="thread-1", status="success")]}

    async def fake_init_engine_from_config(_database):
        return None

    async def fake_close_engine():
        return None

    monkeypatch.setattr(engine_module, "init_engine_from_config", fake_init_engine_from_config)
    monkeypatch.setattr(engine_module, "get_session_factory", lambda: object())
    monkeypatch.setattr(engine_module, "close_engine", fake_close_engine)
    monkeypatch.setattr(runtime_module, "make_stream_bridge", lambda _config: _fake_context(stream_bridge))
    monkeypatch.setattr(checkpointer_module, "make_checkpointer", lambda _config: _fake_context(object()))
    monkeypatch.setattr(runtime_module, "make_store", lambda _config: _fake_context(object()))
    monkeypatch.setattr(thread_meta_module, "make_thread_store", lambda _sf: thread_store)
    monkeypatch.setattr(event_store_module, "make_run_event_store", lambda _config: object())
    monkeypatch.setattr(gateway_deps, "RunManager", _FakeRunManager)

    async with gateway_deps.langgraph_runtime(app, config):
        pass
    await anyio.sleep(0)

    assert len(_FakeRunManager.instances) == 1
    assert _FakeRunManager.instances[0].list_by_thread_calls == [{"thread_id": "thread-1", "user_id": None, "limit": 1}]
    assert thread_store.status_updates == []
    assert stream_bridge.publish_end_calls == ["old-running"]
    assert stream_bridge.cleanup_calls == [("old-running", 60.0)]


@pytest.mark.postgres
@pytest.mark.anyio
async def test_disabled_scheduler_reconciles_manual_run_before_generic_recovery(
    monkeypatch,
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    occurrence_id = str(uuid.uuid4())
    task_id = f"task-{uuid.uuid4().hex[:20]}"
    thread_id = deterministic_thread_id(occurrence_id)
    run_id = deterministic_run_id(occurrence_id)
    manual_hash = hashlib.sha256(f"manual:{occurrence_id}".encode()).hexdigest()
    try:
        async with seed.factory() as session, session.begin():
            task = await ScheduledTaskRepository(session).create(
                seed.owner_a.resource_scope,
                ScheduledTaskCreate(
                    task_id=task_id,
                    thread_id=None,
                    context_mode="fresh_thread_per_run",
                    agent_asset_id=seed.system_agent_id,
                    agent_scope="system",
                    title="Manual crash recovery",
                    prompt="Recover this admitted manual run.",
                    schedule_type="once",
                    schedule_spec={"run_at": NOW.isoformat()},
                    timezone="UTC",
                    next_run_at=None,
                ),
            )
            occurrences = ScheduledTaskRunRepository(session)
            await occurrences.create(
                seed.owner_a.resource_scope,
                ScheduledTaskRunCreate(
                    occurrence_id=occurrence_id,
                    task_id=task.id,
                    task_version=task.version,
                    occurrence_key=hashlib.sha256(occurrence_id.encode()).hexdigest(),
                    manual_idempotency_hash=manual_hash,
                    scheduled_for=NOW,
                    trigger="manual",
                    status="queued",
                    created_at=NOW,
                ),
            )
            claimed = await occurrences.claim(
                seed.owner_a.resource_scope,
                occurrence_id,
                now=NOW,
                lease_owner="crashed-gateway",
                lease_expires_at=NOW,
            )
            assert claimed is not None
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.system_agent_id, "system"),
                metadata={"scheduled_task_run_id": occurrence_id},
            )
            await PrivateRunRepository(session).create(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                request=PrivateRunCreate(
                    run_id=run_id,
                    status="pending",
                    metadata={
                        "scheduled_task_id": task_id,
                        "scheduled_task_run_id": occurrence_id,
                        "scheduled_trigger": "manual",
                    },
                ),
            )
            running = await occurrences.mark_running(
                seed.owner_a.resource_scope,
                occurrence_id,
                thread_id=thread_id,
                run_id=run_id,
                started_at=NOW,
                updated_at=NOW,
            )
            assert running is not None

        app = FastAPI()
        config = AppConfig(
            database={"url": migrated_postgres_database_url},
            sandbox=SandboxConfig(use="deerflow.sandbox.local:LocalSandboxProvider"),
            scheduler={"enabled": False},
        )
        stream_bridge = _FakeStreamBridge(existing_streams={run_id})
        thread_store = _FakeThreadStore()
        close_engine = AsyncMock()
        monkeypatch.setenv("GATEWAY_WORKERS", "1")
        monkeypatch.setattr(engine_module, "init_engine_from_config", AsyncMock())
        monkeypatch.setattr(engine_module, "get_session_factory", lambda: seed.factory)
        monkeypatch.setattr(engine_module, "get_engine", lambda: seed.engine)
        monkeypatch.setattr(engine_module, "close_engine", close_engine)
        monkeypatch.setattr(
            runtime_module,
            "make_stream_bridge",
            lambda _config: _fake_context(stream_bridge),
        )
        monkeypatch.setattr(
            checkpointer_module,
            "make_checkpointer",
            lambda _config: _fake_context(object()),
        )
        monkeypatch.setattr(
            runtime_module,
            "make_store",
            lambda _config: _fake_context(object()),
        )
        monkeypatch.setattr(
            thread_meta_module,
            "make_thread_store",
            lambda _sf: thread_store,
        )
        monkeypatch.setattr(
            event_store_module,
            "make_run_event_store",
            lambda _config: object(),
        )

        async with gateway_deps.langgraph_runtime(app, config):
            assert app.state.automation_scheduler_ownership.is_acquired is False
            assert app.state.scheduled_task_service.task is None

        async with seed.factory() as session:
            private_run = await PrivateRunRepository(session).get(
                scope=seed.owner_a.resource_scope,
                run_id=run_id,
            )
            occurrence = await ScheduledTaskRunRepository(session).get(
                seed.owner_a.resource_scope,
                occurrence_id,
            )
            parent = await ScheduledTaskRepository(session).get(
                seed.owner_a.resource_scope,
                task_id,
            )

        assert private_run is not None and private_run.status == "interrupted"
        assert occurrence is not None and occurrence.status == "interrupted"
        assert occurrence.error_code == "AUTOMATION_GATEWAY_RESTARTED"
        assert parent is not None and parent.status == "cancelled"
        assert parent.last_outcome == "interrupted"
        assert parent.run_count == 1
        assert stream_bridge.publish_end_calls == []
        close_engine.assert_awaited_once_with()
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_automation_reconciliation_failure_blocks_generic_orphan_mutation(
    monkeypatch,
) -> None:
    app = FastAPI()
    monkeypatch.setenv("GATEWAY_WORKERS", "1")
    config = SimpleNamespace(
        database=SimpleNamespace(),
        run_events=SimpleNamespace(backend="memory"),
        stream_bridge=None,
        scheduler=SimpleNamespace(
            enabled=True,
            poll_interval_seconds=5,
            lease_seconds=120,
            max_concurrent_runs=3,
        ),
    )
    stream_bridge = _FakeStreamBridge()
    _FakeRunManager.instances.clear()
    order: list[str] = []

    class OpenGuard:
        def __init__(self, _session_factory):
            pass

        async def require_project_open(self):
            return None

    class FailingReconciler:
        def __init__(self, _session_factory):
            pass

        async def reconcile_restart(self, _now):
            order.append("automation")
            raise AutomationUnavailable("automation-restart")

    class HeldOwnership:
        def __init__(self, _engine):
            self.is_acquired = False

        @asynccontextmanager
        async def hold(self):
            order.append("ownership-enter")
            self.is_acquired = True
            try:
                yield self
            finally:
                self.is_acquired = False
                order.append("ownership-exit")

    close_engine = AsyncMock()
    monkeypatch.setattr(engine_module, "init_engine_from_config", AsyncMock())
    monkeypatch.setattr(engine_module, "get_session_factory", lambda: object())
    monkeypatch.setattr(engine_module, "get_engine", lambda: object())
    monkeypatch.setattr(engine_module, "close_engine", close_engine)
    monkeypatch.setattr(
        runtime_module,
        "make_stream_bridge",
        lambda _config: _fake_context(stream_bridge),
    )
    monkeypatch.setattr(
        checkpointer_module,
        "make_checkpointer",
        lambda _config: _fake_context(object()),
    )
    monkeypatch.setattr(
        runtime_module,
        "make_store",
        lambda _config: _fake_context(object()),
    )
    monkeypatch.setattr(
        thread_meta_module,
        "make_thread_store",
        lambda _sf: _FakeThreadStore(),
    )
    monkeypatch.setattr(
        event_store_module,
        "make_run_event_store",
        lambda _config: object(),
    )
    monkeypatch.setattr(gateway_deps, "RunManager", _FakeRunManager)
    monkeypatch.setattr(
        "app.automations.cutover.AutomationCutoverGuard",
        OpenGuard,
    )
    monkeypatch.setattr(
        "app.automations.reconciliation.AutomationReconciler",
        FailingReconciler,
    )
    monkeypatch.setattr(
        "app.automations.ownership.AutomationSchedulerOwnership",
        HeldOwnership,
    )

    with pytest.raises(AutomationUnavailable):
        async with gateway_deps.langgraph_runtime(app, config):
            pass

    assert len(_FakeRunManager.instances) == 1
    assert _FakeRunManager.instances[0].reconcile_calls == []
    assert order == ["ownership-enter", "automation", "ownership-exit"]
    close_engine.assert_awaited_once_with()


@pytest.mark.anyio
async def test_automation_reconciliation_precedes_generic_orphan_recovery(
    monkeypatch,
) -> None:
    app = FastAPI()
    monkeypatch.setenv("GATEWAY_WORKERS", "1")
    config = SimpleNamespace(
        database=SimpleNamespace(),
        run_events=SimpleNamespace(backend="memory"),
        stream_bridge=None,
        scheduler=SimpleNamespace(
            enabled=True,
            poll_interval_seconds=5,
            lease_seconds=120,
            max_concurrent_runs=3,
        ),
    )
    order: list[str] = []

    class OpenGuard:
        def __init__(self, _session_factory):
            pass

        async def require_project_open(self):
            return None

    class OrderingReconciler:
        def __init__(self, _session_factory):
            pass

        async def reconcile_restart(self, _now):
            order.append("automation")

    class OrderingRunManager(_FakeRunManager):
        async def reconcile_orphaned_inflight_runs(
            self,
            *,
            error: str,
            before: str | None = None,
        ):
            order.append("generic")
            return await super().reconcile_orphaned_inflight_runs(
                error=error,
                before=before,
            )

    class HeldOwnership:
        def __init__(self, _engine):
            self.is_acquired = False

        @asynccontextmanager
        async def hold(self):
            order.append("ownership-enter")
            self.is_acquired = True
            try:
                yield self
            finally:
                self.is_acquired = False
                order.append("ownership-exit")

    OrderingRunManager.instances.clear()
    OrderingRunManager.recovered_runs = []
    monkeypatch.setattr(engine_module, "init_engine_from_config", AsyncMock())
    monkeypatch.setattr(engine_module, "get_session_factory", lambda: object())
    monkeypatch.setattr(engine_module, "get_engine", lambda: object())
    monkeypatch.setattr(engine_module, "close_engine", AsyncMock())
    monkeypatch.setattr(
        runtime_module,
        "make_stream_bridge",
        lambda _config: _fake_context(_FakeStreamBridge()),
    )
    monkeypatch.setattr(
        checkpointer_module,
        "make_checkpointer",
        lambda _config: _fake_context(object()),
    )
    monkeypatch.setattr(
        runtime_module,
        "make_store",
        lambda _config: _fake_context(object()),
    )
    monkeypatch.setattr(
        thread_meta_module,
        "make_thread_store",
        lambda _sf: _FakeThreadStore(),
    )
    monkeypatch.setattr(
        event_store_module,
        "make_run_event_store",
        lambda _config: object(),
    )
    monkeypatch.setattr(gateway_deps, "RunManager", OrderingRunManager)
    monkeypatch.setattr(
        "app.automations.cutover.AutomationCutoverGuard",
        OpenGuard,
    )
    monkeypatch.setattr(
        "app.automations.reconciliation.AutomationReconciler",
        OrderingReconciler,
    )
    monkeypatch.setattr(
        "app.automations.ownership.AutomationSchedulerOwnership",
        HeldOwnership,
    )

    async with gateway_deps.langgraph_runtime(app, config):
        pass

    assert order == [
        "ownership-enter",
        "automation",
        "generic",
        "ownership-exit",
    ]
