from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.gateway.deps import (
    get_current_user_from_request,
    private_work_context,
    project_session,
)
from app.gateway.routers import private_work
from app.private_work.cutover import PrivateWorkCutoverGuard
from app.private_work.errors import PrivateWorkCutover


@pytest_asyncio.fixture()
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


def _project_app(seed: M4ThreadSeed) -> FastAPI:
    app = FastAPI()
    app.state.private_work_cutover_guard = PrivateWorkCutoverGuard(seed.factory)
    app.include_router(private_work.router)

    async def request_session():
        async with seed.factory() as session:
            yield session

    async def current_user():
        return SimpleNamespace(id=seed.owner_a.user_id, system_role="user")

    app.dependency_overrides[project_session] = request_session
    app.dependency_overrides[get_current_user_from_request] = current_user
    return app


def _assert_cutover(response: httpx.Response) -> None:
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_CUTOVER"


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_private_http_stays_closed_until_cutover_marker_is_complete(
    seed: M4ThreadSeed,
) -> None:
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE private_work_cutover_state
                SET stage='migration_ready', cutover_at=NULL
                WHERE id=1"""
            )
        )

    app = _project_app(seed)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{seed.owner_a.project_id}/private-work/threads",
            json={
                "thread_id": str(uuid.uuid4()),
                "agent_asset_id": str(seed.project_agent_id),
            },
        )

    _assert_cutover(response)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_pre_expand_schema_keeps_project_private_work_closed(
    seed: M4ThreadSeed,
) -> None:
    async with seed.engine.begin() as connection:
        await connection.execute(text("DROP TABLE private_work_cutover_state"))

    guard = PrivateWorkCutoverGuard(seed.factory, request_id="pre-expand")
    with pytest.raises(PrivateWorkCutover):
        await guard.require_project_open()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_private_guard_accepts_descendant_revision(
    seed: M4ThreadSeed,
) -> None:
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE alembic_version
                SET version_num='0013_project_automation_finalize'"""
            )
        )

    guard = PrivateWorkCutoverGuard(seed.factory, request_id="m5-descendant")
    await guard.require_project_open()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_private_guard_fails_closed_for_unknown_revision(
    seed: M4ThreadSeed,
) -> None:
    async with seed.engine.begin() as connection:
        await connection.execute(text("UPDATE alembic_version SET version_num='unknown_branch'"))

    guard = PrivateWorkCutoverGuard(seed.factory, request_id="unknown-branch")
    with pytest.raises(PrivateWorkCutover):
        await guard.require_project_open()


@pytest.mark.anyio
async def test_project_route_without_lifespan_guard_returns_stable_503() -> None:
    app = FastAPI()
    app.include_router(private_work.router)
    app.dependency_overrides[private_work_context] = lambda: SimpleNamespace(request_id="missing-guard")
    project_id = uuid.uuid4()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{project_id}/private-work/threads/search",
            json={},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Private work cutover guard not available"}
