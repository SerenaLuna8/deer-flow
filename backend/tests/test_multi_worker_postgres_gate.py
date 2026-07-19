"""PostgreSQL-only runtime has no backend-dependent worker gate."""

from __future__ import annotations

import inspect

import pytest

from deerflow.config.database_config import DatabaseConfig


def test_gateway_has_no_embedded_run_manager_and_worker_owns_agent_execution() -> None:
    from app.gateway.deps import gateway_platform_runtime
    from app.reliability.execution import RunAgentPrivateExecutor

    gateway_source = inspect.getsource(gateway_platform_runtime)
    worker_source = inspect.getsource(RunAgentPrivateExecutor.execute)

    assert "RunManager" not in gateway_source
    assert "make_stream_bridge" not in gateway_source
    assert "run_agent" not in gateway_source
    assert "RunManager()" in worker_source
    assert "await self._runner(" in worker_source


def test_postgres_only_runtime_exposes_no_worker_backend_gate() -> None:
    from app.gateway import deps

    assert not hasattr(deps, "_enforce_postgres_for_multi_worker")


def test_database_config_rejects_backend_selector() -> None:
    with pytest.raises(ValueError, match="backend"):
        DatabaseConfig(url="postgresql://localhost/test", backend="memory")


def test_db_event_store_requires_explicit_postgres_session_factory() -> None:
    from deerflow.runtime.events import store as event_store_module

    assert not hasattr(event_store_module, "make_run_event_store")
    parameter = inspect.signature(event_store_module.DbRunEventStore).parameters["session_factory"]
    assert parameter.default is inspect.Parameter.empty
