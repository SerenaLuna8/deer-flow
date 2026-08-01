from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.gateway.deps import project_session
from app.gateway.routers import projects
from app.projects.capabilities import Capability, capabilities_for
from app.projects.errors import ProjectDatabaseUnavailable, ProjectQuotaStateConflict
from app.projects.models import ProjectQuotaSummary, ProjectRole, ProjectView, QuotaDimensionSummary
from deerflow.config.quota_config import QuotaConfig


class _NoopQuota:
    async def reserve_member(self, *_args, **_kwargs) -> None:
        return None


class _NoopQuotaService:
    config = QuotaConfig()

    async def current_config(self, _session):
        return self.config


def _quota_summary() -> ProjectQuotaSummary:
    return ProjectQuotaSummary(
        members=QuotaDimensionSummary(used=1, reserved=0, limit=20),
        storage_bytes=QuotaDimensionSummary(used=0, reserved=0, limit=5_368_709_120),
        concurrent_runs=QuotaDimensionSummary(used=0, reserved=0, limit=3),
        mcp_calls_daily=QuotaDimensionSummary(used=0, reserved=0, limit=10_000),
    )


def _client() -> TestClient:
    app = FastAPI()
    app.state.project_quota_enforcer = _NoopQuota()
    app.state.project_quota_service = _NoopQuotaService()
    app.state.operational_audit_sink = AsyncMock()
    app.include_router(projects.router)

    async def fake_session():
        yield None

    app.dependency_overrides[project_session] = fake_session
    app.dependency_overrides[projects.authenticated_project_identity] = lambda: (uuid.uuid4(), "trace-test")
    return TestClient(app)


def test_project_router_validation_uses_stable_code_for_path_body_and_query() -> None:
    client = _client()
    for response in (
        client.get("/api/projects/not-a-uuid"),
        client.post("/api/projects", json={"slug": "alpha", "display_name": "Alpha", "role": "admin"}),
        client.put("/api/projects/00000000-0000-0000-0000-000000000001/pin", json={"pinned": 1}),
        client.get("/api/projects?limit=not-a-number"),
        client.get("/api/projects?pinned=not-a-boolean"),
        client.get("/api/projects?limit=0"),
        client.post("/api/projects", json={"slug": "bad slug", "display_name": "Bad"}),
    ):
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "PROJECT_VALIDATION_FAILED"


def test_project_operation_ids_are_unique() -> None:
    schema = _client().app.openapi()
    operation_ids = [operation["operationId"] for path in schema["paths"].values() for operation in path.values()]
    assert len(operation_ids) == len(set(operation_ids))


def test_project_openapi_preserves_role_and_capability_enums() -> None:
    schemas = _client().app.openapi()["components"]["schemas"]
    response = schemas["ProjectResponse"]
    assert response["properties"]["role"]["$ref"].endswith("/ProjectRole")
    assert response["properties"]["capabilities"]["items"]["$ref"].endswith("/Capability")
    assert schemas["ProjectRole"]["enum"] == [role.value for role in ProjectRole]
    assert schemas["Capability"]["enum"] == [capability.value for capability in Capability]


def test_project_response_capabilities_follow_declaration_order_and_hide_private_fields() -> None:
    project_id = uuid.uuid4()
    response = projects._response(
        ProjectView(
            project_id,
            "alpha",
            "Alpha",
            "",
            "folder",
            ProjectRole.ADMIN,
            capabilities_for(ProjectRole.ADMIN),
            False,
            None,
            1,
            0,
            0,
            0,
            _quota_summary(),
            "active",
            False,
            1,
            "trace-1",
        )
    )
    assert response.request_id == "trace-1"
    assert response.capabilities == list(Capability)
    assert "created_by_user_id" not in response.model_dump()
    assert "members" not in response.model_dump()
    assert response.quota_summary.members.limit == 20


def test_project_router_requires_authentication() -> None:
    app = FastAPI()
    app.state.project_quota_enforcer = _NoopQuota()
    app.state.project_quota_service = _NoopQuotaService()
    app.state.operational_audit_sink = AsyncMock()
    app.include_router(projects.router)

    async def fake_session():
        yield None

    app.dependency_overrides[project_session] = fake_session
    response = TestClient(app).get("/api/projects")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "not_authenticated"


@pytest.mark.asyncio
async def test_project_session_maps_uninitialized_engine_to_stable_503(monkeypatch) -> None:
    from deerflow.persistence import engine as persistence_engine

    def unavailable_factory():
        raise RuntimeError("Persistence engine is not initialized")

    monkeypatch.setattr(persistence_engine, "get_session_factory", unavailable_factory)
    dependency = project_session()
    with pytest.raises(HTTPException) as exc_info:
        await anext(dependency)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "DATABASE_UNAVAILABLE"
    assert exc_info.value.detail["message"] == "Project storage unavailable"
    assert isinstance(exc_info.value.detail["request_id"], str)
    assert exc_info.value.detail["request_id"]


def test_project_database_failure_is_503_without_driver_details(monkeypatch) -> None:
    app = FastAPI()
    app.state.project_quota_service = _NoopQuotaService()
    app.include_router(projects.router)

    async def fake_session():
        yield None

    app.dependency_overrides[project_session] = fake_session
    app.dependency_overrides[projects.authenticated_project_identity] = lambda: (uuid.uuid4(), "trace-db")
    monkeypatch.setattr(projects.ProjectService, "list", AsyncMock(side_effect=ProjectDatabaseUnavailable()))

    response = TestClient(app).get("/api/projects")
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "DATABASE_UNAVAILABLE", "message": "Project storage unavailable"}}
    assert "postgresql" not in response.text


def test_project_create_quota_state_conflict_is_stable_409(monkeypatch) -> None:
    monkeypatch.setattr(
        projects.ProjectService,
        "create",
        AsyncMock(side_effect=ProjectQuotaStateConflict()),
    )

    response = _client().post(
        "/api/projects",
        json={"slug": "alpha", "display_name": "Alpha"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "PROJECT_QUOTA_STATE_CONFLICT",
        "message": "Project quota state conflict",
    }


@pytest.mark.asyncio
async def test_project_context_service_uses_current_quota_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = object()
    context = object()
    quota_service = AsyncMock()
    captured: dict[str, object] = {}

    class Repository:
        def __init__(self, passed_session, *, quota_policy, **_kwargs) -> None:
            captured["session"] = passed_session
            captured["quota_policy"] = quota_policy

    monkeypatch.setattr(
        projects,
        "resolve_project_context",
        AsyncMock(return_value=context),
    )
    monkeypatch.setattr(projects, "ProjectRepository", Repository)

    resolved, _service = await projects._context_service(
        uuid.uuid4(),
        (uuid.uuid4(), "request-id"),
        session,
        quota_service,
    )

    assert resolved is context
    assert captured == {
        "session": session,
        "quota_policy": quota_service,
    }


@pytest.mark.asyncio
async def test_project_api_postgres_full_authorization_and_personal_state(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    users = {name: uuid.uuid4() for name in ("admin", "editor", "runner", "viewer", "outsider", "system_admin")}
    identity = {"user_id": users["admin"]}
    app = FastAPI()
    app.state.project_quota_enforcer = _NoopQuota()
    app.state.project_quota_service = _NoopQuotaService()
    app.state.operational_audit_sink = AsyncMock()
    app.include_router(projects.router)

    async def request_session():
        async with factory() as session:
            yield session

    async def request_identity():
        user_id = identity["user_id"]
        return user_id, f"trace-{user_id}"

    app.dependency_overrides[project_session] = request_session
    app.dependency_overrides[projects.authenticated_project_identity] = request_identity

    try:
        async with engine.begin() as connection:
            for name, user_id in users.items():
                await connection.execute(
                    text(
                        """INSERT INTO users
                        (id,email,system_role,created_at,needs_setup,token_version)
                        VALUES (:id,:email,:role,:now,false,0)"""
                    ),
                    {
                        "id": str(user_id),
                        "email": f"{name}@example.com",
                        "role": "system_admin" if name == "system_admin" else "user",
                        "now": datetime.now(UTC),
                    },
                )

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/api/projects",
                json={
                    "slug": "alpha-project",
                    "display_name": "Alpha",
                    "description": "Shared",
                    "icon": "folder",
                },
            )
            assert created.status_code == 201
            project = created.json()
            project_id = project["id"]
            assert project["role"] == "admin"
            assert project["capabilities"] == [capability.value for capability in Capability]
            assert project["request_id"] == f"trace-{users['admin']}"
            assert set(project) == set(projects.ProjectResponse.model_fields)

            async with engine.begin() as connection:
                for role in ("editor", "runner", "viewer"):
                    await connection.execute(
                        text(
                            """INSERT INTO project_memberships
                            (id,project_id,user_id,role) VALUES (:id,:project_id,:user_id,:role)"""
                        ),
                        {
                            "id": uuid.uuid4(),
                            "project_id": uuid.UUID(project_id),
                            "user_id": str(users[role]),
                            "role": role,
                        },
                    )

            listed = await client.get("/api/projects?query=Alpha&limit=1")
            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()["items"]] == [project_id]

            patched = await client.patch(f"/api/projects/{project_id}", json={"display_name": "Alpha changed"})
            assert patched.status_code == 200
            assert patched.json()["display_name"] == "Alpha changed"

            for role in ("editor", "runner", "viewer"):
                identity["user_id"] = users[role]
                forbidden = await client.patch(f"/api/projects/{project_id}", json={"display_name": "Denied"})
                assert forbidden.status_code == 403
                assert forbidden.json()["detail"]["code"] == "PROJECT_FORBIDDEN"

            for name in ("outsider", "system_admin"):
                identity["user_id"] = users[name]
                hidden = await client.get(f"/api/projects/{project_id}")
                assert hidden.status_code == 404
                assert hidden.json()["detail"] == {
                    "code": "PROJECT_NOT_FOUND",
                    "message": "Project not found",
                }

            identity["user_id"] = users["viewer"]
            pinned = await client.put(f"/api/projects/{project_id}/pin", json={"pinned": True})
            entered = await client.post(f"/api/projects/{project_id}/enter")
            assert pinned.status_code == entered.status_code == 200
            assert entered.json()["is_pinned"] is True
            assert entered.json()["last_entered_at"] is not None

            identity["user_id"] = users["admin"]
            admin_view = await client.get(f"/api/projects/{project_id}")
            assert admin_view.json()["is_pinned"] is False
            assert admin_view.json()["last_entered_at"] is None

            first, second = await asyncio.gather(
                client.post("/api/projects", json={"slug": "same-slug", "display_name": "First"}),
                client.post("/api/projects", json={"slug": "same-slug", "display_name": "Second"}),
            )
            assert sorted((first.status_code, second.status_code)) == [201, 409]
            conflict = first if first.status_code == 409 else second
            assert conflict.json()["detail"]["code"] == "PROJECT_SLUG_CONFLICT"

            invalid_cursor = await client.get("/api/projects?cursor=not-a-cursor")
            assert invalid_cursor.status_code == 422
            assert invalid_cursor.json()["detail"]["code"] == "PROJECT_VALIDATION_FAILED"

            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE projects SET is_suspended=true WHERE id=:id"),
                    {"id": uuid.UUID(project_id)},
                )
            suspended = await client.get(f"/api/projects/{project_id}")
            assert suspended.status_code == 404
            assert suspended.json()["detail"]["code"] == "PROJECT_NOT_FOUND"
    finally:
        await engine.dispose()
