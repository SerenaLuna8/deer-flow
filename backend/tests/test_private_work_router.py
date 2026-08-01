from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.gateway.deps import get_config, private_work_context, project_session
from app.gateway.routers.private_work import (
    PrivateThreadCreateRequest,
    _public_event,
    _run_message_response,
    _thread_response,
    router,
)
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import (
    PrivateThreadRecord,
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.private_work.thread_service import PrivateThreadService
from deerflow.config.app_config import AppConfig


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
    app.state.private_run_service = PrivateRunService(seed.factory)
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

    async def session_override():
        async with seed.factory() as session:
            yield session

    app.dependency_overrides[private_work_context] = context_override
    app.dependency_overrides[project_session] = session_override
    app.dependency_overrides[get_config] = lambda: AppConfig.model_validate(
        {
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
            },
        }
    )
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


def test_thread_create_request_accepts_explicit_or_default_agent_selection() -> None:
    thread_id = uuid.uuid4()
    default_selection = PrivateThreadCreateRequest.model_validate({"thread_id": str(thread_id)})
    assert default_selection.agent_asset_id is None
    assert default_selection.agent_scope is None

    agent_id = uuid.uuid4()
    explicit_selection = PrivateThreadCreateRequest.model_validate(
        {
            "thread_id": str(thread_id),
            "agent_asset_id": str(agent_id),
            "agent_scope": "project",
        }
    )
    assert explicit_selection.agent_asset_id == agent_id
    assert explicit_selection.agent_scope == "project"


@pytest.mark.parametrize(
    "partial_selection",
    [
        {"agent_asset_id": str(uuid.uuid4())},
        {"agent_scope": "project"},
        {"agent_asset_id": None},
        {"agent_scope": None},
        {"agent_asset_id": None, "agent_scope": None},
    ],
)
def test_thread_create_request_rejects_partial_agent_selection(
    partial_selection: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        PrivateThreadCreateRequest.model_validate({"thread_id": str(uuid.uuid4()), **partial_selection})


def test_thread_response_preserves_record_timestamps() -> None:
    created_at = datetime(2026, 7, 20, 8, 30, tzinfo=UTC)
    updated_at = datetime(2026, 7, 21, 9, 45, tzinfo=UTC)
    record = PrivateThreadRecord(
        thread_id=str(uuid.uuid4()),
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        agent_asset_id=uuid.uuid4(),
        agent_scope="project",
        display_name="Timestamped Thread",
        status="idle",
        metadata={},
        frozen_at=None,
        deleted_at=None,
        checkpoint_delete_status="not_requested",
        version=1,
        created_at=created_at,
        updated_at=updated_at,
    )

    response = _thread_response(record)

    assert response.model_dump()["created_at"] == created_at.isoformat()
    assert response.model_dump()["updated_at"] == updated_at.isoformat()


def test_rest_event_sequences_are_canonical_decimal_strings() -> None:
    sequence = 9_007_199_254_740_993
    record = {
        "thread_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "event_type": "llm.ai.response",
        "category": "message",
        "content": {"type": "ai", "content": "hello"},
        "metadata": {},
        "seq": sequence,
        "created_at": "2026-07-30T00:00:00+00:00",
    }

    assert _public_event(record)["seq"] == "9007199254740993"
    assert _run_message_response(record).model_dump()["seq"] == "9007199254740993"


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
    created_payload = created.json()
    created_at = created_payload.pop("created_at")
    updated_at = created_payload.pop("updated_at")
    assert datetime.fromisoformat(created_at).tzinfo is not None
    assert updated_at == created_at
    assert created_payload == {
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
    searched_item = searched.json()["items"][0]
    assert searched_item["thread_id"] == str(thread_id)
    assert searched_item["created_at"] == created_at
    assert searched_item["updated_at"] == updated_at

    fetched = await harness.request("GET", f"/threads/{thread_id}")
    assert fetched.status_code == 200
    assert fetched.json()["created_at"] == created_at
    assert fetched.json()["updated_at"] == updated_at
    patched = await harness.request(
        "PATCH",
        f"/threads/{thread_id}",
        json={"expected_version": 1, "display_name": "Renamed"},
    )
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Renamed"
    assert patched.json()["version"] == 2
    assert patched.json()["created_at"] == created_at
    assert datetime.fromisoformat(patched.json()["updated_at"]) >= datetime.fromisoformat(updated_at)

    state = await harness.request("GET", f"/threads/{thread_id}/state")
    assert state.status_code == 200
    assert state.json()["values"] == {
        "artifacts": [],
        "delegations": [],
        "messages": [],
        "skill_context": [],
        "viewed_images": {},
    }
    assert "deerflow_private_scope" not in state.json()["metadata"]

    deleted = await harness.request(
        "DELETE",
        f"/threads/{thread_id}?expected_version=2",
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"success": True}
    assert (await harness.request("GET", f"/threads/{thread_id}")).status_code == 404


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_runner_implicit_thread_create_resolves_configured_project_default(
    harness: _Harness,
) -> None:
    from deerflow.persistence.projects import ProjectDefaultAgentRow

    async with harness.seed.factory() as session:
        async with session.begin():
            session.add(
                ProjectDefaultAgentRow(
                    project_id=harness.seed.owner_a.project_id,
                    agent_asset_id=harness.seed.project_agent_id,
                    revision=1,
                    created_by_user_id=str(harness.seed.owner_a.user_id),
                    updated_by_user_id=str(harness.seed.owner_a.user_id),
                )
            )

    response = await harness.request(
        "POST",
        "/threads",
        identity="owner-a",
        json={"thread_id": str(uuid.uuid4()), "display_name": "Default Agent"},
    )

    assert response.status_code == 201
    assert response.json()["agent_asset_id"] == str(harness.seed.project_agent_id)
    assert response.json()["agent_scope"] == "project"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_runner_implicit_thread_create_falls_back_to_builtin_main(
    harness: _Harness,
) -> None:
    response = await harness.request(
        "POST",
        "/threads",
        identity="owner-a",
        json={"thread_id": str(uuid.uuid4())},
    )

    assert response.status_code == 201
    assert response.json()["agent_asset_id"] == str(harness.seed.system_agent_id)
    assert response.json()["agent_scope"] == "system"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_runner_implicit_thread_create_returns_stable_conflict_for_unavailable_default(
    harness: _Harness,
) -> None:
    from deerflow.persistence.projects import ProjectDefaultAgentRow
    from deerflow.persistence.shared_assets import AgentRow

    async with harness.seed.factory() as session:
        async with session.begin():
            session.add(
                ProjectDefaultAgentRow(
                    project_id=harness.seed.owner_a.project_id,
                    agent_asset_id=harness.seed.project_agent_id,
                    revision=1,
                    created_by_user_id=str(harness.seed.owner_a.user_id),
                    updated_by_user_id=str(harness.seed.owner_a.user_id),
                )
            )
            agent = await session.get(AgentRow, harness.seed.project_agent_id)
            assert agent is not None
            agent.status = "suspended"

    response = await harness.request(
        "POST",
        "/threads",
        identity="owner-a",
        json={"thread_id": str(uuid.uuid4())},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "DEFAULT_AGENT_UNAVAILABLE",
        "message": "The project default Agent is unavailable.",
        "request_id": harness.seed.owner_a.request_id,
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_thread_state_projects_duration_only_to_the_turn_final_ai(
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
    run_id = str(uuid.uuid4())
    async with harness.seed.factory() as session, session.begin():
        await PrivateRunRepository(session).create(
            scope=harness.seed.owner_a.resource_scope,
            thread_id=str(thread_id),
            request=PrivateRunCreate(run_id=run_id, status="success"),
        )

    saver = harness.app.state.project_scoped_checkpointer.for_context(harness.seed.owner_a)
    root = await saver.aget_tuple(
        {
            "configurable": {
                "thread_id": str(thread_id),
                "checkpoint_ns": "",
            }
        }
    )
    assert root is not None
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(
                id="human-duration",
                content="question",
                additional_kwargs={"run_id": run_id},
            ),
            AIMessage(id="ai-progress", content="progress"),
            AIMessage(id="ai-final", content="final"),
        ]
    }
    checkpoint["channel_versions"] = {"messages": checkpoint["id"]}
    await saver.aput(
        root.config,
        checkpoint,
        {"source": "loop", "step": 1, "parents": {}},
        {"messages": checkpoint["id"]},
    )

    response = await harness.request(
        "GET",
        f"/threads/{thread_id}/state",
        identity="owner-a",
    )

    assert response.status_code == 200
    messages = response.json()["values"]["messages"]
    assert "turn_duration" not in messages[1].get("additional_kwargs", {})
    duration = messages[2]["additional_kwargs"]["turn_duration"]
    assert type(duration) is int and duration >= 0


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
