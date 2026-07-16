"""Gateway startup recovery for legacy in-process persisted runs."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest
from fastapi import FastAPI

import deerflow.runtime as runtime_module
from app.gateway import deps as gateway_deps
from deerflow.persistence import engine as engine_module
from deerflow.persistence import thread_meta as thread_meta_module
from deerflow.runtime.checkpointer import async_provider as checkpointer_module
from deerflow.runtime.events import store as event_store_module


@asynccontextmanager
async def _fake_context(value):
    yield value


class _FakeRunManager:
    instances: list[_FakeRunManager] = []
    recovered_runs = [SimpleNamespace(run_id="run-1", thread_id="thread-1")]
    latest_by_thread: dict[str, list[SimpleNamespace]] = {}

    def __init__(self, *, store):
        self.store = store
        self.reconcile_calls: list[dict] = []
        self.list_by_thread_calls: list[dict] = []
        self.shutdown_calls = 0
        self.instances.append(self)

    async def reconcile_orphaned_inflight_runs(
        self,
        *,
        error: str,
        before: str | None = None,
    ):
        self.reconcile_calls.append({"error": error, "before": before})
        return self.recovered_runs

    async def list_by_thread(
        self,
        thread_id: str,
        *,
        user_id=None,
        limit: int = 100,
    ):
        self.list_by_thread_calls.append(
            {"thread_id": thread_id, "user_id": user_id, "limit": limit},
        )
        return self.latest_by_thread.get(thread_id, self.recovered_runs[:limit])

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        del timeout
        self.shutdown_calls += 1


class _FakeThreadStore:
    def __init__(self) -> None:
        self.status_updates: list[tuple[str, str, str | None]] = []

    async def update_status(
        self,
        thread_id: str,
        status: str,
        *,
        user_id=None,
    ) -> None:
        self.status_updates.append((thread_id, status, user_id))


class _FakeStreamBridge:
    def __init__(self, *, existing_streams: set[str] | None = None) -> None:
        self.publish_end_calls: list[str] = []
        self.cleanup_calls: list[tuple[str, float]] = []
        self._existing_streams = existing_streams or set()

    async def stream_exists(self, run_id: str) -> bool:
        return run_id in self._existing_streams

    async def publish_end(self, run_id: str) -> None:
        self.publish_end_calls.append(run_id)

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        self.cleanup_calls.append((run_id, delay))


def _install_runtime_doubles(
    monkeypatch,
    *,
    stream_bridge: _FakeStreamBridge,
    thread_store: _FakeThreadStore,
) -> None:
    monkeypatch.setattr(
        engine_module,
        "init_engine_from_config",
        async_noop,
    )
    monkeypatch.setattr(engine_module, "get_session_factory", lambda: object())
    monkeypatch.setattr(engine_module, "close_engine", async_noop)
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
    monkeypatch.setattr(gateway_deps, "RunManager", _FakeRunManager)


async def async_noop(*_args, **_kwargs) -> None:
    return None


@pytest.mark.anyio
async def test_recovered_run_stream_end_skips_expired_stream():
    stream_bridge = _FakeStreamBridge(existing_streams=set())

    await gateway_deps._publish_recovered_run_stream_end(
        stream_bridge,
        [SimpleNamespace(run_id="expired-run", thread_id="thread-1")],
    )

    assert stream_bridge.publish_end_calls == []
    assert stream_bridge.cleanup_calls == []


@pytest.mark.anyio
async def test_postgres_runtime_reconciles_legacy_orphaned_runs_on_startup(
    monkeypatch,
):
    app = FastAPI()
    monkeypatch.setenv("GATEWAY_WORKERS", "1")
    config = SimpleNamespace(
        database=SimpleNamespace(),
        run_events=SimpleNamespace(backend="memory"),
        stream_bridge=SimpleNamespace(
            recovered_stream_cleanup_delay_seconds=60.0,
        ),
    )
    thread_store = _FakeThreadStore()
    stream_bridge = _FakeStreamBridge(existing_streams={"run-1"})
    _FakeRunManager.instances.clear()
    _FakeRunManager.recovered_runs = [
        SimpleNamespace(run_id="run-1", thread_id="thread-1"),
    ]
    _FakeRunManager.latest_by_thread = {}
    _install_runtime_doubles(
        monkeypatch,
        stream_bridge=stream_bridge,
        thread_store=thread_store,
    )

    async with gateway_deps.langgraph_runtime(app, config):
        assert not hasattr(app.state, "automation_scheduler_ownership")
        assert not hasattr(app.state, "scheduled_task_service")
    await anyio.sleep(0)

    manager = _FakeRunManager.instances[0]
    assert manager.reconcile_calls and manager.reconcile_calls[0]["error"]
    assert manager.list_by_thread_calls == [
        {"thread_id": "thread-1", "user_id": None, "limit": 1},
    ]
    assert thread_store.status_updates == [("thread-1", "error", None)]
    assert stream_bridge.publish_end_calls == ["run-1"]
    assert stream_bridge.cleanup_calls == [("run-1", 60.0)]


@pytest.mark.anyio
async def test_legacy_orphan_recovery_preserves_newer_terminal_thread_state(
    monkeypatch,
):
    app = FastAPI()
    monkeypatch.setenv("GATEWAY_WORKERS", "1")
    config = SimpleNamespace(
        database=SimpleNamespace(),
        run_events=SimpleNamespace(backend="memory"),
        stream_bridge=SimpleNamespace(
            recovered_stream_cleanup_delay_seconds=60.0,
        ),
    )
    thread_store = _FakeThreadStore()
    stream_bridge = _FakeStreamBridge(existing_streams={"old-running"})
    _FakeRunManager.instances.clear()
    _FakeRunManager.recovered_runs = [
        SimpleNamespace(run_id="old-running", thread_id="thread-1"),
    ]
    _FakeRunManager.latest_by_thread = {
        "thread-1": [
            SimpleNamespace(
                run_id="newer-success",
                thread_id="thread-1",
                status="success",
            ),
        ],
    }
    _install_runtime_doubles(
        monkeypatch,
        stream_bridge=stream_bridge,
        thread_store=thread_store,
    )

    async with gateway_deps.langgraph_runtime(app, config):
        pass
    await anyio.sleep(0)

    assert _FakeRunManager.instances[0].list_by_thread_calls == [
        {"thread_id": "thread-1", "user_id": None, "limit": 1},
    ]
    assert thread_store.status_updates == []
    assert stream_bridge.publish_end_calls == ["old-running"]
    assert stream_bridge.cleanup_calls == [("old-running", 60.0)]
