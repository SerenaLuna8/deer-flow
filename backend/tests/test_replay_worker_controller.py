from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from types import SimpleNamespace

import _replay_fixture as replay_fixture
import psycopg
import pytest
from _replay_fixture import (
    ReplayFaultBarriers,
    ReplayWorkerController,
    replay_test_database_from_development,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, message_to_dict
from replay_provider import ReplayChatModel, hash_replay_input
from sqlalchemy.engine import make_url

from app.private_work.context import PrivateWorkContext
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole


class _Process:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, *, timeout: float) -> int:
        del timeout
        if self.returncode is None:
            raise AssertionError("wait called before terminate")
        return self.returncode


def test_delayed_replay_worker_requires_exact_disposable_database_prefix() -> None:
    with pytest.raises(RuntimeError, match="deerflow_test_replay_"):
        ReplayWorkerController(
            database_url="postgresql+asyncpg://localhost/deerflow_test_other",
            mode="delayed",
        )


def test_delayed_replay_worker_start_and_stop_are_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process()
    spawns: list[object] = []

    def spawn(*, database_url: str, barrier_root):
        assert database_url.endswith("/deerflow_test_replay_controller")
        assert barrier_root.is_dir()
        spawns.append(object())
        return process

    monkeypatch.setattr(replay_fixture, "_start_replay_worker_process", spawn)
    monkeypatch.setattr(
        replay_fixture,
        "_replay_worker_registry_is_fresh",
        lambda *, database_url, fresh_for_seconds: database_url.endswith("/deerflow_test_replay_controller") and fresh_for_seconds == 3 and bool(spawns) and process.poll() is None,
    )
    monkeypatch.setattr(replay_fixture.time, "sleep", lambda _seconds: None)

    controller = ReplayWorkerController(
        database_url=("postgresql+asyncpg://localhost/deerflow_test_replay_controller"),
        mode="delayed",
        worker_fresh_for_seconds=3,
        readiness_timeout_seconds=1,
    )

    assert controller.status() == {
        "mode": "delayed",
        "running": False,
        "fresh": False,
        "held_model": False,
        "held_claim": False,
        "held_begin_execution": False,
    }
    assert controller.start() == {
        "mode": "delayed",
        "running": True,
        "fresh": True,
        "held_model": False,
        "held_claim": False,
        "held_begin_execution": False,
    }
    assert controller.start()["fresh"] is True
    assert len(spawns) == 1

    assert controller.stop() == {
        "mode": "delayed",
        "running": False,
        "fresh": False,
        "held_model": False,
        "held_claim": False,
        "held_begin_execution": False,
    }
    assert controller.stop()["running"] is False
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


def test_replay_fault_barriers_default_released_and_unblock_across_threads(
    tmp_path,
) -> None:
    barriers = ReplayFaultBarriers(tmp_path)

    assert barriers.snapshot() == {
        "held_model": False,
        "held_claim": False,
        "held_begin_execution": False,
    }
    barriers.hold("model")
    completed = threading.Event()
    waiter = threading.Thread(
        target=lambda: (barriers.wait("model"), completed.set()),
    )
    waiter.start()
    time.sleep(0.03)
    assert completed.is_set() is False

    barriers.release("model")
    waiter.join(timeout=1)
    assert completed.is_set() is True


async def _consume_async_stream(
    model: ReplayChatModel,
    messages: list[HumanMessage],
):
    return [chunk async for chunk in model._astream(messages)]


@pytest.mark.asyncio
async def test_replay_model_async_barrier_does_not_block_worker_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    barriers = ReplayFaultBarriers(tmp_path / "barriers")
    barriers.hold("model")
    monkeypatch.setenv(
        "ACT_WEAVE_REPLAY_FAULT_BARRIER_ROOT",
        str(barriers.root),
    )
    messages = [HumanMessage(content="barrier input")]
    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "scenario": "model-barrier",
                "turns": [
                    {
                        "caller": "lead_agent",
                        "input_hash": hash_replay_input(
                            messages,
                            caller="lead_agent",
                        ),
                        "output": message_to_dict(
                            AIMessage(content="released"),
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    model = ReplayChatModel(fixture=str(fixture))
    stream_task = asyncio.create_task(
        _consume_async_stream(model, messages),
    )

    heartbeat_ticks = 0
    for _ in range(5):
        await asyncio.sleep(0.02)
        heartbeat_ticks += 1
    assert heartbeat_ticks == 5
    assert stream_task.done() is False

    barriers.release("model")
    chunks = await asyncio.wait_for(stream_task, timeout=1)
    assert [chunk.message.content for chunk in chunks] == ["released"]


@pytest.mark.asyncio
async def test_replay_worker_claim_and_begin_execution_barriers_are_async(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from replay_worker_process import install_replay_worker_fault_controls

    from app.private_work.run_repository import PrivateRunRepository
    from app.worker.service import WorkerService

    barriers = ReplayFaultBarriers(tmp_path / "worker-barriers")
    barriers.hold("claim")
    barriers.hold("begin_execution")
    monkeypatch.setenv(
        "ACT_WEAVE_REPLAY_FAULT_BARRIER_ROOT",
        str(barriers.root),
    )
    calls: list[str] = []

    async def original_fill(_service, _stop_event=None) -> bool:
        calls.append("claim")
        return True

    async def original_begin(_repository, *args, **kwargs) -> str:
        del args, kwargs
        calls.append("begin_execution")
        return "begun"

    monkeypatch.setattr(WorkerService, "_fill_capacity", original_fill)
    monkeypatch.setattr(
        PrivateRunRepository,
        "begin_execution",
        original_begin,
    )
    install_replay_worker_fault_controls()

    stop_event = asyncio.Event()
    claim_task = asyncio.create_task(
        WorkerService._fill_capacity(SimpleNamespace(), stop_event),
    )
    begin_task = asyncio.create_task(
        PrivateRunRepository.begin_execution(SimpleNamespace()),
    )
    for _ in range(5):
        await asyncio.sleep(0.02)
    assert calls == []
    assert claim_task.done() is False
    assert begin_task.done() is False

    barriers.release("claim")
    barriers.release("begin_execution")
    assert await asyncio.wait_for(claim_task, timeout=1) is True
    assert await asyncio.wait_for(begin_task, timeout=1) == "begun"
    assert sorted(calls) == ["begin_execution", "claim"]


def test_replay_worker_crash_uses_sigkill_and_preserves_fresh_registry_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    process = _Process()
    spawned = False

    def spawn(*, database_url: str, barrier_root):
        nonlocal spawned
        assert database_url.endswith("/deerflow_test_replay_controller")
        assert barrier_root.parent == tmp_path
        spawned = True
        return process

    monkeypatch.setattr(replay_fixture, "_start_replay_worker_process", spawn)
    monkeypatch.setattr(
        replay_fixture,
        "_replay_worker_registry_is_fresh",
        lambda **_kwargs: spawned,
    )

    controller = ReplayWorkerController(
        database_url=("postgresql+asyncpg://localhost/deerflow_test_replay_controller"),
        mode="delayed",
        barrier_parent=tmp_path,
    )
    controller.hold("begin_execution")
    controller.start()

    assert controller.crash() == {
        "mode": "delayed",
        "running": False,
        "fresh": True,
        "held_model": False,
        "held_claim": False,
        "held_begin_execution": True,
    }
    assert process.kill_calls == 1
    assert process.terminate_calls == 0
    assert controller.lifecycle_readback()["crashes"] == 1


@pytest.mark.asyncio
async def test_replay_worker_router_exposes_only_controller_projection() -> None:
    from replay_agent_router import build_replay_worker_router

    calls: list[str] = []
    controller = SimpleNamespace(
        status=lambda: {
            "mode": "delayed",
            "running": False,
            "fresh": False,
            "held_model": False,
            "held_claim": False,
            "held_begin_execution": False,
        },
        start=lambda: (
            calls.append("start")
            or {
                "mode": "delayed",
                "running": True,
                "fresh": True,
                "held_model": False,
                "held_claim": False,
                "held_begin_execution": False,
            }
        ),
        stop=lambda: (
            calls.append("stop")
            or {
                "mode": "delayed",
                "running": False,
                "fresh": False,
                "held_model": False,
                "held_claim": False,
                "held_begin_execution": False,
            }
        ),
        crash=lambda: (
            calls.append("crash")
            or {
                "mode": "delayed",
                "running": False,
                "fresh": True,
                "held_model": False,
                "held_claim": False,
                "held_begin_execution": False,
            }
        ),
        hold=lambda fault: (
            calls.append(f"hold:{fault}")
            or {
                "mode": "delayed",
                "running": True,
                "fresh": True,
                "held_model": fault == "model",
                "held_claim": fault == "claim",
                "held_begin_execution": fault == "begin_execution",
            }
        ),
        release=lambda fault: (
            calls.append(f"release:{fault}")
            or {
                "mode": "delayed",
                "running": True,
                "fresh": True,
                "held_model": False,
                "held_claim": False,
                "held_begin_execution": False,
            }
        ),
    )
    router = build_replay_worker_router(controller)
    paths = {(route.path, frozenset(route.methods or ())) for route in router.routes}

    assert (
        "/api/test-only/replay-worker",
        frozenset({"GET"}),
    ) in paths
    assert (
        "/api/test-only/replay-worker/start",
        frozenset({"POST"}),
    ) in paths
    assert (
        "/api/test-only/replay-worker/stop",
        frozenset({"POST"}),
    ) in paths
    assert (
        "/api/test-only/replay-worker/crash",
        frozenset({"POST"}),
    ) in paths
    assert (
        "/api/test-only/replay-worker/faults/{fault}/{action}",
        frozenset({"POST"}),
    ) in paths
    assert calls == []

    application = FastAPI()
    application.include_router(router)
    client = TestClient(application)
    response = client.post(
        "/api/test-only/replay-worker/faults/model/hold",
        json={"barrier_root": "/private/test-path", "worker_id": "secret"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "mode": "delayed",
        "running": True,
        "fresh": True,
        "held_model": True,
        "held_claim": False,
        "held_begin_execution": False,
    }
    assert "private/test-path" not in response.text
    assert "secret" not in response.text
    assert (
        client.post(
            "/api/test-only/replay-worker/faults/model/delete",
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/test-only/replay-worker/faults/arbitrary/hold",
        ).status_code
        == 422
    )


def test_retry_safety_fault_route_is_exactly_scoped_and_has_no_value_input() -> None:
    from replay_agent_router import router

    paths = {(route.path, frozenset(route.methods or ())) for route in router.routes if "retry-safety" in route.path}

    assert paths == {
        (
            "/api/projects/{project_id}/test-only/threads/{thread_id}/runs/{run_id}/retry-safety/unknown",
            frozenset({"POST"}),
        )
    }


class _AsyncContext:
    def __init__(self, value=None) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args) -> None:
        return None


class _RetrySafetySession:
    def __init__(self, *, thread_id: str, run_id: str) -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self.job_id = uuid.uuid4()
        self.job = SimpleNamespace(retry_safety="safe")
        self.scalar_calls = 0

    def begin(self) -> _AsyncContext:
        return _AsyncContext()

    async def scalar(self, _statement):
        self.scalar_calls += 1
        if self.scalar_calls == 1:
            return self.thread_id
        if self.scalar_calls == 2:
            return SimpleNamespace(job_id=self.job_id, run_id=self.run_id)
        if self.scalar_calls == 3:
            return self.job
        raise AssertionError("unexpected retry-safety query")


@pytest.mark.asyncio
async def test_retry_safety_fault_changes_only_safe_current_private_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import replay_agent_router

    project_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    thread_id = "thread-current"
    run_id = "run-current"
    context = PrivateWorkContext.from_project(
        ProjectContext(
            user_id=owner_id,
            project_id=project_id,
            membership_id=uuid.uuid4(),
            role=ProjectRole.RUNNER,
            capabilities=frozenset(),
            membership_version=1,
            request_id="retry-safety-fault",
        )
    )
    session = _RetrySafetySession(
        thread_id=thread_id,
        run_id=run_id,
    )

    async def resolve(*_args, **_kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(
        replay_agent_router,
        "get_session_factory",
        lambda: lambda: _AsyncContext(session),
    )
    monkeypatch.setattr(
        replay_agent_router,
        "resolve_project_context_in_transaction",
        resolve,
    )

    response = await replay_agent_router.mark_replay_run_retry_safety_unknown(
        project_id,
        thread_id,
        run_id,
        context,
    )

    assert response.model_dump() == {
        "run_id": run_id,
        "retry_safety": "unknown",
    }
    assert session.job.retry_safety == "unknown"
    assert session.scalar_calls == 3


@pytest.mark.postgres
def test_replay_database_lifecycle_derives_and_drops_exact_disposable_database(
    postgres_admin_url: str,
) -> None:
    development_url = make_url(postgres_admin_url).set(database="deerflow")
    maintenance_url = development_url.set(
        drivername="postgresql",
        database="postgres",
    ).render_as_string(hide_password=False)

    database_name: str | None = None
    with replay_test_database_from_development(
        development_url.render_as_string(hide_password=False),
    ) as database:
        database_name = database.database_name
        assert database_name.startswith("deerflow_test_replay_")
        assert make_url(database.database_url).database == database_name
        with psycopg.connect(maintenance_url) as connection:
            assert connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=%s)",
                (database_name,),
            ).fetchone() == (True,)

    assert database_name is not None
    assert database.dropped is True
    with psycopg.connect(maintenance_url) as connection:
        assert connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=%s)",
            (database_name,),
        ).fetchone() == (False,)
