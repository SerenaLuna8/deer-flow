from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import text
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.gateway.deps import private_work_context, require_project_private_open
from app.gateway.routers import private_work as private_work_router
from app.private_work.context import PrivateWorkContext
from app.private_work.file_service import PrivateFileService
from app.private_work.file_streaming import PrivateFileStreamer
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.persistence.private_work.model import PrivateFileRow


def test_private_work_router_exposes_project_file_route_matrix() -> None:
    routes = {(route.path, method) for route in private_work_router.router.routes for method in route.methods or ()}

    prefix = "/api/projects/{project_id}/private-work"
    thread_prefix = f"{prefix}/threads/{{thread_id}}"
    assert (f"{thread_prefix}/uploads", "POST") in routes
    assert (f"{thread_prefix}/uploads/limits", "GET") in routes
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
    identities: dict[str, PrivateWorkContext]

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
    app.include_router(private_work_router.router)
    app.state.private_file_service = PrivateFileService(seed.factory)
    app.state.private_file_streamer = PrivateFileStreamer(seed.factory)
    app.dependency_overrides[require_project_private_open] = lambda: None

    identities = {
        "owner-a": seed.owner_a,
        "owner-b": seed.owner_b,
        "viewer": seed.viewer,
        "project-b-owner-a": seed.project_b_owner_a,
    }

    async def context_override(project_id: uuid.UUID, request: Request):
        identity = request.headers.get("x-test-private-identity", "owner-a")
        # The same account owns private scopes in both seeded projects. Mirror
        # production context resolution instead of rejecting that valid
        # account/project pair in the test dependency itself.
        if identity == "owner-a" and project_id == seed.project_b_owner_a.project_id:
            return seed.project_b_owner_a
        selected = identities.get(identity)
        if selected is not None and project_id == selected.project_id:
            return selected
        raise HTTPException(status_code=404)

    app.dependency_overrides[private_work_context] = context_override
    return _Harness(seed=seed, app=app, identities=identities)


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

    limits = await harness.request(
        "GET",
        f"/threads/{thread_id}/uploads/limits",
    )
    assert limits.status_code == 200
    limit_payload = limits.json()
    assert set(limit_payload) == {
        "max_files",
        "max_file_size",
        "max_total_size",
        "project_storage",
        "request_id",
    }
    assert limit_payload["max_files"] == 10
    assert limit_payload["max_file_size"] == 100 * 1024 * 1024
    assert limit_payload["max_total_size"] == 100 * 1024 * 1024
    storage = limit_payload["project_storage"]
    assert set(storage) == {
        "policy",
        "remaining_bytes",
    }
    assert storage["policy"] == "project_quota"
    assert storage["remaining_bytes"] >= 0

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
    listed_metadata = listed.json()[0]
    assert {key: value for key, value in listed_metadata.items() if key != "updated_at"} == {key: value for key, value in metadata.items() if key != "updated_at"}
    assert listed_metadata["updated_at"] >= metadata["updated_at"]

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
        ("GET", f"/threads/{thread_id}/uploads/limits", "owner-b", None),
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
        (
            "DELETE",
            f"/threads/{thread_id}/uploads?file_id={file_id}",
            "project-b-owner-a",
            harness.seed.project_b_owner_a.project_id,
        ),
        ("GET", f"/threads/{uuid.uuid4()}/uploads", "owner-a", None),
        (
            "GET",
            f"/threads/{uuid.uuid4()}/uploads/limits",
            "owner-a",
            None,
        ),
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


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_ready_file_list_pages_past_one_hundred_without_omission(
    harness: _Harness,
) -> None:
    thread_id = uuid.uuid4()
    async with harness.seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=harness.seed.owner_a.resource_scope,
            thread_id=str(thread_id),
            agent=ThreadAgentRef(harness.seed.project_agent_id, "project"),
        )
        session.add_all(
            [
                PrivateFileRow(
                    id=uuid.uuid5(uuid.NAMESPACE_URL, f"m09-upload-page-{index}"),
                    project_id=harness.seed.owner_a.project_id,
                    owner_user_id=str(harness.seed.owner_a.user_id),
                    thread_id=str(thread_id),
                    kind="upload",
                    logical_path=f"uploads/page-{index:03d}.txt",
                    media_type="text/plain",
                    size=0,
                    sha256=hashlib.sha256(b"").hexdigest(),
                    status="ready",
                    version=1,
                )
                for index in range(101)
            ]
        )

    first = await harness.request(
        "GET",
        f"/threads/{thread_id}/uploads?limit=40&offset=0",
    )
    second = await harness.request(
        "GET",
        f"/threads/{thread_id}/uploads?limit=40&offset=40",
    )
    third = await harness.request(
        "GET",
        f"/threads/{thread_id}/uploads?limit=40&offset=80",
    )

    assert first.status_code == second.status_code == third.status_code == 200
    assert first.headers["x-next-offset"] == "40"
    assert second.headers["x-next-offset"] == "80"
    assert "x-next-offset" not in third.headers
    combined = [*first.json(), *second.json(), *third.json()]
    assert len(combined) == 101
    assert len({item["id"] for item in combined}) == 101
    assert [item["logical_path"] for item in combined] == [f"uploads/page-{index:03d}.txt" for index in range(101)]

    default_page = await harness.request(
        "GET",
        f"/threads/{thread_id}/uploads",
    )
    assert default_page.status_code == 200
    assert len(default_page.json()) == 100
    assert default_page.headers["x-next-offset"] == "100"

    for query in (
        "limit=0",
        "limit=101",
        "offset=-1",
        "offset=9223372036854775808",
    ):
        invalid = await harness.request(
            "GET",
            f"/threads/{thread_id}/uploads?{query}",
        )
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_downgraded_viewer_can_delete_existing_own_file(
    harness: _Harness,
) -> None:
    thread_id = uuid.uuid4()
    async with harness.seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=harness.seed.owner_a.resource_scope,
            thread_id=str(thread_id),
            agent=ThreadAgentRef(harness.seed.project_agent_id, "project"),
        )

    uploaded = await harness.request(
        "POST",
        f"/threads/{thread_id}/uploads",
        files={"file": ("before-downgrade.txt", b"retained", "text/plain")},
    )
    assert uploaded.status_code == 201
    file_id = uploaded.json()["id"]

    async with harness.seed.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE project_memberships
                   SET role='viewer', version=version+1
                   WHERE id=:membership_id"""
            ),
            {"membership_id": harness.seed.owner_a.membership_id},
        )
    harness.identities["owner-a"] = PrivateWorkContext.from_project(
        ProjectContext(
            user_id=harness.seed.owner_a.user_id,
            project_id=harness.seed.owner_a.project_id,
            membership_id=harness.seed.owner_a.membership_id,
            role=ProjectRole.VIEWER,
            capabilities=capabilities_for(ProjectRole.VIEWER),
            membership_version=harness.seed.owner_a.membership_version + 1,
            request_id="req-owner-a-downgraded",
        )
    )

    deleted = await harness.request(
        "DELETE",
        f"/threads/{thread_id}/uploads?file_id={file_id}",
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"success": True}

    limits_forbidden = await harness.request(
        "GET",
        f"/threads/{thread_id}/uploads/limits",
    )
    assert limits_forbidden.status_code == 403
    assert limits_forbidden.json()["detail"]["code"] == "PRIVATE_WORK_FORBIDDEN"

    listed = await harness.request("GET", f"/threads/{thread_id}/uploads")
    assert listed.status_code == 200
    assert listed.json() == []
