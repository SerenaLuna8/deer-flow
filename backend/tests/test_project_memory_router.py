from __future__ import annotations

import importlib
import uuid
from types import ModuleType, SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import update
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.gateway.deps import get_current_user_from_request, project_session
from app.private_work.memory_service import PrivateMemoryService
from app.projects.models import ProjectRole
from deerflow.agents.memory.storage import create_empty_memory
from deerflow.persistence.projects import ProjectMembershipRow


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

        created = await client.post(
            f"{base}/facts",
            json={
                "expected_version": initial_body["version"],
                "content": "Ship runnable versions first.",
                "category": "preference",
                "confidence": 0.9,
            },
        )
        assert created.status_code == 200
        created_body = created.json()
        created_fact = created_body["memory"]["facts"][0]
        assert created_fact["content"] == "Ship runnable versions first."
        assert created_fact["category"] == "preference"
        assert created_fact["confidence"] == 0.9
        assert created_fact["source"] == "manual"
        assert created_fact["id"]
        assert created_fact["createdAt"]
        fact_id = created_fact["id"]

        status = await client.get(f"{base}/status")
        assert status.status_code == 200
        assert status.json() == {
            "namespace": "default",
            "version": created_body["version"],
            "fact_count": 1,
            "last_updated": created_body["memory"]["lastUpdated"],
        }

        exported = await client.get(f"{base}/export")
        assert exported.status_code == 200
        assert exported.json()["facts"][0]["content"] == "Ship runnable versions first."

        reloaded = await client.post(f"{base}/reload")
        assert reloaded.status_code == 200
        assert reloaded.json() == created_body

        updated = await client.patch(
            f"{base}/facts/{fact_id}",
            json={
                "expected_version": created_body["version"],
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

        create_forbidden = await client.post(
            f"{base}/facts",
            json={
                "expected_version": listed.json()["version"],
                "content": "Viewer cannot create facts.",
                "category": "context",
                "confidence": 0.8,
            },
        )
        assert create_forbidden.status_code == 403
        assert create_forbidden.json()["detail"]["code"] == "PRIVATE_WORK_FORBIDDEN"

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

        create_invalid = await client.post(
            f"{base}/facts",
            json={
                "expected_version": listed.json()["version"],
                "content": "No client authority fields.",
                "category": "context",
                "confidence": 0.8,
                "source": "forged-thread",
            },
        )
        assert create_invalid.status_code == 422
        assert create_invalid.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"

    identity["user_id"] = uuid.uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        outsider = await client.get(base)
        assert outsider.status_code == 404
        assert outsider.json()["detail"]["code"] == "PRIVATE_WORK_NOT_FOUND"


@pytest.mark.postgres
@pytest.mark.anyio
async def test_viewer_can_delete_own_memory_fact_but_cannot_modify_memory(
    seed: M4ThreadSeed,
) -> None:
    identity = {"user_id": seed.owner_a.user_id}
    app = _app(seed, identity)
    base = f"/api/projects/{seed.owner_a.project_id}/memory"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        initial = await client.get(base)
        imported = await client.post(
            f"{base}/import",
            json={
                "expected_version": initial.json()["version"],
                "memory": _memory("Viewer-owned memory", fact="Delete this fact."),
            },
        )
        fact_id = imported.json()["memory"]["facts"][0]["id"]
        async with seed.factory() as session, session.begin():
            await session.execute(update(ProjectMembershipRow).where(ProjectMembershipRow.id == seed.owner_a.membership_id).values(role=ProjectRole.VIEWER.value))

        denied = await client.patch(
            f"{base}/facts/{fact_id}",
            json={
                "expected_version": imported.json()["version"],
                "content": "Viewer may not modify this fact.",
            },
        )
        deleted = await client.request(
            "DELETE",
            f"{base}/facts/{fact_id}",
            json={"expected_version": imported.json()["version"]},
        )

    assert denied.status_code == 403
    assert deleted.status_code == 200
    assert deleted.json()["memory"]["facts"] == []
