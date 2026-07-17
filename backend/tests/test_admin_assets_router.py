from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.gateway.deps import get_current_user_from_request
from app.gateway.routers import admin_assets
from app.projects.errors import ProjectNotFound
from app.projects.repository import ProjectRepository
from app.shared_assets.agent_service import AgentAssetView, AgentService, CreateAgent
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.credential_service import CreateCredential, CredentialService, CredentialView
from app.shared_assets.keyring import CredentialKeyring
from app.shared_assets.mcp_service import CreateMcpServer, McpService
from app.shared_assets.models import AgentPayload, AssetScope

PROJECT_ID = uuid.uuid4()
ADMIN_ID = uuid.uuid4()
NOW = datetime.now(UTC)


class _User:
    def __init__(self, role: str) -> None:
        self.id = ADMIN_ID
        self.system_role = role


def _credential(scope: AssetScope) -> CredentialView:
    return CredentialView(
        id=uuid.uuid4(),
        scope=scope,
        project_id=PROJECT_ID if scope is AssetScope.PROJECT else None,
        name="github",
        display_name="GitHub",
        credential_type="oauth",
        status="active",
        current_version_id=uuid.uuid4(),
        version=1,
        created_by_user_id=str(ADMIN_ID),
        created_at=NOW,
        updated_at=NOW,
    )


def _client(role: str, *, credential_service=None) -> TestClient:
    app = FastAPI()
    app.include_router(admin_assets.admin_router)
    app.include_router(admin_assets.admin_project_router)
    app.dependency_overrides[get_current_user_from_request] = lambda: _User(role)
    if credential_service is not None:
        app.dependency_overrides[admin_assets.get_credential_service] = lambda: credential_service
    return TestClient(app)


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for nested in value.values() for key in _recursive_keys(nested)}
    if isinstance(value, list):
        return {key for nested in value for key in _recursive_keys(nested)}
    return set()


def test_non_system_admin_cannot_access_admin_assets() -> None:
    response = _client("user", credential_service=AsyncMock()).get("/api/admin/assets/credentials")
    assert response.status_code == 403


def test_credential_response_never_contains_envelope_fields() -> None:
    service = AsyncMock()
    service.list_visible.return_value = (_credential(AssetScope.SYSTEM),)

    response = _client("system_admin", credential_service=service).get("/api/admin/assets/credentials")

    assert response.status_code == 200
    body = response.json()
    assert not ({"plaintext", "ciphertext", "nonce", "key_id", "storage_locator", "secret_hash"} & _recursive_keys(body))
    assert set(body) == {"items", "request_id"}


def test_non_system_admin_cannot_access_credential_rotation_status() -> None:
    response = _client("user", credential_service=AsyncMock()).get("/api/admin/assets/credentials/rotation-status")

    assert response.status_code == 403


def test_credential_rotation_status_is_static_and_secret_safe() -> None:
    service = AsyncMock()
    service.rotation_status.return_value = SimpleNamespace(
        eligible_total=7,
        current=5,
        pending=2,
        status="pending",
    )

    response = _client("system_admin", credential_service=service).get("/api/admin/assets/credentials/rotation-status")

    assert response.status_code == 200
    assert response.json() == {
        "eligible_total": 7,
        "current": 5,
        "pending": 2,
        "status": "pending",
    }
    assert not ({"plaintext", "ciphertext", "nonce", "key_id", "storage_locator", "secret_hash"} & _recursive_keys(response.json()))
    service.rotation_status.assert_awaited_once()
    actor = service.rotation_status.await_args.args[0]
    assert actor.user_id == ADMIN_ID
    assert actor.project_id is None


def test_system_admin_override_uses_governance_context_without_membership() -> None:
    service = AsyncMock()
    service.list_visible.return_value = (_credential(AssetScope.PROJECT),)

    response = _client("system_admin", credential_service=service).get(f"/api/admin/projects/{PROJECT_ID}/assets/credentials")

    assert response.status_code == 200
    actor = service.list_visible.await_args.args[0]
    assert actor == SystemAssetGovernanceContext(
        user_id=ADMIN_ID,
        request_id=actor.request_id,
        project_id=PROJECT_ID,
    )
    assert not hasattr(actor, "membership_id")
    with pytest.raises(ProjectNotFound):
        ProjectRepository(None)._scope(actor)


def test_successful_platform_override_emits_governance_event() -> None:
    sink = Mock()
    sink.append_override = AsyncMock()
    service = AgentService(lambda: None, governance_sink=sink)
    service._execute = AsyncMock(
        return_value=AgentAssetView(
            id=uuid.uuid4(),
            scope=AssetScope.PROJECT,
            project_id=PROJECT_ID,
            slug="reviewer",
            display_name="Reviewer",
            status="active",
            current_published_version_id=None,
            version=1,
            created_by_user_id=str(ADMIN_ID),
            created_at=NOW,
            updated_at=NOW,
        )
    )

    response = _client("system_admin").app
    response.dependency_overrides[admin_assets.get_agent_service] = lambda: service
    result = TestClient(response).post(
        f"/api/admin/projects/{PROJECT_ID}/assets/agents",
        json={"slug": "reviewer", "display_name": "Reviewer"},
    )

    assert result.status_code == 201
    governance = service._execute.await_args.kwargs["governance"]
    session = object()
    asyncio.run(governance(session, service._execute.return_value))
    assert sink.append_override.await_args.args == (session,)
    event = sink.append_override.await_args.kwargs
    assert event["actor"] == ADMIN_ID
    assert event["project_id"] == PROJECT_ID
    assert event["asset_id"] == service._execute.return_value.id
    assert event["action"] == "agent.create"
    assert set(event) == {
        "actor",
        "project_id",
        "asset_id",
        "version_id",
        "action",
        "request_id",
        "asset_kind",
    }
    assert event["asset_kind"] == "agent"


@pytest.mark.asyncio
async def test_system_admin_without_membership_governs_project_agent_in_postgres(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    actor = SystemAssetGovernanceContext(
        user_id=ADMIN_ID,
        project_id=PROJECT_ID,
        request_id="req-admin-override-postgres",
    )
    service = AgentService(factory)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'system_admin',:now,false,0)"""
                ),
                {"id": str(ADMIN_ID), "email": "override@example.com", "now": NOW},
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                    (id,slug,display_name,description,icon,created_by_user_id)
                    VALUES (:id,'override-project','Override project','','folder',:user_id)"""
                ),
                {"id": PROJECT_ID, "user_id": str(ADMIN_ID)},
            )

        created = await service.create_asset(actor, CreateAgent("reviewer", "Reviewer"))
        version = await service.create_version(
            actor,
            created.id,
            AgentPayload("Review", "Be precise", "default", (), (), ()),
            expected_asset_version=created.version,
        )
        published = await service.publish(
            actor,
            created.id,
            version.id,
            expected_asset_version=created.version + 1,
        )
        listed = await service.list_visible(actor)

        assert published.workflow_status.value == "published"
        assert [item.id for item in listed] == [created.id]
        async with engine.connect() as connection:
            membership_count = await connection.scalar(
                text("SELECT count(*) FROM project_memberships WHERE project_id=:project_id"),
                {"project_id": PROJECT_ID},
            )
        assert membership_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_override_mcp_and_credential_get_fail_closed_when_project_is_unavailable(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    other_project_id = uuid.uuid4()
    mcp_service = McpService(factory)
    credential_service = CredentialService(
        factory,
        keyring=CredentialKeyring("router-test-key", {"router-test-key": b"r" * 32}),
    )
    actor = SystemAssetGovernanceContext(ADMIN_ID, "req-override-get", PROJECT_ID)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'system_admin',:now,false,0)"""
                ),
                {"id": str(ADMIN_ID), "email": "override-get@example.com", "now": NOW},
            )
            for project_id, slug in (
                (PROJECT_ID, "override-get"),
                (other_project_id, "override-get-other"),
            ):
                await connection.execute(
                    text(
                        """INSERT INTO projects
                        (id,slug,display_name,description,icon,created_by_user_id)
                        VALUES (:id,:slug,:name,'','folder',:user_id)"""
                    ),
                    {
                        "id": project_id,
                        "slug": slug,
                        "name": slug,
                        "user_id": str(ADMIN_ID),
                    },
                )

        mcp = await mcp_service.create_asset(actor, CreateMcpServer("override-mcp", "Override MCP"))
        credential = await credential_service.create(
            actor,
            CreateCredential("override-credential", "Override credential", "token"),
            {"env": {"TOKEN": "never-return"}},
        )

        app = FastAPI()
        app.include_router(admin_assets.admin_project_router)

        async def override_actor(project_id: uuid.UUID):
            return SystemAssetGovernanceContext(ADMIN_ID, "req-override-get", project_id)

        app.dependency_overrides[admin_assets._admin_project_actor] = override_actor
        app.dependency_overrides[admin_assets.get_mcp_service] = lambda: mcp_service
        app.dependency_overrides[admin_assets.get_credential_service] = lambda: credential_service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            mcp_path = f"/api/admin/projects/{PROJECT_ID}/assets/mcp-servers/{mcp.id}"
            credential_path = f"/api/admin/projects/{PROJECT_ID}/assets/credentials/{credential.id}"
            assert (await client.get(mcp_path)).status_code == 200
            assert (await client.get(credential_path)).status_code == 200

            cross_mcp = await client.get(f"/api/admin/projects/{other_project_id}/assets/mcp-servers/{mcp.id}")
            cross_credential = await client.get(f"/api/admin/projects/{other_project_id}/assets/credentials/{credential.id}")
            assert cross_mcp.status_code == cross_credential.status_code == 404

            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE projects SET is_suspended=true WHERE id=:id"),
                    {"id": PROJECT_ID},
                )
            assert (await client.get(mcp_path)).status_code == 404
            assert (await client.get(credential_path)).status_code == 404

            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE projects SET is_suspended=false,status='pending_deletion' WHERE id=:id"),
                    {"id": PROJECT_ID},
                )
            assert (await client.get(mcp_path)).status_code == 404
            assert (await client.get(credential_path)).status_code == 404

        async with engine.connect() as connection:
            membership_count = await connection.scalar(
                text("SELECT count(*) FROM project_memberships"),
            )
        assert membership_count == 0
    finally:
        await engine.dispose()
