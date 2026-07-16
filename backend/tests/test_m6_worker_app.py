from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.worker import app as worker_app
from deerflow.config.worker_config import WorkerConfig
from deerflow.persistence.jobs.sql import JobOwnerRef


@asynccontextmanager
async def _resource(value):
    yield value


@pytest.mark.asyncio
async def test_worker_entrypoint_installs_default_private_run_handler(
    monkeypatch,
) -> None:
    config = SimpleNamespace(
        worker=WorkerConfig(enabled=True),
        database=object(),
        run_events=SimpleNamespace(max_trace_content=1024),
    )
    session_factory = object()
    captured: dict[str, object] = {}

    class Guard:
        def __init__(self, factory) -> None:
            assert factory is session_factory

        async def require_worker_open(self) -> None:
            captured["guard"] = True

    class Registry:
        def __init__(self, factory, *, version) -> None:
            captured["registry"] = (factory, version)

    class Executor:
        def __init__(self, factory, **kwargs) -> None:
            captured["executor"] = (factory, kwargs)

    class Handler:
        def __init__(self, factory, **kwargs) -> None:
            captured["handler"] = (factory, kwargs)

    class Service:
        def __init__(
            self,
            factory,
            registry,
            handlers,
            worker_config,
            *,
            repository_builder,
        ) -> None:
            captured["service"] = (
                factory,
                registry,
                handlers,
                worker_config,
                repository_builder,
            )

        async def run(self, stop_event) -> None:
            captured["stop_event"] = stop_event

    async def init_engine(database) -> None:
        captured["database"] = database

    async def close_engine() -> None:
        captured["closed"] = True

    monkeypatch.setattr(worker_app, "get_app_config", lambda: config)
    monkeypatch.setattr(worker_app, "init_engine", init_engine)
    monkeypatch.setattr(worker_app, "close_engine", close_engine)
    monkeypatch.setattr(
        worker_app,
        "get_session_factory",
        lambda: session_factory,
    )
    monkeypatch.setattr(worker_app, "ReliabilityCutoverGuard", Guard)
    monkeypatch.setattr(worker_app, "WorkerRegistry", Registry)
    monkeypatch.setattr(worker_app, "RunAgentPrivateExecutor", Executor)
    monkeypatch.setattr(worker_app, "PrivateRunJobHandler", Handler)
    monkeypatch.setattr(worker_app, "WorkerService", Service)

    class Keyring:
        @classmethod
        def from_environment(cls):
            captured["audit_keyring"] = True
            return cls()

        @staticmethod
        def job_owner_ref(_owner):
            return JobOwnerRef(key_id="test", hmac_hex="a" * 64)

    monkeypatch.setattr(worker_app, "AuditHmacKeyring", Keyring)
    monkeypatch.setattr(
        worker_app,
        "make_stream_bridge",
        lambda _config: _resource("bridge"),
    )
    monkeypatch.setattr(
        worker_app,
        "make_checkpointer",
        lambda _config: _resource("checkpointer"),
    )
    monkeypatch.setattr(
        worker_app,
        "make_store",
        lambda _config: _resource("store"),
    )
    monkeypatch.setattr(
        worker_app,
        "ProjectScopedCheckpointer",
        lambda raw, factory: (raw, factory),
    )
    monkeypatch.setattr(
        worker_app,
        "DbRunEventStore",
        lambda factory, **kwargs: (factory, kwargs),
    )

    stop_event = asyncio.Event()
    await worker_app.run_worker(stop_event=stop_event)

    _factory, _registry, handlers, worker_config, repository_builder = captured["service"]
    assert set(handlers) == {"private_run"}
    assert worker_config is config.worker
    repository = repository_builder(object())
    assert repository._owner_ref(str(uuid.uuid4())).key_id == "test"
    assert captured["stop_event"] is stop_event
    assert captured["guard"] is True
    assert captured["audit_keyring"] is True
    assert captured["closed"] is True
