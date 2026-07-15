from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.gateway.deps import private_work_context
from app.gateway.routers.private_work import router
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.private_work.thread_service import PrivateThreadService


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

    def path(self, suffix: str, *, project_id: uuid.UUID | None = None) -> str:
        selected = project_id or self.seed.owner_a.project_id
        return f"/api/projects/{selected}/private-work{suffix}"

    async def request(
        self,
        method: str,
        suffix: str,
        *,
        identity: str = "owner-b",
        project_id: uuid.UUID | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        headers = {"x-test-private-identity": identity}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        ) as client:
            return await client.request(
                method,
                self.path(suffix, project_id=project_id),
                headers=headers,
                **kwargs,
            )


@pytest_asyncio.fixture()
async def harness(seed: M4ThreadSeed) -> _Harness:
    raw = InMemorySaver()
    scoped = ProjectScopedCheckpointer(raw, seed.factory)
    app = FastAPI()
    app.include_router(router)
    app.state.private_thread_service = PrivateThreadService(seed.factory, scoped)
    app.state.project_scoped_checkpointer = scoped

    async def context_override(project_id: uuid.UUID, request: Request):
        identity = request.headers.get("x-test-private-identity", "owner-b")
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
    return _Harness(seed=seed, app=app, raw=raw)


def _create_body(
    seed: M4ThreadSeed,
    *,
    thread_id: uuid.UUID | None = None,
    display_name: str = "Runnable Thread",
) -> dict[str, object]:
    return {
        "thread_id": str(thread_id or uuid.uuid4()),
        "agent_asset_id": str(seed.project_agent_id),
        "agent_scope": "project",
        "display_name": display_name,
        "metadata": {"topic": "private"},
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_runner_thread_crud_search_and_state(harness: _Harness) -> None:
    thread_id = uuid.uuid4()
    created = await harness.request(
        "POST",
        "/threads",
        json=_create_body(harness.seed, thread_id=thread_id),
    )
    assert created.status_code == 201
    assert created.json() == {
        "thread_id": str(thread_id),
        "agent_asset_id": str(harness.seed.project_agent_id),
        "agent_scope": "project",
        "display_name": "Runnable Thread",
        "status": "idle",
        "metadata": {"topic": "private"},
        "version": 1,
    }

    searched = await harness.request(
        "POST",
        "/threads/search",
        json={"limit": 20, "offset": 0},
    )
    assert searched.status_code == 200
    assert [item["thread_id"] for item in searched.json()["items"]] == [str(thread_id)]

    fetched = await harness.request("GET", f"/threads/{thread_id}")
    assert fetched.status_code == 200
    patched = await harness.request(
        "PATCH",
        f"/threads/{thread_id}",
        json={"expected_version": 1, "display_name": "Renamed"},
    )
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Renamed"
    assert patched.json()["version"] == 2

    state = await harness.request("GET", f"/threads/{thread_id}/state")
    assert state.status_code == 200
    assert state.json()["values"] == {}
    assert "deerflow_private_scope" not in state.json()["metadata"]

    deleted = await harness.request(
        "DELETE",
        f"/threads/{thread_id}?expected_version=2",
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"success": True}
    assert (await harness.request("GET", f"/threads/{thread_id}")).status_code == 404


async def _seed_viewer_thread(harness: _Harness, thread_id: uuid.UUID) -> None:
    seed = harness.seed
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.viewer.resource_scope,
            thread_id=str(thread_id),
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
            display_name="Migrated Viewer Thread",
        )
    checkpoint = empty_checkpoint()
    await harness.raw.aput(
        {
            "configurable": {
                "thread_id": str(thread_id),
                "checkpoint_ns": "",
            }
        },
        checkpoint,
        {
            "source": "input",
            "step": -1,
            "parents": {},
            "deerflow_private_scope": {
                "project_id": str(seed.viewer.project_id),
                "owner_user_id": str(seed.viewer.user_id),
            },
        },
        {},
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_viewer_reads_and_deletes_own_thread_but_cannot_create_or_patch(
    harness: _Harness,
) -> None:
    thread_id = uuid.uuid4()
    await _seed_viewer_thread(harness, thread_id)

    assert (await harness.request("GET", f"/threads/{thread_id}", identity="viewer")).status_code == 200
    assert (
        await harness.request(
            "POST",
            "/threads",
            identity="viewer",
            json=_create_body(harness.seed),
        )
    ).status_code == 403
    assert (
        await harness.request(
            "PATCH",
            f"/threads/{thread_id}",
            identity="viewer",
            json={"expected_version": 1, "display_name": "Forbidden"},
        )
    ).status_code == 403
    assert (
        await harness.request(
            "DELETE",
            f"/threads/{thread_id}?expected_version=1",
            identity="viewer",
        )
    ).status_code == 200


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_thread_routes_hide_other_owner_and_cross_project(
    harness: _Harness,
) -> None:
    thread_id = uuid.uuid4()
    assert (
        await harness.request(
            "POST",
            "/threads",
            identity="owner-a",
            json=_create_body(harness.seed, thread_id=thread_id),
        )
    ).status_code == 201

    assert (await harness.request("GET", f"/threads/{thread_id}", identity="owner-b")).status_code == 404
    assert (
        await harness.request(
            "GET",
            f"/threads/{thread_id}",
            identity="owner-a",
            project_id=harness.seed.project_b_owner_a.project_id,
        )
    ).status_code == 404


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_thread_routes_reject_invalid_uuid_and_extra_body_fields(
    harness: _Harness,
) -> None:
    bad_path = await harness.request("GET", "/threads/not-a-uuid")
    assert bad_path.status_code == 422
    assert bad_path.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"

    body = _create_body(harness.seed)
    body["owner_user_id"] = str(harness.seed.owner_a.user_id)
    bad_body = await harness.request("POST", "/threads", json=body)
    assert bad_body.status_code == 422
    assert bad_body.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"
