from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from support.m4_private_threads import (
    M4ThreadSeed,
    install_open_project_cutover_guard,
    seed_m4_thread_database,
)

from app.gateway.deps import get_current_user_from_request, project_session
from app.gateway.routers import project_connections
from app.private_work.connection_service import ProjectConnectionService
from deerflow.config.channel_connections_config import ChannelConnectionsConfig
from deerflow.persistence.channel_connections import ChannelConnectionRepository


@pytest_asyncio.fixture()
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


def _app(seed: M4ThreadSeed, identity: dict[str, uuid.UUID]) -> FastAPI:
    app = FastAPI()
    install_open_project_cutover_guard(app)
    app.include_router(project_connections.router)
    repository = ChannelConnectionRepository(seed.factory)
    app.state.project_connection_service = ProjectConnectionService(
        seed.factory,
        repository=repository,
    )
    app.state.channel_connections_config = ChannelConnectionsConfig.model_validate({"enabled": True, "slack": {"enabled": True}})
    app.state.channels_config = {
        "slack": {
            "enabled": True,
            "bot_token": "test-bot-token",
            "app_token": "test-app-token",
        }
    }

    async def request_session():
        async with seed.factory() as session:
            yield session

    async def current_user():
        return SimpleNamespace(id=identity["user_id"])

    app.dependency_overrides[project_session] = request_session
    app.dependency_overrides[get_current_user_from_request] = current_user
    return app


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_connection_routes_run_for_runner_and_editor(
    seed: M4ThreadSeed,
) -> None:
    identity = {"user_id": seed.owner_b.user_id}
    app = _app(seed, identity)
    repository = ChannelConnectionRepository(seed.factory)
    project_id = seed.owner_b.project_id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        begun = await client.post(
            f"/api/projects/{project_id}/connections/slack/connect",
            json={
                "agent_asset_id": str(seed.project_agent_id),
                "agent_scope": "project",
                "redirect_after": f"/projects/{project_id}/connections",
            },
        )
        assert begun.status_code == 200
        assert begun.json()["provider"] == "slack"
        assert begun.json()["mode"] == "binding_code"
        assert begun.json()["code"]
        assert begun.json()["url"] is None
        assert begun.json()["instruction"].startswith("Send /connect")
        assert begun.json()["expires_in"] == 600

        connection = await repository.upsert_connection(
            scope=seed.owner_b_scope,
            provider="slack",
            external_account_id="runner-account",
            workspace_id="runner-workspace",
            metadata={"agent_asset_id": str(seed.project_agent_id)},
        )
        listed = await client.get(f"/api/projects/{project_id}/connections")
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()["connections"]] == [connection["id"]]
        assert "credentials" not in listed.text
        assert "access_token" not in listed.text

        deleted = await client.delete(f"/api/projects/{project_id}/connections/{connection['id']}")
        assert deleted.status_code == 204

        async with seed.engine.begin() as connection_handle:
            await connection_handle.execute(
                text(
                    """UPDATE project_memberships
                    SET role='editor', version=version + 1
                    WHERE project_id=:project_id AND user_id=:user_id"""
                ),
                {
                    "project_id": project_id,
                    "user_id": str(seed.owner_b.user_id),
                },
            )

        editor_begun = await client.post(
            f"/api/projects/{project_id}/connections/slack/connect",
            json={
                "agent_asset_id": str(seed.system_agent_id),
                "agent_scope": "system",
            },
        )
        assert editor_begun.status_code == 200
        editor_connection = await repository.upsert_connection(
            scope=seed.owner_b_scope,
            provider="slack",
            external_account_id="editor-account",
            workspace_id="editor-workspace",
        )
        editor_listed = await client.get(f"/api/projects/{project_id}/connections")
        assert [row["id"] for row in editor_listed.json()["connections"]] == [
            editor_connection["id"],
            connection["id"],
        ]
        editor_deleted = await client.delete(f"/api/projects/{project_id}/connections/{editor_connection['id']}")
        assert editor_deleted.status_code == 204


@pytest.mark.postgres
@pytest.mark.anyio
async def test_viewer_can_list_but_cannot_connect_or_disconnect(
    seed: M4ThreadSeed,
) -> None:
    identity = {"user_id": seed.viewer.user_id}
    app = _app(seed, identity)
    project_id = seed.viewer.project_id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        listed = await client.get(f"/api/projects/{project_id}/connections")
        assert listed.status_code == 200
        assert listed.json() == {"connections": []}

        forbidden_connect = await client.post(
            f"/api/projects/{project_id}/connections/slack/connect",
            json={
                "agent_asset_id": str(seed.project_agent_id),
                "agent_scope": "project",
            },
        )
        forbidden_delete = await client.delete(f"/api/projects/{project_id}/connections/{uuid.uuid4().hex}")
        for response in (forbidden_connect, forbidden_delete):
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "PRIVATE_WORK_FORBIDDEN"

        invalid = await client.post(
            f"/api/projects/{project_id}/connections/slack/connect",
            json={
                "agent_asset_id": str(seed.project_agent_id),
                "agent_scope": "project",
                "owner_user_id": str(seed.owner_a.user_id),
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"
