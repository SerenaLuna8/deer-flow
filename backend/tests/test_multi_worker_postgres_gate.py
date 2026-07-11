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
async def test_multi_worker_postgres_runtime_starts_without_backend_gate(monkeypatch):
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
    run_manager.reconcile_orphaned_inflight_runs.assert_awaited_once()


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
