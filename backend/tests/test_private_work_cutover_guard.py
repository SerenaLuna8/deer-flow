from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from starlette.requests import Request
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.gateway.app import _ensure_admin_user
from app.gateway.deps import (
    get_current_user_from_request,
    private_work_context,
    project_session,
)
from app.gateway.routers import (
    artifacts,
    channel_connections,
    memory,
    private_work,
    runs,
    thread_runs,
    threads,
    uploads,
)
from app.gateway.routers.thread_runs import RunCreateRequest
from app.gateway.services import start_run
from app.private_work.cutover import PrivateWorkCutoverGuard
from app.private_work.errors import PrivateWorkCutover


@pytest_asyncio.fixture()
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


def _legacy_app(seed: M4ThreadSeed) -> FastAPI:
    app = FastAPI()
    app.state.private_work_cutover_guard = PrivateWorkCutoverGuard(seed.factory)
    for legacy_router in (
        threads.router,
        thread_runs.router,
        runs.router,
        memory.router,
        channel_connections.router,
        uploads.router,
        artifacts.router,
    ):
        app.include_router(legacy_router)

    async def request_session():
        async with seed.factory() as session:
            yield session

    async def current_user():
        return SimpleNamespace(id=seed.owner_a.user_id, system_role="user")

    app.dependency_overrides[project_session] = request_session
    app.dependency_overrides[get_current_user_from_request] = current_user
    return app


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
async def test_cutover_marker_closes_legacy_private_http_entrypoints_before_handlers(
    seed: M4ThreadSeed,
) -> None:
    app = _legacy_app(seed)
    requests = (
        ("POST", "/api/threads/search", {"json": {}}),
        ("GET", "/api/threads/legacy-thread/runs", {}),
        ("GET", "/api/runs/legacy-run/messages", {}),
        ("GET", "/api/memory", {}),
        ("GET", "/api/channels/connections", {}),
        ("GET", "/api/threads/legacy-thread/uploads/list", {}),
        (
            "GET",
            "/api/threads/legacy-thread/artifacts/mnt/user-data/outputs/result.txt",
            {},
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        for method, path, kwargs in requests:
            _assert_cutover(await client.request(method, path, **kwargs))


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
async def test_pre_expand_schema_keeps_legacy_open_and_project_closed(
    seed: M4ThreadSeed,
) -> None:
    async with seed.engine.begin() as connection:
        await connection.execute(text("DROP TABLE private_work_cutover_state"))

    guard = PrivateWorkCutoverGuard(seed.factory, request_id="pre-expand")
    await guard.require_legacy_open()
    with pytest.raises(PrivateWorkCutover):
        await guard.require_project_open()


class _RejectingLegacyGuard:
    async def require_legacy_open(self) -> None:
        raise PrivateWorkCutover("runtime-cutover")


def _request(app: FastAPI) -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/runs/wait",
            "raw_path": b"/api/runs/wait",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "app": app,
        }
    )
    request.state.user = SimpleNamespace(id="legacy-owner", system_role="user")
    return request


@pytest.mark.anyio
async def test_shared_start_run_stops_at_runtime_cutover_guard_before_singletons() -> None:
    app = FastAPI()
    app.state.private_work_cutover_guard = _RejectingLegacyGuard()

    with pytest.raises(HTTPException) as captured:
        await start_run(
            RunCreateRequest(input={"messages": [{"role": "user", "content": "hello"}]}),
            "legacy-thread",
            _request(app),
        )

    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "PRIVATE_WORK_CUTOVER"


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


@pytest.mark.anyio
async def test_gateway_skips_orphan_store_migration_after_cutover() -> None:
    provider = AsyncMock()
    provider.count_admin_users = AsyncMock(return_value=1)
    admin = SimpleNamespace(id=uuid.uuid4())
    result = MagicMock()
    result.scalar_one_or_none.return_value = admin
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session_context = AsyncMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=session_context)
    app = SimpleNamespace(
        state=SimpleNamespace(
            store=MagicMock(),
            private_work_cutover_guard=_RejectingLegacyGuard(),
        )
    )

    with (
        patch("app.gateway.deps.get_local_provider", return_value=provider),
        patch(
            "deerflow.persistence.engine.get_session_factory",
            return_value=session_factory,
        ),
        patch(
            "app.gateway.app._migrate_orphaned_threads",
            new=AsyncMock(),
        ) as migrate,
    ):
        await _ensure_admin_user(app)

    migrate.assert_not_awaited()
