from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import text
from support.m4_private_threads import (
    M4ThreadSeed,
    install_open_project_cutover_guard,
    seed_m4_thread_database,
)

from app.gateway.deps import private_work_context
from app.gateway.routers import private_work as private_work_router
from app.private_work.file_service import PrivateFileService
from app.private_work.file_streaming import PrivateFileStreamer
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef


def test_private_work_router_exposes_project_file_route_matrix() -> None:
    routes = {(route.path, method) for route in private_work_router.router.routes for method in route.methods or ()}

    prefix = "/api/projects/{project_id}/private-work"
    thread_prefix = f"{prefix}/threads/{{thread_id}}"
    assert (f"{thread_prefix}/uploads", "POST") in routes
    assert (f"{thread_prefix}/uploads", "GET") in routes
    assert (f"{thread_prefix}/uploads", "DELETE") in routes
    assert (f"{thread_prefix}/files/{{file_id}}", "GET") in routes
    assert (f"{prefix}/artifacts/{{artifact_id}}", "GET") in routes


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

    async def request(
        self,
        method: str,
        suffix: str,
        *,
        identity: str = "owner-a",
        project_id: uuid.UUID | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        selected_project = project_id or self.seed.owner_a.project_id
        path = f"/api/projects/{selected_project}/private-work{suffix}"
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
    install_open_project_cutover_guard(app)
    app.include_router(private_work_router.router)
    app.state.private_file_service = PrivateFileService(seed.factory)
    app.state.private_file_streamer = PrivateFileStreamer(seed.factory)

    async def context_override(project_id: uuid.UUID, request: Request):
        identity = request.headers.get("x-test-private-identity", "owner-a")
        if identity == "owner-a":
            if project_id == seed.owner_a.project_id:
                return seed.owner_a
            if project_id == seed.project_b_owner_a.project_id:
                return seed.project_b_owner_a
        if identity == "owner-b" and project_id == seed.owner_b.project_id:
            return seed.owner_b
        raise HTTPException(status_code=404)

    app.dependency_overrides[private_work_context] = context_override
    return _Harness(seed=seed, app=app)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_files_happy_path_scope_and_stable_errors(
    harness: _Harness,
) -> None:
    thread_id = uuid.uuid4()
    run_id = uuid.uuid4()
    payload = b"private project upload"
    async with harness.seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=harness.seed.owner_a.resource_scope,
            thread_id=str(thread_id),
            agent=ThreadAgentRef(harness.seed.project_agent_id, "project"),
        )
        await PrivateRunRepository(session).create(
            scope=harness.seed.owner_a.resource_scope,
            thread_id=str(thread_id),
            request=PrivateRunCreate(run_id=str(run_id), status="success"),
        )

    uploaded = await harness.request(
        "POST",
        f"/threads/{thread_id}/uploads",
        files={"file": ("notes.txt", payload, "text/plain")},
    )
    assert uploaded.status_code == 201
    metadata = uploaded.json()
    assert set(metadata) == {
        "id",
        "logical_path",
        "display_name",
        "kind",
        "media_type",
        "size",
        "sha256",
        "status",
        "created_at",
        "updated_at",
    }
    assert metadata["logical_path"] == "uploads/notes.txt"
    assert metadata["display_name"] == "notes.txt"
    assert metadata["kind"] == "upload"
    assert metadata["media_type"] == "text/plain"
    assert metadata["size"] == len(payload)
    assert metadata["sha256"] == hashlib.sha256(payload).hexdigest()
    assert metadata["status"] == "ready"
    file_id = uuid.UUID(metadata["id"])

    listed = await harness.request("GET", f"/threads/{thread_id}/uploads")
    assert listed.status_code == 200
    assert listed.json() == [metadata]

    downloaded = await harness.request(
        "GET",
        f"/threads/{thread_id}/files/{file_id}",
    )
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    assert downloaded.headers["content-type"].startswith("text/plain")
    assert "notes.txt" in downloaded.headers["content-disposition"]
    assert downloaded.headers["x-content-type-options"] == "nosniff"

    artifact_id = uuid.uuid4()
    async with harness.seed.engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO artifacts
                (id,project_id,owner_user_id,thread_id,run_id,file_id,
                 display_name,media_type,artifact_metadata)
                VALUES (:id,:project_id,:owner_user_id,:thread_id,:run_id,
                        :file_id,'download.txt','text/plain','{}'::jsonb)"""
            ),
            {
                "id": artifact_id,
                "project_id": harness.seed.owner_a.project_id,
                "owner_user_id": str(harness.seed.owner_a.user_id),
                "thread_id": str(thread_id),
                "run_id": str(run_id),
                "file_id": file_id,
            },
        )

    artifact = await harness.request(
        "GET",
        f"/artifacts/{artifact_id}?thread_id={thread_id}",
    )
    assert artifact.status_code == 200
    assert artifact.content == payload
    assert "download.txt" in artifact.headers["content-disposition"]

    hidden_requests = (
        ("GET", f"/threads/{thread_id}/uploads", "owner-b", None),
        ("GET", f"/threads/{thread_id}/files/{file_id}", "owner-b", None),
        ("DELETE", f"/threads/{thread_id}/uploads?file_id={file_id}", "owner-b", None),
        ("GET", f"/artifacts/{artifact_id}?thread_id={thread_id}", "owner-b", None),
        (
            "GET",
            f"/threads/{thread_id}/files/{file_id}",
            "owner-a",
            harness.seed.project_b_owner_a.project_id,
        ),
        (
            "GET",
            f"/artifacts/{artifact_id}?thread_id={thread_id}",
            "owner-a",
            harness.seed.project_b_owner_a.project_id,
        ),
        ("GET", f"/threads/{uuid.uuid4()}/uploads", "owner-a", None),
        ("GET", f"/threads/{thread_id}/files/{uuid.uuid4()}", "owner-a", None),
        ("GET", f"/artifacts/{uuid.uuid4()}?thread_id={thread_id}", "owner-a", None),
    )
    for method, suffix, identity, project_id in hidden_requests:
        hidden = await harness.request(
            method,
            suffix,
            identity=identity,
            project_id=project_id,
        )
        assert hidden.status_code == 404
        assert hidden.json()["detail"]["code"] == "PRIVATE_WORK_NOT_FOUND"

    invalid_requests = (
        ("POST", f"/threads/{thread_id}/uploads", {}),
        ("DELETE", f"/threads/{thread_id}/uploads?file_id=not-a-uuid", {}),
        ("GET", f"/threads/{thread_id}/files/not-a-uuid", {}),
        ("GET", f"/artifacts/not-a-uuid?thread_id={thread_id}", {}),
        ("GET", f"/artifacts/{artifact_id}?thread_id=not-a-uuid", {}),
    )
    for method, suffix, kwargs in invalid_requests:
        invalid = await harness.request(method, suffix, **kwargs)
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"

    deleted = await harness.request(
        "DELETE",
        f"/threads/{thread_id}/uploads?file_id={file_id}",
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"success": True}
    assert (await harness.request("GET", f"/threads/{thread_id}/uploads")).json() == []
    hidden_deleted = await harness.request(
        "GET",
        f"/threads/{thread_id}/files/{file_id}",
    )
    assert hidden_deleted.status_code == 404
    assert hidden_deleted.json()["detail"]["code"] == "PRIVATE_WORK_NOT_FOUND"
