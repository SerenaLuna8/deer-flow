from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.gateway.deps import private_work_context, require_project_private_open
from app.gateway.routers import private_work as private_work_router
from app.private_work.feedback_service import PrivateFeedbackService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.persistence.models.run_event import ThreadEventSequenceRow
from deerflow.persistence.run import RunRepository
from deerflow.runtime.events.store.db import DbRunEventStore

_INVALID_REST_FEED_CURSORS = (
    pytest.param("01", id="leading-zero"),
    pytest.param("+1", id="explicit-plus"),
    pytest.param("1.0", id="decimal"),
    pytest.param(" 1", id="leading-space"),
    pytest.param("1 ", id="trailing-space"),
    pytest.param("-0", id="negative-zero"),
    pytest.param("-1", id="negative"),
    pytest.param("١", id="unicode-digit"),
    pytest.param("", id="empty"),
    pytest.param("9223372036854775808", id="above-postgres-bigint"),
    pytest.param("9" * 5_000, id="overlong"),
)


def test_private_work_router_exposes_project_chat_feed_matrix() -> None:
    routes = {(route.path, method) for route in private_work_router.router.routes for method in route.methods or ()}

    prefix = "/api/projects/{project_id}/private-work/threads/{thread_id}"
    assert (f"{prefix}/messages", "GET") in routes
    assert (f"{prefix}/runs/{{run_id}}/messages", "GET") in routes
    assert (f"{prefix}/runs/{{run_id}}/events", "GET") in routes
    assert (f"{prefix}/events", "GET") in routes
    assert (f"{prefix}/token-usage", "GET") in routes
    assert (f"{prefix}/runs/{{run_id}}/feedback", "GET") in routes
    assert (f"{prefix}/runs/{{run_id}}/feedback", "PUT") in routes
    assert (f"{prefix}/runs/{{run_id}}/feedback", "DELETE") in routes
    assert (f"{prefix}/runs/{{run_id}}/feedback", "POST") in routes


@pytest.mark.parametrize(
    ("path", "cursor_names"),
    (
        (
            "/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}/messages",
            ("before_seq", "after_seq"),
        ),
        (
            "/api/projects/{project_id}/private-work/threads/{thread_id}/messages",
            ("before_seq", "after_seq"),
        ),
        (
            "/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}/events",
            ("after_seq",),
        ),
        (
            "/api/projects/{project_id}/private-work/threads/{thread_id}/events",
            ("after_seq",),
        ),
    ),
)
def test_private_work_rest_feed_cursors_are_openapi_strings(
    path: str,
    cursor_names: tuple[str, ...],
) -> None:
    app = FastAPI()
    app.include_router(private_work_router.router)
    parameters = {parameter["name"]: parameter for parameter in app.openapi()["paths"][path]["get"]["parameters"]}

    for cursor_name in cursor_names:
        variants = parameters[cursor_name]["schema"]["anyOf"]
        assert {variant["type"] for variant in variants} == {"string", "null"}


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
    app.include_router(private_work_router.router)
    event_store = DbRunEventStore(seed.factory)
    run_store = RunRepository(seed.factory)
    app.state.private_run_service = PrivateRunService(seed.factory)
    app.state.private_run_event_store = event_store
    app.state.run_store = run_store
    app.state.private_feedback_service = PrivateFeedbackService(seed.factory)
    app.dependency_overrides[require_project_private_open] = lambda: None

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
    turn_duration = messages.json()[0]["content"]["additional_kwargs"]["turn_duration"]
    assert type(turn_duration) is int and turn_duration >= 0
    assert [item["content"] for item in messages.json()] == [
        {
            "type": "ai",
            "content": "hello",
            "additional_kwargs": {"turn_duration": turn_duration},
        }
    ]

    run_messages_path = f"/threads/{thread_id}/runs/{run_id}/messages"
    run_messages = await harness.request("GET", run_messages_path)
    assert run_messages.status_code == 200
    assert run_messages.json() == {
        "data": [
            {
                "run_id": run_id,
                "seq": "1",
                "content": {
                    "type": "ai",
                    "content": "hello",
                    "additional_kwargs": {"turn_duration": turn_duration},
                },
                "metadata": {"content_is_dict": True, "content_is_json": True},
                "created_at": run_messages.json()["data"][0]["created_at"],
            }
        ],
        "has_more": False,
    }

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

    feedback_path = f"/threads/{thread_id}/runs/{run_id}/feedback"
    empty_feedback = await harness.request("GET", feedback_path)
    assert empty_feedback.status_code == 200
    assert empty_feedback.json() is None

    feedback = await harness.request(
        "PUT",
        feedback_path,
        json={"rating": 1, "comment": "useful", "message_id": "message-1"},
    )
    assert feedback.status_code == 200
    feedback_id = feedback.json()["feedback_id"]
    assert feedback.json()["rating"] == 1
    assert feedback.json()["comment"] == "useful"
    assert feedback.json()["message_id"] == "message-1"
    assert "project_id" not in feedback.json()
    assert "owner_user_id" not in feedback.json()

    persisted = await harness.request("GET", feedback_path)
    assert persisted.status_code == 200
    assert persisted.json() == feedback.json()

    updated = await harness.request(
        "PUT",
        feedback_path,
        json={"rating": -1, "comment": None, "message_id": "message-2"},
    )
    assert updated.status_code == 200
    assert updated.json()["feedback_id"] == feedback_id
    assert updated.json()["rating"] == -1
    assert updated.json()["message_id"] == "message-2"

    compatibility = await harness.request(
        "POST",
        feedback_path,
        json={"rating": 1, "comment": "legacy client"},
    )
    assert compatibility.status_code == 201
    assert compatibility.json()["feedback_id"] == feedback_id
    assert compatibility.json()["rating"] == 1

    deleted_feedback = await harness.request("DELETE", feedback_path)
    assert deleted_feedback.status_code == 204
    assert deleted_feedback.content == b""
    assert (await harness.request("GET", feedback_path)).json() is None

    for method, suffix, kwargs in (
        ("GET", f"/threads/{thread_id}/messages", {}),
        ("GET", run_messages_path, {}),
        ("GET", events_path, {}),
        ("GET", f"/threads/{thread_id}/events?run_id={run_id}", {}),
        ("GET", f"/threads/{thread_id}/token-usage", {}),
        (
            "GET",
            feedback_path,
            {},
        ),
        (
            "PUT",
            feedback_path,
            {"json": {"rating": -1}},
        ),
        ("DELETE", feedback_path, {}),
        ("POST", feedback_path, {"json": {"rating": -1}}),
    ):
        hidden = await harness.request(
            method,
            suffix,
            identity="owner-b",
            **kwargs,
        )
        assert hidden.status_code == 404
        assert hidden.json()["detail"]["code"] == "PRIVATE_WORK_NOT_FOUND"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_message_feeds_stamp_duration_only_on_each_run_final_ai(
    harness: _Harness,
) -> None:
    thread_id, run_id = await _seed_thread_and_run(harness)
    await harness.event_store.put(
        thread_id=thread_id,
        run_id=run_id,
        event_type="llm.ai.response",
        category="message",
        content={"type": "ai", "content": "final answer"},
        scope=harness.seed.owner_a.resource_scope,
    )

    thread_response = await harness.request(
        "GET",
        f"/threads/{thread_id}/messages",
    )
    run_response = await harness.request(
        "GET",
        f"/threads/{thread_id}/runs/{run_id}/messages",
    )

    assert thread_response.status_code == 200
    assert run_response.status_code == 200
    for messages in (
        thread_response.json(),
        run_response.json()["data"],
    ):
        assert "additional_kwargs" not in messages[0]["content"]
        duration = messages[-1]["content"]["additional_kwargs"]["turn_duration"]
        assert type(duration) is int and duration >= 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_failed_run_messages_recover_visible_admitted_prompt_before_graph_execution(
    harness: _Harness,
) -> None:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    visible_prompt = {
        "type": "human",
        "content": [{"type": "text", "text": "只回复四个字：验收成功"}],
        "additional_kwargs": {
            "files": [
                {
                    "filename": "acceptance.txt",
                    "path": "/uploads/acceptance.txt",
                    "size": 42,
                    "status": "uploaded",
                }
            ],
            "credential_token": "must-not-be-returned",
        },
        "client_runtime_context": {"must_not": "be_returned"},
    }
    async with harness.seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=harness.seed.owner_a.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(harness.seed.project_agent_id, "project"),
        )
        admitted = await PrivateRunRepository(session).create(
            scope=harness.seed.owner_a.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(
                run_id=run_id,
                status="error",
                kwargs={
                    "input": {
                        "messages": [
                            {
                                "type": "human",
                                "content": "private sidecar context",
                                "additional_kwargs": {"hide_from_ui": True},
                            },
                            visible_prompt,
                        ]
                    }
                },
            ),
        )

    response = await harness.request(
        "GET",
        f"/threads/{thread_id}/runs/{run_id}/messages",
    )

    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {
                "run_id": run_id,
                "seq": "0",
                "content": {
                    "type": "human",
                    "id": f"run-admission-{run_id}",
                    "content": visible_prompt["content"],
                    "additional_kwargs": {
                        "files": visible_prompt["additional_kwargs"]["files"],
                    },
                },
                "metadata": {"source": "run_admission"},
                "created_at": admitted.created_at.isoformat(),
            }
        ],
        "has_more": False,
    }

    after_admission = await harness.request(
        "GET",
        f"/threads/{thread_id}/runs/{run_id}/messages?after_seq=0",
    )
    assert after_admission.status_code == 200
    assert after_admission.json() == {"data": [], "has_more": False}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_viewer_can_read_own_feedback_but_cannot_mutate_it(
    harness: _Harness,
) -> None:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    async with harness.seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=harness.seed.viewer.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(harness.seed.project_agent_id, "project"),
        )
        await PrivateRunRepository(session).create(
            scope=harness.seed.viewer.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(run_id=run_id, status="success"),
        )

    async def viewer_context_override(project_id: uuid.UUID, request: Request):
        identity = request.headers.get("x-test-private-identity", "owner-a")
        if identity == "viewer" and project_id == harness.seed.viewer.project_id:
            return harness.seed.viewer
        if identity == "owner-a" and project_id == harness.seed.owner_a.project_id:
            return harness.seed.owner_a
        raise HTTPException(status_code=404)

    harness.app.dependency_overrides[private_work_context] = viewer_context_override
    path = f"/threads/{thread_id}/runs/{run_id}/feedback"
    readable = await harness.request("GET", path, identity="viewer")
    assert readable.status_code == 200
    assert readable.json() is None

    for method, kwargs in (
        ("PUT", {"json": {"rating": 1}}),
        ("POST", {"json": {"rating": 1}}),
        ("DELETE", {}),
    ):
        forbidden = await harness.request(
            method,
            path,
            identity="viewer",
            **kwargs,
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"]["code"] == "PRIVATE_WORK_FORBIDDEN"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_run_messages_paginate_without_crossing_run_or_owner_boundaries(
    harness: _Harness,
) -> None:
    thread_id, run_id = await _seed_thread_and_run(harness)
    await harness.event_store.put_batch(
        [
            {
                "thread_id": thread_id,
                "run_id": run_id,
                "event_type": "llm.ai.response",
                "category": "message",
                "content": {"type": "ai", "content": f"message-{index}"},
                "metadata": {"caller": "lead_agent"},
            }
            for index in range(2, 6)
        ],
        scope=harness.seed.owner_a.resource_scope,
    )

    latest = await harness.request(
        "GET",
        f"/threads/{thread_id}/runs/{run_id}/messages?limit=2",
    )
    assert latest.status_code == 200
    assert latest.json()["has_more"] is True
    assert [item["seq"] for item in latest.json()["data"]] == ["5", "6"]

    older = await harness.request(
        "GET",
        f"/threads/{thread_id}/runs/{run_id}/messages?limit=2&before_seq=5",
    )
    assert older.status_code == 200
    assert older.json()["has_more"] is True
    assert [item["seq"] for item in older.json()["data"]] == ["3", "4"]

    newer = await harness.request(
        "GET",
        f"/threads/{thread_id}/runs/{run_id}/messages?limit=2&after_seq=3",
    )
    assert newer.status_code == 200
    assert newer.json()["has_more"] is True
    assert [item["seq"] for item in newer.json()["data"]] == ["4", "5"]

    both_cursors = await harness.request(
        "GET",
        f"/threads/{thread_id}/runs/{run_id}/messages?before_seq=5&after_seq=3",
    )
    assert both_cursors.status_code == 422
    assert both_cursors.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"

    thread_both_cursors = await harness.request(
        "GET",
        f"/threads/{thread_id}/messages?before_seq=5&after_seq=3",
    )
    assert thread_both_cursors.status_code == 422
    assert thread_both_cursors.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"

    other_thread_id, other_run_id = await _seed_thread_and_run(harness)
    mismatched = await harness.request(
        "GET",
        f"/threads/{thread_id}/runs/{other_run_id}/messages",
    )
    assert mismatched.status_code == 404
    assert mismatched.json()["detail"]["code"] == "PRIVATE_WORK_NOT_FOUND"

    other_owner = await harness.request(
        "GET",
        f"/threads/{other_thread_id}/runs/{other_run_id}/messages",
        identity="owner-b",
    )
    assert other_owner.status_code == 404
    assert other_owner.json()["detail"]["code"] == "PRIVATE_WORK_NOT_FOUND"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_event_rest_cursor_preserves_values_above_javascript_safe_integer(
    harness: _Harness,
) -> None:
    thread_id, run_id = await _seed_thread_and_run(harness)
    previous_sequence = 9_007_199_254_740_992
    async with harness.seed.factory() as session, session.begin():
        await session.execute(
            sa.update(ThreadEventSequenceRow)
            .where(
                ThreadEventSequenceRow.project_id == harness.seed.owner_a_scope.project_id,
                ThreadEventSequenceRow.owner_user_id == harness.seed.owner_a_scope.owner_user_id,
                ThreadEventSequenceRow.thread_id == thread_id,
            )
            .values(high_watermark=previous_sequence)
        )
    written = await harness.event_store.put(
        thread_id=thread_id,
        run_id=run_id,
        event_type="subagent.step",
        category="trace",
        content={"task_id": "task-a", "message_index": 0},
        metadata={"task_id": "task-a"},
        scope=harness.seed.owner_a_scope,
    )
    assert written["seq"] == previous_sequence + 1

    response = await harness.request(
        "GET",
        (f"/threads/{thread_id}/runs/{run_id}/events?after_seq={previous_sequence}"),
    )

    assert response.status_code == 200
    assert [event["seq"] for event in response.json()] == ["9007199254740993"]

    out_of_range = await harness.request(
        "GET",
        (f"/threads/{thread_id}/runs/{run_id}/events?after_seq=9223372036854775808"),
    )
    assert out_of_range.status_code == 422


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("raw_cursor", _INVALID_REST_FEED_CURSORS)
async def test_private_work_rest_feed_cursors_reject_noncanonical_decimal_strings(
    harness: _Harness,
    raw_cursor: str,
) -> None:
    thread_id, run_id = await _seed_thread_and_run(harness)
    feed_cursors = (
        (
            f"/threads/{thread_id}/runs/{run_id}/messages",
            "before_seq",
            {},
        ),
        (
            f"/threads/{thread_id}/runs/{run_id}/messages",
            "after_seq",
            {},
        ),
        (
            f"/threads/{thread_id}/messages",
            "before_seq",
            {},
        ),
        (
            f"/threads/{thread_id}/messages",
            "after_seq",
            {},
        ),
        (
            f"/threads/{thread_id}/runs/{run_id}/events",
            "after_seq",
            {},
        ),
        (
            f"/threads/{thread_id}/events",
            "after_seq",
            {"run_id": run_id},
        ),
    )

    for suffix, cursor_name, base_params in feed_cursors:
        response = await harness.request(
            "GET",
            suffix,
            params={
                **base_params,
                cursor_name: raw_cursor,
            },
        )
        assert response.status_code == 422, (
            suffix,
            cursor_name,
            raw_cursor,
            response.text,
        )
        assert response.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_private_work_rest_feed_cursors_accept_canonical_bigint_strings(
    harness: _Harness,
) -> None:
    thread_id, run_id = await _seed_thread_and_run(harness)
    feeds = (
        (f"/threads/{thread_id}/runs/{run_id}/messages", "before_seq", {}),
        (f"/threads/{thread_id}/runs/{run_id}/messages", "after_seq", {}),
        (f"/threads/{thread_id}/messages", "before_seq", {}),
        (f"/threads/{thread_id}/messages", "after_seq", {}),
        (f"/threads/{thread_id}/runs/{run_id}/events", "after_seq", {}),
        (f"/threads/{thread_id}/events", "after_seq", {"run_id": run_id}),
    )

    for raw_cursor in ("0", "9007199254740993", "9223372036854775807"):
        for suffix, cursor_name, base_params in feeds:
            response = await harness.request(
                "GET",
                suffix,
                params={
                    **base_params,
                    cursor_name: raw_cursor,
                },
            )
            assert response.status_code == 200, (
                suffix,
                cursor_name,
                raw_cursor,
                response.text,
            )
