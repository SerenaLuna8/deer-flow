from __future__ import annotations

import json
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
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.reliability.error_mapping import (
    ReliabilityHTTPException,
    reliability_http_exception_handler,
)
from deerflow.runtime.events.models import StreamFrame
from deerflow.runtime.stream_bridge.postgres import PostgresStreamBridge


def _sse_data_payloads(transcript: str) -> list[object]:
    return [json.loads(line.removeprefix("data: ")) for line in transcript.splitlines() if line.startswith("data: ")]


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

    def path(
        self,
        thread_id: str,
        run_id: str,
        *,
        project_id: uuid.UUID | None = None,
    ) -> str:
        selected = project_id or self.seed.owner_a.project_id
        return f"/api/projects/{selected}/private-work/threads/{thread_id}/runs/{run_id}/stream"

    async def get(
        self,
        thread_id: str,
        run_id: str,
        *,
        identity: str = "owner-a",
        project_id: uuid.UUID | None = None,
        last_event_id: str | None = None,
    ) -> httpx.Response:
        headers = {"x-test-private-identity": identity}
        if last_event_id is not None:
            headers["Last-Event-ID"] = last_event_id
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        ) as client:
            return await client.get(
                self.path(thread_id, run_id, project_id=project_id),
                headers=headers,
            )


@pytest_asyncio.fixture()
async def harness(seed: M4ThreadSeed) -> _Harness:
    app = FastAPI()
    app.add_exception_handler(
        ReliabilityHTTPException,
        reliability_http_exception_handler,
    )
    install_open_project_cutover_guard(app)
    app.include_router(private_work_router.router)
    app.state.private_run_service = PrivateRunService(seed.factory)
    # A newly constructed bridge models a Gateway restart over the same DB.
    app.state.private_stream_bridge = PostgresStreamBridge(seed.factory)

    async def context_override(project_id: uuid.UUID, request: Request):
        identity = request.headers.get("x-test-private-identity", "owner-a")
        if identity == "owner-a" and project_id == seed.owner_a.project_id:
            return seed.owner_a
        if identity == "owner-b" and project_id == seed.owner_b.project_id:
            return seed.owner_b
        raise HTTPException(status_code=404)

    app.dependency_overrides[private_work_context] = context_override
    return _Harness(seed=seed, app=app)


async def _seed_private_stream(seed: M4ThreadSeed) -> tuple[str, str, tuple[str, str, str]]:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        await PrivateRunRepository(session).create(
            scope=seed.owner_a.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(run_id=run_id, status="running"),
        )

    writer = PostgresStreamBridge(seed.factory)
    first = await writer.publish_frame(
        seed.owner_a.resource_scope,
        thread_id,
        run_id,
        StreamFrame(event="updates", data={"delta": "first"}),
    )
    second = await writer.publish_frame(
        seed.owner_a.resource_scope,
        thread_id,
        run_id,
        StreamFrame(event="updates", data={"delta": "second"}),
    )
    terminal = await writer.publish_terminal(
        seed.owner_a.resource_scope,
        thread_id,
        run_id,
        status="completed",
    )
    async with seed.factory() as session, session.begin():
        assert await PrivateRunRepository(session).update_status(
            scope=seed.owner_a.resource_scope,
            run_id=run_id,
            status="success",
        )
    return thread_id, run_id, (first.id, second.id, terminal.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_replays_only_frames_after_last_event_id_across_gateway_restart(
    harness: _Harness,
) -> None:
    thread_id, run_id, (first_id, second_id, terminal_id) = await _seed_private_stream(
        harness.seed,
    )

    response = await harness.get(
        thread_id,
        run_id,
        last_event_id=first_id,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert f"id: {first_id}\n" not in response.text
    assert response.text.count(f"id: {second_id}\n") == 1
    assert response.text.count(f"id: {terminal_id}\n") == 1
    assert _sse_data_payloads(response.text) == [
        {"delta": "second"},
        {"status": "completed"},
    ]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_empty_cursor_means_zero_and_ahead_cursor_is_rejected(
    harness: _Harness,
) -> None:
    thread_id, run_id, (first_id, second_id, terminal_id) = await _seed_private_stream(
        harness.seed,
    )

    replay_all = await harness.get(thread_id, run_id, last_event_id="")
    ahead = await harness.get(
        thread_id,
        run_id,
        last_event_id=str(int(terminal_id) + 1),
    )

    assert replay_all.status_code == 200
    assert [replay_all.text.count(f"id: {event_id}\n") for event_id in (first_id, second_id, terminal_id)] == [1, 1, 1]
    assert ahead.status_code == 400
    assert ahead.json()["code"] == "INVALID_STREAM_CURSOR"


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cursor",
    [
        "-1",
        "+1",
        "01",
        "1.0",
        " 1",
        "abc",
        "9223372036854775808",
        "9" * 5_000,
    ],
)
async def test_rejects_noncanonical_last_event_id(
    harness: _Harness,
    cursor: str,
) -> None:
    thread_id, run_id, _ = await _seed_private_stream(harness.seed)

    response = await harness.get(
        thread_id,
        run_id,
        last_event_id=cursor,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_STREAM_CURSOR"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_private_stream_reconnect_is_owner_and_project_scoped(
    harness: _Harness,
) -> None:
    thread_id, run_id, _ = await _seed_private_stream(harness.seed)

    response = await harness.get(
        thread_id,
        run_id,
        identity="owner-b",
        project_id=harness.seed.owner_b.project_id,
        last_event_id="0",
    )

    assert response.status_code == 404
    assert "first" not in response.text


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_terminal_run_without_worker_frame_gets_one_durable_terminal_id(
    harness: _Harness,
) -> None:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    async with harness.seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=harness.seed.owner_a.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(harness.seed.project_agent_id, "project"),
        )
    admitted = await PrivateRunAdmissionService(harness.seed.factory).admit(
        harness.seed.owner_a,
        thread_id,
        PrivateRunCreate(
            run_id=run_id,
            kwargs={"input": {"messages": []}, "config": {}},
        ),
    )
    await PrivateRunService(harness.seed.factory).cancel(
        harness.seed.owner_a,
        thread_id,
        admitted.run.run_id,
    )

    response = await harness.get(thread_id, run_id, last_event_id="0")

    assert response.status_code == 200
    assert "event: end" in response.text
    assert "id: " in response.text
    frames = await PostgresStreamBridge(harness.seed.factory).read_after(
        harness.seed.owner_a.resource_scope,
        thread_id,
        cursor=0,
        limit=10,
        run_id=run_id,
    )
    assert len(frames) == 1
    assert frames[0].terminal is True
    assert response.text.count(f"id: {frames[0].id}\n") == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_replay_corrects_provisional_success_when_cancel_wins_settlement(
    harness: _Harness,
) -> None:
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
            request=PrivateRunCreate(run_id=run_id, status="running"),
        )
    writer = PostgresStreamBridge(harness.seed.factory)
    provisional = await writer.publish_terminal(
        harness.seed.owner_a.resource_scope,
        thread_id,
        run_id,
        status="success",
    )
    async with harness.seed.factory() as session, session.begin():
        assert await PrivateRunRepository(session).update_status(
            scope=harness.seed.owner_a.resource_scope,
            run_id=run_id,
            status="interrupted",
        )

    response = await harness.get(thread_id, run_id, last_event_id="0")

    assert response.status_code == 200
    assert f"id: {provisional.id}\n" in response.text
    assert _sse_data_payloads(response.text) == [{"status": "interrupted"}]
    frames = await writer.read_after(
        harness.seed.owner_a.resource_scope,
        thread_id,
        cursor=0,
        limit=10,
        run_id=run_id,
    )
    assert len(frames) == 1
    assert frames[0].id == provisional.id
    assert frames[0].data == {"status": "interrupted"}
