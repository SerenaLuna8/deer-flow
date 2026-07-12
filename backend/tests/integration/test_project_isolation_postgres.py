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

from app.gateway.deps import project_session
from app.gateway.routers import projects
from app.projects.context import resolve_project_context
from app.projects.errors import ProjectNotFound
from app.projects.repository import ProjectRepository


@pytest.mark.integration
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_api_and_repository_enforce_account_isolation(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    users = {name: uuid.uuid4() for name in ("alpha_admin", "alpha_viewer", "beta_admin", "outsider")}
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
                await connection.execute(
                    text(
                        """INSERT INTO project_memberships
                        (id,project_id,user_id,role) VALUES (:id,:project,:user,'viewer')"""
                    ),
                    {"id": uuid.uuid4(), "project": alpha_id, "user": str(users["alpha_viewer"])},
                )

            alpha_admin, alpha_viewer, beta_admin, outsider = await asyncio.gather(
                client.get("/api/projects", headers=headers("alpha_admin")),
                client.get("/api/projects", headers=headers("alpha_viewer")),
                client.get("/api/projects", headers=headers("beta_admin")),
                client.get("/api/projects", headers=headers("outsider")),
            )
            assert [item["id"] for item in alpha_admin.json()["items"]] == [str(alpha_id)]
            assert [item["id"] for item in alpha_viewer.json()["items"]] == [str(alpha_id)]
            assert [item["id"] for item in beta_admin.json()["items"]] == [str(beta_id)]
            assert outsider.json()["items"] == []

            assert (await client.get(f"/api/projects/{alpha_id}", headers=headers("alpha_admin"))).status_code == 200
            assert (await client.get(f"/api/projects/{alpha_id}", headers=headers("alpha_viewer"))).status_code == 200
            updated = await client.patch(
                f"/api/projects/{alpha_id}",
                headers=headers("alpha_admin"),
                json={"display_name": "Alpha Updated"},
            )
            assert updated.status_code == 200
            assert updated.json()["display_name"] == "Alpha Updated"
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

        async with factory() as session:
            context = await resolve_project_context(session, users["alpha_viewer"], alpha_id, "context-ok")
        async with factory() as session:
            with pytest.raises(ProjectNotFound):
                await resolve_project_context(session, users["beta_admin"], alpha_id, "context-hidden")
        forged = replace(context, user_id=users["beta_admin"], request_id="forged")
        async with factory() as session:
            with pytest.raises(ProjectNotFound):
                await ProjectRepository(session).get(forged)
    finally:
        await engine.dispose()
