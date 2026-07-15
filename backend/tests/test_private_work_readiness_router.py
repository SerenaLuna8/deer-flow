from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.gateway.deps import (
    get_current_user_from_request,
    private_work_context,
    project_session,
)
from app.gateway.routers.private_work import router


def test_readiness_route_is_registered() -> None:
    routes = {(route.path, method) for route in router.routes for method in getattr(route, "methods", set())}

    assert (
        "/api/projects/{project_id}/private-work/readiness",
        "GET",
    ) in routes


@pytest_asyncio.fixture()
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


def _app(seed: M4ThreadSeed) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def request_session():
        async with seed.factory() as session:
            yield session

    async def current_user():
        return SimpleNamespace(id=seed.owner_a.user_id)

    app.dependency_overrides[project_session] = request_session
    app.dependency_overrides[get_current_user_from_request] = current_user
    return app


async def _get(app: FastAPI, seed: M4ThreadSeed) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.get(f"/api/projects/{seed.owner_a.project_id}/private-work/readiness")


def _assert_public_response(
    response: httpx.Response,
    *,
    status: str,
    code: str,
) -> None:
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"status", "code", "request_id"}
    assert payload["status"] == status
    assert payload["code"] == code
    assert isinstance(payload["request_id"], str)
    assert payload["request_id"]


@pytest.mark.postgres
@pytest.mark.anyio
async def test_readiness_reports_ready_incomplete_and_missing_marker(
    seed: M4ThreadSeed,
) -> None:
    app = _app(seed)

    _assert_public_response(
        await _get(app, seed),
        status="ready",
        code="PRIVATE_WORK_READY",
    )

    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE private_work_cutover_state
                SET stage='migration_ready', cutover_at=NULL
                WHERE id=1"""
            )
        )
    _assert_public_response(
        await _get(app, seed),
        status="migration_required",
        code="PRIVATE_WORK_CUTOVER",
    )

    async with seed.engine.begin() as connection:
        await connection.execute(text("DELETE FROM private_work_cutover_state WHERE id=1"))
    _assert_public_response(
        await _get(app, seed),
        status="migration_required",
        code="PRIVATE_WORK_CUTOVER",
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_readiness_requires_final_schema_even_with_complete_marker(
    seed: M4ThreadSeed,
) -> None:
    app = _app(seed)
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE alembic_version
                SET version_num='0010_private_file_source'"""
            )
        )

    _assert_public_response(
        await _get(app, seed),
        status="migration_required",
        code="PRIVATE_WORK_CUTOVER",
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_readiness_reports_database_unavailable_without_leaking_scope(
    seed: M4ThreadSeed,
) -> None:
    app = _app(seed)

    class UnavailableSession:
        async def scalar(self, *_args, **_kwargs):
            raise SQLAlchemyError("database unavailable")

    async def trusted_context():
        return seed.owner_a

    async def unavailable_session():
        yield UnavailableSession()

    app.dependency_overrides[private_work_context] = trusted_context
    app.dependency_overrides[project_session] = unavailable_session

    _assert_public_response(
        await _get(app, seed),
        status="unavailable",
        code="PRIVATE_WORK_UNAVAILABLE",
    )
