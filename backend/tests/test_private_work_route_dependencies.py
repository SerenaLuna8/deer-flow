from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI
from pydantic import Field
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.gateway import deps
from app.gateway.automation_schemas import AutomationRoute, StrictAutomationRequest
from app.gateway.deps import (
    get_current_user_from_request,
    private_work_context,
    project_session,
)
from app.gateway.private_work_schemas import (
    PrivateWorkRoute,
    StrictPrivateWorkRequest,
    strip_client_authority_fields,
)
from app.gateway.routers import project_automations
from app.private_work.context import PrivateWorkContext
from app.projects.errors import ProjectDatabaseUnavailable


class _Request(StrictPrivateWorkRequest):
    name: str
    config: dict[str, Any] = Field(default_factory=dict)


router = APIRouter(
    prefix="/test/private-work",
    route_class=PrivateWorkRoute,
)


@router.post("/{project_id}")
async def _probe(
    body: _Request,
    context: PrivateWorkContext = Depends(private_work_context),
):
    return {
        "project_id": str(context.project_id),
        "owner_user_id": str(context.user_id),
        "config": strip_client_authority_fields(body.config),
    }


@pytest_asyncio.fixture()
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


def _app(seed: M4ThreadSeed, identity: dict[str, uuid.UUID]) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def request_session():
        async with seed.factory() as session:
            yield session

    async def current_user():
        return SimpleNamespace(id=identity["user_id"])

    app.dependency_overrides[project_session] = request_session
    app.dependency_overrides[get_current_user_from_request] = current_user
    return app


@pytest.mark.postgres
@pytest.mark.anyio
async def test_private_work_context_resolves_authenticated_project_member(
    seed: M4ThreadSeed,
) -> None:
    identity = {"user_id": seed.owner_a.user_id}
    app = _app(seed, identity)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/test/private-work/{seed.owner_a.project_id}",
            json={"name": "ok"},
        )

    assert response.status_code == 200
    assert response.json()["project_id"] == str(seed.owner_a.project_id)
    assert response.json()["owner_user_id"] == str(seed.owner_a.user_id)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_private_work_route_maps_invalid_uuid_and_body_to_stable_422(
    seed: M4ThreadSeed,
) -> None:
    app = _app(seed, {"user_id": seed.owner_a.user_id})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        invalid_uuid = await client.post(
            "/test/private-work/not-a-uuid",
            json={"name": "ok"},
        )
        invalid_body = await client.post(
            f"/test/private-work/{seed.owner_a.project_id}",
            json={"name": "ok", "owner_user_id": str(seed.owner_b.user_id)},
        )

    for response in (invalid_uuid, invalid_body):
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"


@pytest.mark.postgres
@pytest.mark.anyio
async def test_private_work_context_hides_outsider_and_maps_database_unavailable(
    seed: M4ThreadSeed,
    monkeypatch,
) -> None:
    identity = {"user_id": uuid.uuid4()}
    app = _app(seed, identity)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        outsider = await client.post(
            f"/test/private-work/{seed.owner_a.project_id}",
            json={"name": "hidden"},
        )
        assert outsider.status_code == 404
        assert outsider.json()["detail"]["code"] == "PRIVATE_WORK_NOT_FOUND"

        monkeypatch.setattr(
            deps,
            "resolve_project_context",
            AsyncMock(side_effect=ProjectDatabaseUnavailable()),
        )
        unavailable = await client.post(
            f"/test/private-work/{seed.owner_a.project_id}",
            json={"name": "unavailable"},
        )

    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "PRIVATE_WORK_UNAVAILABLE"


def test_client_authority_fields_are_stripped_recursively() -> None:
    sanitized = strip_client_authority_fields(
        {
            "safe": "kept",
            "project_id": "forged-project",
            "nested": {"owner_user_id": "forged-owner", "value": 1},
            "items": [{"user_id": "forged-user", "value": 2}],
            "tuple_items": ({"membership_version": 999, "value": 3},),
            "__private": "drop",
        }
    )

    assert sanitized == {
        "safe": "kept",
        "nested": {"value": 1},
        "items": [{"value": 2}],
        "tuple_items": ({"value": 3},),
    }


def test_project_automation_uses_its_own_strict_route_and_split_readiness_router() -> None:
    assert project_automations.router.route_class is AutomationRoute
    assert project_automations.readiness_router.route_class is AutomationRoute
    assert issubclass(project_automations.AutomationCreateRequest, StrictAutomationRequest)

    readiness_paths = {route.path for route in project_automations.readiness_router.routes}
    data_paths = {route.path for route in project_automations.router.routes}
    assert readiness_paths == {"/api/projects/{project_id}/automations/readiness"}
    assert "/api/projects/{project_id}/automations/readiness" not in data_paths
