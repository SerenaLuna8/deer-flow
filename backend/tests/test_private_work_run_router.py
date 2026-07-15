from __future__ import annotations

import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from support.m4_private_threads import (
    M4ThreadSeed,
    install_open_project_cutover_guard,
    seed_m4_thread_database,
)

from app.gateway.deps import private_work_context
from app.gateway.routers import private_work as private_work_router
from app.private_work.checkpointer import PRIVATE_SCOPE_MARKER, ProjectScopedCheckpointer
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.runtime import DisconnectMode, RunRecord, RunStatus


@pytest_asyncio.fixture()
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


@dataclass
class _Harness:
    seed: M4ThreadSeed
    app: FastAPI
    raw: InMemorySaver
    scoped: ProjectScopedCheckpointer

    def path(self, suffix: str, *, project_id: uuid.UUID | None = None) -> str:
        selected = project_id or self.seed.owner_a.project_id
        return f"/api/projects/{selected}/private-work{suffix}"

    async def request(
        self,
        method: str,
        suffix: str,
        *,
        identity: str = "owner-a",
        project_id: uuid.UUID | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        ) as client:
            return await client.request(
                method,
                self.path(suffix, project_id=project_id),
                headers={"x-test-private-identity": identity},
                **kwargs,
            )


@pytest_asyncio.fixture()
async def harness(seed: M4ThreadSeed) -> _Harness:
    raw = InMemorySaver()
    scoped = ProjectScopedCheckpointer(raw, seed.factory)
    app = FastAPI()
    install_open_project_cutover_guard(app)
    app.include_router(private_work_router.router)
    app.state.private_run_service = PrivateRunService(seed.factory)
    app.state.project_scoped_checkpointer = scoped
    app.state.stream_bridge = object()
    app.state.run_manager = object()

    async def context_override(project_id: uuid.UUID, request: Request):
        identity = request.headers.get("x-test-private-identity", "owner-a")
        if identity == "owner-a":
            if project_id == seed.owner_a.project_id:
                return seed.owner_a
            if project_id == seed.project_b_owner_a.project_id:
                return seed.project_b_owner_a
        elif identity == "owner-b" and project_id == seed.owner_b.project_id:
            return seed.owner_b
        elif identity == "viewer" and project_id == seed.viewer.project_id:
            return seed.viewer
        raise HTTPException(status_code=404)

    app.dependency_overrides[private_work_context] = context_override
    return _Harness(seed=seed, app=app, raw=raw, scoped=scoped)


async def _seed_thread(
    seed: M4ThreadSeed,
    *,
    context,
    thread_id: str,
) -> None:
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=context.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(
                seed.project_b_agent_id if context.project_id == seed.project_b_owner_a.project_id else seed.project_agent_id,
                "project",
            ),
        )


async def _seed_run(
    seed: M4ThreadSeed,
    *,
    context,
    thread_id: str,
    status: str,
    metadata: dict | None = None,
    kwargs: dict | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    async with seed.factory() as session, session.begin():
        await PrivateRunRepository(session).create(
            scope=context.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(
                run_id=run_id,
                status=status,
                metadata=metadata or {},
                kwargs=kwargs or {},
                model_name="test-model",
            ),
        )
    return run_id


def _runtime_record(thread_id: str, *, task: object | None = None) -> RunRecord:
    return RunRecord(
        run_id=str(uuid.uuid4()),
        thread_id=thread_id,
        assistant_id=None,
        status=RunStatus.success,
        on_disconnect=DisconnectMode.cancel,
        metadata={"safe": "value"},
        kwargs={"credential": "must-not-serialize"},
        error=None,
        model_name="test-model",
        created_at="2026-07-15T00:00:00+00:00",
        updated_at="2026-07-15T00:00:01+00:00",
        task=task,  # type: ignore[arg-type]
    )


def test_private_work_router_exposes_minimum_run_lifecycle() -> None:
    routes = {(route.path, method) for route in private_work_router.router.routes for method in route.methods or ()}

    assert ("/api/projects/{project_id}/private-work/threads/{thread_id}/runs", "POST") in routes
    assert ("/api/projects/{project_id}/private-work/threads/{thread_id}/runs/wait", "POST") in routes
    assert ("/api/projects/{project_id}/private-work/threads/{thread_id}/runs", "GET") in routes
    assert ("/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}", "GET") in routes
    assert ("/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}", "DELETE") in routes


def test_public_private_run_strips_non_interactive() -> None:
    request = private_work_router.PrivateRunCreateRequest(
        input={"messages": [{"role": "user", "content": "hello"}]},
        context={
            "non_interactive": True,
            "project_id": str(uuid.uuid4()),
        },
        config={
            "context": {
                "non_interactive": True,
                "thinking_enabled": True,
            }
        },
    )

    assert request.context == {}
    assert request.config == {"context": {"thinking_enabled": True}}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_start_run_strips_nested_authority_and_serializes_no_private_coordinates(
    harness: _Harness,
    monkeypatch,
) -> None:
    thread_id = str(uuid.uuid4())
    await _seed_thread(harness.seed, context=harness.seed.owner_a, thread_id=thread_id)
    record = _runtime_record(thread_id)
    launcher = AsyncMock(return_value=record)
    monkeypatch.setattr(private_work_router, "start_private_run", launcher)

    response = await harness.request(
        "POST",
        f"/threads/{thread_id}/runs",
        json={
            "input": {"messages": [{"role": "user", "content": "run"}], "owner_user_id": "forged"},
            "command": {
                "resume": {
                    "role": "tool",
                    "project_id": "payload-project",
                }
            },
            "checkpoint": {
                "checkpoint_id": "payload-checkpoint",
                "owner_user_id": "payload-owner",
            },
            "metadata": {"safe": "value", "project_id": "forged"},
            "config": {"context": {"membership_id": "forged", "thinking_enabled": True}},
            "context": {"user_id": "forged", "thinking_enabled": False},
        },
    )

    assert response.status_code == 200
    body = launcher.await_args.args[0]
    assert body.input == {
        "messages": [{"role": "user", "content": "run"}],
        "owner_user_id": "forged",
    }
    assert body.command == {"resume": {"role": "tool", "project_id": "payload-project"}}
    assert body.checkpoint == {
        "checkpoint_id": "payload-checkpoint",
        "owner_user_id": "payload-owner",
    }
    assert body.metadata == {"safe": "value"}
    assert body.config == {"context": {"thinking_enabled": True}}
    assert body.context == {"thinking_enabled": False}
    payload = response.json()
    assert payload["run_id"] == record.run_id
    assert payload["thread_id"] == thread_id
    assert payload["status"] == "success"
    assert "project_id" not in payload
    assert "owner_user_id" not in payload
    assert "kwargs" not in payload
    assert "credential" not in str(payload).lower()

    invalid = await harness.request(
        "POST",
        f"/threads/{thread_id}/runs",
        json={"input": {}, "unexpected": True},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_wait_run_uses_bridge_helper_and_returns_final_scoped_values(
    harness: _Harness,
    monkeypatch,
) -> None:
    thread_id = str(uuid.uuid4())
    await _seed_thread(harness.seed, context=harness.seed.owner_a, thread_id=thread_id)
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"answer": "private final"}
    checkpoint["channel_versions"] = {"answer": "answer-v1"}
    await harness.raw.aput(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
        checkpoint,
        {
            "source": "loop",
            "step": 1,
            "parents": {},
            PRIVATE_SCOPE_MARKER: {
                "project_id": str(harness.seed.owner_a.project_id),
                "owner_user_id": str(harness.seed.owner_a.user_id),
            },
        },
        {"answer": "answer-v1"},
    )
    record = _runtime_record(thread_id, task=object())
    monkeypatch.setattr(private_work_router, "start_private_run", AsyncMock(return_value=record))
    waiter = AsyncMock(return_value=True)
    monkeypatch.setattr(private_work_router, "wait_for_run_completion", waiter)

    response = await harness.request("POST", f"/threads/{thread_id}/runs/wait", json={"input": {}})

    assert response.status_code == 200
    assert response.json() == {"answer": "private final"}
    waiter.assert_awaited_once_with(
        harness.app.state.stream_bridge,
        record,
        waiter.await_args.args[2],
        harness.app.state.run_manager,
    )

    record.status = RunStatus.running
    record.error = "client disconnected"
    waiter.return_value = False
    disconnected = await harness.request(
        "POST",
        f"/threads/{thread_id}/runs/wait",
        json={"input": {}},
    )
    assert disconnected.status_code == 200
    assert disconnected.json() == {
        "status": "running",
        "error": "client disconnected",
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_list_get_delete_are_thread_and_scope_bound(harness: _Harness) -> None:
    owner_thread = str(uuid.uuid4())
    owner_other_thread = str(uuid.uuid4())
    other_owner_thread = str(uuid.uuid4())
    other_project_thread = str(uuid.uuid4())
    for context, thread_id in (
        (harness.seed.owner_a, owner_thread),
        (harness.seed.owner_a, owner_other_thread),
        (harness.seed.owner_b, other_owner_thread),
        (harness.seed.project_b_owner_a, other_project_thread),
    ):
        await _seed_thread(harness.seed, context=context, thread_id=thread_id)

    terminal_run = await _seed_run(
        harness.seed,
        context=harness.seed.owner_a,
        thread_id=owner_thread,
        status="success",
        metadata={"safe": "listed"},
        kwargs={"credential_envelope": "secret"},
    )
    active_run = await _seed_run(
        harness.seed,
        context=harness.seed.owner_a,
        thread_id=owner_thread,
        status="running",
    )
    await _seed_run(
        harness.seed,
        context=harness.seed.owner_a,
        thread_id=owner_other_thread,
        status="success",
    )

    listed = await harness.request("GET", f"/threads/{owner_thread}/runs")
    assert listed.status_code == 200
    assert {item["run_id"] for item in listed.json()} == {terminal_run, active_run}
    assert all(item["thread_id"] == owner_thread for item in listed.json())
    assert all("kwargs" not in item for item in listed.json())

    fetched = await harness.request("GET", f"/threads/{owner_thread}/runs/{terminal_run}")
    assert fetched.status_code == 200
    assert fetched.json()["metadata"] == {"safe": "listed"}
    assert (
        await harness.request(
            "GET",
            f"/threads/{other_owner_thread}/runs/{terminal_run}",
            identity="owner-b",
        )
    ).status_code == 404
    assert (
        await harness.request(
            "GET",
            f"/threads/{other_project_thread}/runs/{terminal_run}",
            project_id=harness.seed.project_b_owner_a.project_id,
        )
    ).status_code == 404

    conflict = await harness.request("DELETE", f"/threads/{owner_thread}/runs/{active_run}")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "PRIVATE_WORK_CONFLICT"

    deleted = await harness.request("DELETE", f"/threads/{owner_thread}/runs/{terminal_run}")
    assert deleted.status_code == 200
    assert deleted.json() == {"success": True}
    assert (await harness.request("GET", f"/threads/{owner_thread}/runs/{terminal_run}")).status_code == 404


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_service_revalidates_membership_capability_and_runtime_dependencies(
    harness: _Harness,
) -> None:
    viewer_thread = str(uuid.uuid4())
    await _seed_thread(harness.seed, context=harness.seed.viewer, thread_id=viewer_thread)
    viewer_run = await _seed_run(
        harness.seed,
        context=harness.seed.viewer,
        thread_id=viewer_thread,
        status="success",
    )

    assert (await harness.request("GET", f"/threads/{viewer_thread}/runs", identity="viewer")).status_code == 200
    forbidden = await harness.request(
        "DELETE",
        f"/threads/{viewer_thread}/runs/{viewer_run}",
        identity="viewer",
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["code"] == "PRIVATE_WORK_FORBIDDEN"

    async with harness.seed.factory() as session, session.begin():
        await session.execute(
            text("UPDATE project_memberships SET status='removed', version=version+1 WHERE id=:membership_id"),
            {"membership_id": harness.seed.viewer.membership_id},
        )
    revoked = await harness.request(
        "GET",
        f"/threads/{viewer_thread}/runs",
        identity="viewer",
    )
    assert revoked.status_code == 404
    assert revoked.json()["detail"]["code"] == "PRIVATE_WORK_NOT_FOUND"

    harness.app.state.stream_bridge = None
    missing_runtime = await harness.request(
        "POST",
        f"/threads/{uuid.uuid4()}/runs/wait",
        json={"input": {}},
    )
    assert missing_runtime.status_code == 503
    assert missing_runtime.json()["detail"]["code"] == "PRIVATE_WORK_UNAVAILABLE"

    harness.app.state.private_run_service = None
    unavailable = await harness.request(
        "GET",
        f"/threads/{uuid.uuid4()}/runs",
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "PRIVATE_WORK_UNAVAILABLE"
