"""PostgreSQL-only runtime has no backend-dependent worker gate."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from app.gateway.deps import langgraph_runtime
from deerflow.config.database_config import DatabaseConfig


@asynccontextmanager
async def _context(value):
    yield value


@pytest.mark.asyncio
async def test_multi_worker_postgres_runtime_does_not_recover_another_workers_active_run(monkeypatch):
    monkeypatch.setenv("GATEWAY_WORKERS", "8")
    config = SimpleNamespace(
        database=SimpleNamespace(),
        run_events=None,
        stream_bridge=None,
    )
    sf = MagicMock()
    run_manager = MagicMock()
    run_manager.reconcile_orphaned_inflight_runs = AsyncMock(return_value=[])
    run_manager.shutdown = AsyncMock()

    with (
        patch("deerflow.persistence.engine.init_engine_from_config", new=AsyncMock()) as init_engine,
        patch("deerflow.persistence.engine.get_session_factory", return_value=sf),
        patch("deerflow.persistence.engine.close_engine", new=AsyncMock()),
        patch("deerflow.runtime.make_stream_bridge", return_value=_context(MagicMock())),
        patch("deerflow.runtime.checkpointer.async_provider.make_checkpointer", return_value=_context(MagicMock())),
        patch("deerflow.runtime.make_store", return_value=_context(MagicMock())),
        patch("deerflow.persistence.thread_meta.make_thread_store", return_value=MagicMock()),
        patch("deerflow.runtime.events.store.make_run_event_store", return_value=MagicMock()),
        patch("app.gateway.deps.RunManager", return_value=run_manager),
    ):
        async with langgraph_runtime(FastAPI(), config):
            pass

    init_engine.assert_awaited_once_with(config.database)
    run_manager.reconcile_orphaned_inflight_runs.assert_not_awaited()


@pytest.mark.parametrize("workers", ["2", "8", "invalid", "0"])
def test_only_explicit_single_worker_allows_startup_recovery(workers: str, monkeypatch) -> None:
    from app.gateway.deps import _should_reconcile_orphaned_runs

    monkeypatch.setenv("GATEWAY_WORKERS", workers)
    assert _should_reconcile_orphaned_runs() is False


def test_single_worker_allows_startup_recovery(monkeypatch) -> None:
    from app.gateway.deps import _should_reconcile_orphaned_runs

    monkeypatch.setenv("GATEWAY_WORKERS", "1")
    assert _should_reconcile_orphaned_runs() is True


def test_uvicorn_web_concurrency_also_disables_recovery(monkeypatch) -> None:
    from app.gateway.deps import _should_reconcile_orphaned_runs

    monkeypatch.delenv("GATEWAY_WORKERS", raising=False)
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    assert _should_reconcile_orphaned_runs() is False


def test_conflicting_worker_environment_is_treated_conservatively(monkeypatch) -> None:
    from app.gateway.deps import _should_reconcile_orphaned_runs

    monkeypatch.setenv("GATEWAY_WORKERS", "1")
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    assert _should_reconcile_orphaned_runs() is False


def test_postgres_only_runtime_exposes_no_worker_backend_gate() -> None:
    from app.gateway import deps

    assert not hasattr(deps, "_enforce_postgres_for_multi_worker")


def test_database_config_rejects_backend_selector() -> None:
    with pytest.raises(ValueError, match="backend"):
        DatabaseConfig(url="postgresql://localhost/test", backend="memory")


def test_db_event_store_does_not_fallback_when_engine_is_uninitialized(monkeypatch) -> None:
    from deerflow.runtime.events import store as event_store_module

    def fail_uninitialized():
        raise RuntimeError("Persistence engine is not initialized")

    monkeypatch.setattr("deerflow.persistence.engine.get_session_factory", fail_uninitialized)
    with pytest.raises(RuntimeError, match="not initialized"):
        event_store_module.make_run_event_store(SimpleNamespace(backend="db", max_trace_content=100))
