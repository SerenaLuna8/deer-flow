from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit.service import AuditService, _bind_gateway_audit_process
from app.audit.sinks import OperationalAuditSink
from app.gateway.deps import project_session
from app.gateway.routers import projects
from app.projects.context import resolve_project_context
from app.projects.errors import ProjectNotFound
from app.projects.repository import ProjectRepository
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.config.quota_config import QuotaConfig


@pytest.mark.integration
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_api_and_repository_enforce_account_isolation(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    users = {
        name: uuid.uuid4()
        for name in (
            "alpha_admin",
            "alpha_editor",
            "alpha_runner",
            "alpha_viewer",
            "beta_admin",
            "outsider",
        )
    }
    app = FastAPI()
    app.include_router(projects.router)

    async def request_session():
        async with factory() as session:
            yield session

    async def request_identity(request: Request):
        user_id = uuid.UUID(request.headers["x-test-user"])
        return user_id, f"test-{user_id}"

    app.dependency_overrides[project_session] = request_session
    app.dependency_overrides[projects.authenticated_project_identity] = request_identity
    audit_keyring = AuditHmacKeyring.from_environment()
    audit_service = AuditService(factory, audit_keyring)
    app.state.operational_audit_sink = OperationalAuditSink(
        audit_service,
        process_context=_bind_gateway_audit_process(audit_service),
    )
    quota_service = QuotaService(factory, QuotaConfig(), source_ref_hasher=audit_keyring)
    app.state.project_quota_service = quota_service
    app.state.project_quota_enforcer = ProjectQuotaEnforcer(quota_service)

    def headers(name: str) -> dict[str, str]:
        return {"x-test-user": str(users[name])}

    try:
        async with engine.begin() as connection:
            for name, user_id in users.items():
                await connection.execute(
                    text(
                        """INSERT INTO users
                        (id,email,system_role,created_at,needs_setup,token_version)
                        VALUES (:id,:email,'user',:now,false,0)"""
                    ),
                    {"id": str(user_id), "email": f"{name}@example.invalid", "now": datetime.now(UTC)},
                )

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            alpha_response = await client.post(
                "/api/projects",
                headers=headers("alpha_admin"),
                json={"slug": "alpha", "display_name": "Alpha"},
            )
            beta_response = await client.post(
                "/api/projects",
                headers=headers("beta_admin"),
                json={"slug": "beta", "display_name": "Beta"},
            )
            assert alpha_response.status_code == beta_response.status_code == 201
            alpha_id = uuid.UUID(alpha_response.json()["id"])
            beta_id = uuid.UUID(beta_response.json()["id"])

            async with engine.begin() as connection:
                for name, role in (
                    ("alpha_editor", "editor"),
                    ("alpha_runner", "runner"),
                    ("alpha_viewer", "viewer"),
                ):
                    await connection.execute(
                        text(
                            """INSERT INTO project_memberships
                            (id,project_id,user_id,role) VALUES (:id,:project,:user,:role)"""
                        ),
                        {
                            "id": uuid.uuid4(),
                            "project": alpha_id,
                            "user": str(users[name]),
                            "role": role,
                        },
                    )

            alpha_admin, alpha_editor, alpha_runner, alpha_viewer, beta_admin, outsider = await asyncio.gather(
                client.get("/api/projects", headers=headers("alpha_admin")),
                client.get("/api/projects", headers=headers("alpha_editor")),
                client.get("/api/projects", headers=headers("alpha_runner")),
                client.get("/api/projects", headers=headers("alpha_viewer")),
                client.get("/api/projects", headers=headers("beta_admin")),
                client.get("/api/projects", headers=headers("outsider")),
            )
            assert [item["id"] for item in alpha_admin.json()["items"]] == [str(alpha_id)]
            assert [item["id"] for item in alpha_editor.json()["items"]] == [str(alpha_id)]
            assert [item["id"] for item in alpha_runner.json()["items"]] == [str(alpha_id)]
            assert [item["id"] for item in alpha_viewer.json()["items"]] == [str(alpha_id)]
            assert [item["id"] for item in beta_admin.json()["items"]] == [str(beta_id)]
            assert outsider.json()["items"] == []

            assert (await client.get(f"/api/projects/{alpha_id}", headers=headers("alpha_admin"))).status_code == 200
            assert (await client.get(f"/api/projects/{alpha_id}", headers=headers("alpha_editor"))).status_code == 200
            assert (await client.get(f"/api/projects/{alpha_id}", headers=headers("alpha_runner"))).status_code == 200
            assert (await client.get(f"/api/projects/{alpha_id}", headers=headers("alpha_viewer"))).status_code == 200
            updated = await client.patch(
                f"/api/projects/{alpha_id}",
                headers=headers("alpha_admin"),
                json={"display_name": "Alpha Updated"},
            )
            assert updated.status_code == 200
            assert updated.json()["display_name"] == "Alpha Updated"

            async def authority_snapshot(project_id: uuid.UUID) -> tuple[object, ...]:
                async with engine.connect() as connection:
                    project = (
                        await connection.execute(
                            text(
                                """SELECT display_name,status,is_suspended,membership_version,updated_at
                                FROM projects WHERE id=:project_id"""
                            ),
                            {"project_id": project_id},
                        )
                    ).one()
                    memberships = (
                        await connection.execute(
                            text(
                                """SELECT user_id,role,status,version,is_pinned,last_entered_at,updated_at
                                FROM project_memberships WHERE project_id=:project_id
                                ORDER BY user_id"""
                            ),
                            {"project_id": project_id},
                        )
                    ).all()
                    audit_count = (
                        await connection.execute(
                            text("SELECT count(*) FROM audit_logs WHERE project_id=:project_id"),
                            {"project_id": project_id},
                        )
                    ).scalar_one()
                    project_count = (await connection.execute(text("SELECT count(*) FROM projects"))).scalar_one()
                return tuple(project), tuple(tuple(row) for row in memberships), audit_count, project_count

            authority_before_denials = await authority_snapshot(alpha_id)
            forged_create = await client.post(
                "/api/projects",
                headers=headers("alpha_admin"),
                json={
                    "slug": "forged-authority",
                    "display_name": "Forged Authority",
                    "created_by_user_id": str(users["beta_admin"]),
                    "owner_user_id": str(users["beta_admin"]),
                },
            )
            forged_patch = await client.patch(
                f"/api/projects/{alpha_id}",
                headers=headers("alpha_admin"),
                json={
                    "display_name": "Forged Authority",
                    "project_id": str(beta_id),
                    "owner_user_id": str(users["beta_admin"]),
                },
            )
            for rejected in (forged_create, forged_patch):
                assert rejected.status_code == 422
                assert rejected.json()["detail"]["code"] == "PROJECT_VALIDATION_FAILED"
            forbidden = await client.patch(
                f"/api/projects/{alpha_id}",
                headers=headers("alpha_viewer"),
                json={"display_name": "Denied"},
            )
            assert forbidden.status_code == 403
            assert forbidden.json()["detail"]["code"] == "PROJECT_FORBIDDEN"
            for name in ("beta_admin", "outsider"):
                hidden_responses = (
                    await client.get(f"/api/projects/{alpha_id}", headers=headers(name)),
                    await client.patch(
                        f"/api/projects/{alpha_id}",
                        headers=headers(name),
                        json={"display_name": "Hidden"},
                    ),
                    await client.post(f"/api/projects/{alpha_id}/enter", headers=headers(name)),
                    await client.put(
                        f"/api/projects/{alpha_id}/pin",
                        headers=headers(name),
                        json={"pinned": True},
                    ),
                )
                for hidden in hidden_responses:
                    assert hidden.status_code == 404
                    assert hidden.json()["detail"]["code"] == "PROJECT_NOT_FOUND"
            assert await authority_snapshot(alpha_id) == authority_before_denials

            entered = await client.post(f"/api/projects/{alpha_id}/enter", headers=headers("alpha_viewer"))
            pinned = await client.put(
                f"/api/projects/{alpha_id}/pin",
                headers=headers("alpha_viewer"),
                json={"pinned": True},
            )
            assert entered.status_code == pinned.status_code == 200
            assert pinned.json()["is_pinned"] is True
            admin_view = await client.get(f"/api/projects/{alpha_id}", headers=headers("alpha_admin"))
            assert admin_view.json()["is_pinned"] is False

            created_by_outsider = await client.post(
                "/api/projects",
                headers=headers("outsider"),
                json={"slug": "gamma", "display_name": "Gamma"},
            )
            assert created_by_outsider.status_code == 201
            assert created_by_outsider.json()["role"] == "admin"
            gamma_id = uuid.UUID(created_by_outsider.json()["id"])

            async with factory() as session:
                context = await resolve_project_context(session, users["alpha_viewer"], alpha_id, "context-ok")
            async with factory() as session:
                with pytest.raises(ProjectNotFound):
                    await resolve_project_context(session, users["beta_admin"], alpha_id, "context-hidden")
            forged = replace(context, user_id=users["beta_admin"], request_id="forged")
            async with factory() as session:
                with pytest.raises(ProjectNotFound):
                    await ProjectRepository(session).get(forged)

            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE project_memberships SET version=version+1 WHERE id=:membership_id"),
                    {"membership_id": context.membership_id},
                )
            stale_snapshot = await authority_snapshot(alpha_id)
            async with factory() as session:
                with pytest.raises(ProjectNotFound):
                    await ProjectRepository(session).get(context)
            assert await authority_snapshot(alpha_id) == stale_snapshot

            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE project_memberships SET status='removed' WHERE id=:membership_id"),
                    {"membership_id": context.membership_id},
                )
            removed_snapshot = await authority_snapshot(alpha_id)
            removed_response = await client.get(f"/api/projects/{alpha_id}", headers=headers("alpha_viewer"))
            assert removed_response.status_code == 404
            assert removed_response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"
            assert await authority_snapshot(alpha_id) == removed_snapshot

            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """UPDATE project_memberships SET status='left'
                        WHERE project_id=:project_id AND user_id=:user_id"""
                    ),
                    {"project_id": alpha_id, "user_id": str(users["alpha_admin"])},
                )
            left_snapshot = await authority_snapshot(alpha_id)
            left_response = await client.get(f"/api/projects/{alpha_id}", headers=headers("alpha_admin"))
            assert left_response.status_code == 404
            assert left_response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"
            assert await authority_snapshot(alpha_id) == left_snapshot

            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE projects SET is_suspended=true WHERE id=:project_id"),
                    {"project_id": beta_id},
                )
            suspended_snapshot = await authority_snapshot(beta_id)
            suspended_response = await client.get(f"/api/projects/{beta_id}", headers=headers("beta_admin"))
            assert suspended_response.status_code == 404
            assert suspended_response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"
            assert await authority_snapshot(beta_id) == suspended_snapshot

            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE projects SET status='pending_deletion' WHERE id=:project_id"),
                    {"project_id": gamma_id},
                )
            pending_snapshot = await authority_snapshot(gamma_id)
            pending_response = await client.get(f"/api/projects/{gamma_id}", headers=headers("outsider"))
            assert pending_response.status_code == 404
            assert pending_response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"
            assert await authority_snapshot(gamma_id) == pending_snapshot
    finally:
        await engine.dispose()
