from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from support.m4_private_threads import (
    M4ThreadSeed,
    install_open_project_cutover_guard,
    seed_m4_thread_database,
)

from app.gateway.deps import private_work_context
from app.gateway.routers import private_work as private_work_router
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.persistence.feedback import FeedbackRepository
from deerflow.persistence.run import RunRepository
from deerflow.runtime.events.store.db import DbRunEventStore


def test_private_work_router_exposes_project_chat_feed_matrix() -> None:
    routes = {(route.path, method) for route in private_work_router.router.routes for method in route.methods or ()}

    prefix = "/api/projects/{project_id}/private-work/threads/{thread_id}"
    assert (f"{prefix}/messages", "GET") in routes
    assert (f"{prefix}/runs/{{run_id}}/events", "GET") in routes
    assert (f"{prefix}/events", "GET") in routes
    assert (f"{prefix}/token-usage", "GET") in routes
    assert (f"{prefix}/runs/{{run_id}}/feedback", "POST") in routes


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
    event_store: DbRunEventStore
    run_store: RunRepository

    async def request(
        self,
        method: str,
        suffix: str,
        *,
        identity: str = "owner-a",
        **kwargs: object,
    ) -> httpx.Response:
        path = f"/api/projects/{self.seed.owner_a.project_id}/private-work{suffix}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        ) as client:
            return await client.request(
                method,
                path,
                headers={"x-test-private-identity": identity},
                **kwargs,
            )


@pytest_asyncio.fixture()
async def harness(seed: M4ThreadSeed) -> _Harness:
    app = FastAPI()
    install_open_project_cutover_guard(app)
    app.include_router(private_work_router.router)
    event_store = DbRunEventStore(seed.factory)
    run_store = RunRepository(seed.factory)
    app.state.private_run_service = PrivateRunService(seed.factory)
    app.state.private_run_event_store = event_store
    app.state.run_store = run_store
    app.state.feedback_repo = FeedbackRepository(seed.factory)

    async def context_override(project_id: uuid.UUID, request: Request):
        identity = request.headers.get("x-test-private-identity", "owner-a")
        if identity == "owner-a" and project_id == seed.owner_a.project_id:
            return seed.owner_a
        if identity == "owner-b" and project_id == seed.owner_b.project_id:
            return seed.owner_b
        raise HTTPException(status_code=404)

    app.dependency_overrides[private_work_context] = context_override
    return _Harness(
        seed=seed,
        app=app,
        event_store=event_store,
        run_store=run_store,
    )


async def _seed_thread_and_run(harness: _Harness) -> tuple[str, str]:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    async with harness.seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=harness.seed.owner_a.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(harness.seed.project_agent_id, "project"),
        )
        await PrivateRunRepository(session).create(
            scope=harness.seed.owner_a.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(
                run_id=run_id,
                status="success",
                model_name="test-model",
            ),
        )
    await harness.run_store.update_run_completion(
        run_id,
        status="success",
        total_input_tokens=3,
        total_output_tokens=5,
        total_tokens=8,
        lead_agent_tokens=8,
        token_usage_by_model={"test-model": {"total_tokens": 8}},
        scope=harness.seed.owner_a.resource_scope,
    )
    await harness.event_store.put_batch(
        [
            {
                "thread_id": thread_id,
                "run_id": run_id,
                "event_type": "llm.ai.response",
                "category": "message",
                "content": {"type": "ai", "content": "hello"},
            },
            {
                "thread_id": thread_id,
                "run_id": run_id,
                "event_type": "run.completed",
                "category": "lifecycle",
                "content": "done",
            },
        ],
        scope=harness.seed.owner_a.resource_scope,
    )
    return thread_id, run_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_chat_feed_happy_path_and_cross_owner_404(
    harness: _Harness,
) -> None:
    thread_id, run_id = await _seed_thread_and_run(harness)

    messages = await harness.request("GET", f"/threads/{thread_id}/messages")
    assert messages.status_code == 200
    assert [item["content"] for item in messages.json()] == [{"type": "ai", "content": "hello"}]

    events_path = f"/threads/{thread_id}/runs/{run_id}/events"
    events = await harness.request("GET", events_path)
    assert events.status_code == 200
    assert [item["event_type"] for item in events.json()] == [
        "llm.ai.response",
        "run.completed",
    ]
    alias = await harness.request(
        "GET",
        f"/threads/{thread_id}/events?run_id={run_id}",
    )
    assert alias.status_code == 200
    assert alias.json() == events.json()

    usage = await harness.request("GET", f"/threads/{thread_id}/token-usage")
    assert usage.status_code == 200
    assert usage.json()["total_tokens"] == 8
    assert usage.json()["by_caller"] == {
        "lead_agent": 8,
        "subagent": 0,
        "middleware": 0,
    }

    feedback = await harness.request(
        "POST",
        f"/threads/{thread_id}/runs/{run_id}/feedback",
        json={"rating": 1, "comment": "useful", "message_id": "message-1"},
    )
    assert feedback.status_code == 201
    assert feedback.json()["rating"] == 1
    assert feedback.json()["comment"] == "useful"
    assert "project_id" not in feedback.json()
    assert "owner_user_id" not in feedback.json()

    for method, suffix, kwargs in (
        ("GET", f"/threads/{thread_id}/messages", {}),
        ("GET", events_path, {}),
        ("GET", f"/threads/{thread_id}/events?run_id={run_id}", {}),
        ("GET", f"/threads/{thread_id}/token-usage", {}),
        (
            "POST",
            f"/threads/{thread_id}/runs/{run_id}/feedback",
            {"json": {"rating": -1}},
        ),
    ):
        hidden = await harness.request(
            method,
            suffix,
            identity="owner-b",
            **kwargs,
        )
        assert hidden.status_code == 404
        assert hidden.json()["detail"]["code"] == "PRIVATE_WORK_NOT_FOUND"
