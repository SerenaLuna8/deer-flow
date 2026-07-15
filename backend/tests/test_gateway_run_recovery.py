"""Gateway startup recovery for stale persisted runs."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from fastapi import FastAPI

import deerflow.runtime as runtime_module
from app.automations.errors import AutomationUnavailable
from app.gateway import deps as gateway_deps
from deerflow.persistence import engine as engine_module
from deerflow.persistence import thread_meta as thread_meta_module
from deerflow.runtime.checkpointer import async_provider as checkpointer_module
from deerflow.runtime.events import store as event_store_module


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
        scheduler=SimpleNamespace(
            enabled=False,
            poll_interval_seconds=5,
            lease_seconds=120,
            max_concurrent_runs=3,
        ),
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
