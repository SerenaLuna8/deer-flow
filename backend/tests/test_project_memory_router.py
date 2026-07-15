from __future__ import annotations

import importlib
import uuid
from types import ModuleType, SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from support.m4_private_threads import (
    M4ThreadSeed,
    install_open_project_cutover_guard,
    seed_m4_thread_database,
)

from app.gateway.deps import get_current_user_from_request, project_session
from app.private_work.memory_service import PrivateMemoryService
from deerflow.agents.memory.storage import create_empty_memory


@pytest_asyncio.fixture()
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


def _project_memory_module() -> ModuleType | None:
    try:
        return importlib.import_module("app.gateway.routers.project_memory")
    except ModuleNotFoundError:
        return None


def _app(seed: M4ThreadSeed, identity: dict[str, uuid.UUID]) -> FastAPI:
    app = FastAPI()
    install_open_project_cutover_guard(app)
    module = _project_memory_module()
    if module is not None:
        app.include_router(module.router)
    app.state.project_memory_service = PrivateMemoryService(seed.factory)

    async def request_session():
        async with seed.factory() as session:
            yield session

    async def current_user():
        return SimpleNamespace(id=identity["user_id"])

    app.dependency_overrides[project_session] = request_session
    app.dependency_overrides[get_current_user_from_request] = current_user
    return app


def _memory(summary: str, *, fact: str | None = None) -> dict:
    memory = create_empty_memory()
    memory["user"]["workContext"] = {
        "summary": summary,
        "updatedAt": "2026-07-15T09:00:00Z",
    }
    if fact is not None:
        memory["facts"] = [
            {
                "content": fact,
                "category": "preference",
                "confidence": 0.9,
                "source": "manual",
            }
        ]
    return memory


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_memory_routes_support_owner_crud(seed: M4ThreadSeed) -> None:
    identity = {"user_id": seed.owner_a.user_id}
    app = _app(seed, identity)
    base = f"/api/projects/{seed.owner_a.project_id}/memory"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        initial = await client.get(base)
        assert initial.status_code == 200
        initial_body = initial.json()
        assert initial_body["namespace"] == "default"
        assert initial_body["memory"]["facts"] == []

        imported = await client.post(
            f"{base}/import",
            json={
                "expected_version": initial_body["version"],
                "memory": _memory("Owner project memory", fact="Ship runnable versions first."),
            },
        )
        assert imported.status_code == 200
        imported_body = imported.json()
        fact_id = imported_body["memory"]["facts"][0]["id"]

        status = await client.get(f"{base}/status")
        assert status.status_code == 200
        assert status.json() == {
            "namespace": "default",
            "version": imported_body["version"],
            "fact_count": 1,
            "last_updated": imported_body["memory"]["lastUpdated"],
        }

        exported = await client.get(f"{base}/export")
        assert exported.status_code == 200
        assert exported.json()["facts"][0]["content"] == "Ship runnable versions first."

        reloaded = await client.post(f"{base}/reload")
        assert reloaded.status_code == 200
        assert reloaded.json() == imported_body

        updated = await client.patch(
            f"{base}/facts/{fact_id}",
            json={
                "expected_version": imported_body["version"],
                "content": "Keep the main flow runnable.",
                "confidence": 0.95,
            },
        )
        assert updated.status_code == 200
        updated_body = updated.json()
        assert updated_body["memory"]["facts"][0]["content"] == "Keep the main flow runnable."

        deleted = await client.request(
            "DELETE",
            f"{base}/facts/{fact_id}",
            json={"expected_version": updated_body["version"]},
        )
        assert deleted.status_code == 200
        assert deleted.json()["memory"]["facts"] == []


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_memory_routes_enforce_context_capabilities_and_strict_input(
    seed: M4ThreadSeed,
) -> None:
    identity = {"user_id": seed.viewer.user_id}
    app = _app(seed, identity)
    base = f"/api/projects/{seed.viewer.project_id}/memory"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        listed = await client.get(base)
        status = await client.get(f"{base}/status")
        exported = await client.get(f"{base}/export")
        reloaded = await client.post(f"{base}/reload")
        assert [response.status_code for response in (listed, status, exported, reloaded)] == [200, 200, 200, 200]

        forbidden = await client.post(
            f"{base}/import",
            json={
                "expected_version": listed.json()["version"],
                "memory": _memory("Viewer cannot write"),
            },
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"]["code"] == "PRIVATE_WORK_FORBIDDEN"

        invalid = await client.post(
            f"{base}/import",
            json={
                "expected_version": listed.json()["version"],
                "memory": _memory("Invalid extra authority"),
                "owner_user_id": str(seed.owner_a.user_id),
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"

    identity["user_id"] = uuid.uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        outsider = await client.get(base)
        assert outsider.status_code == 404
        assert outsider.json()["detail"]["code"] == "PRIVATE_WORK_NOT_FOUND"
