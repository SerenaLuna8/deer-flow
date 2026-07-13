from __future__ import annotations

import importlib
import json
import uuid
from base64 import b64encode
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.projects.context import ProjectContext, resolve_project_context
from app.shared_assets.errors import AssetNotFound


async def _seed_project(
    engine: AsyncEngine,
    factory: async_sessionmaker,
    *,
    label: str,
) -> ProjectContext:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users
                (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'user',:now,false,0)"""
            ),
            {
                "id": str(user_id),
                "email": f"{label}-{user_id}@example.com",
                "now": now,
            },
        )
        await connection.execute(
            text(
                """INSERT INTO projects
                (id,slug,display_name,created_by_user_id,created_at,updated_at)
                VALUES (:id,:slug,:name,:user,:now,:now)"""
            ),
            {
                "id": project_id,
                "slug": f"{label}-{str(project_id)[:8]}",
                "name": label,
                "user": str(user_id),
                "now": now,
            },
        )
        await connection.execute(
            text(
                """INSERT INTO project_memberships
                (id,project_id,user_id,role,status,version)
                VALUES (:id,:project,:user,'admin','active',1)"""
            ),
            {
                "id": membership_id,
                "project": project_id,
                "user": str(user_id),
            },
        )
    async with factory() as session:
        return await resolve_project_context(
            session,
            user_id,
            project_id,
            f"req-{label}",
        )


def _configure_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", "history-key")
    monkeypatch.setenv(
        "DEER_FLOW_CREDENTIAL_KEYRING_JSON",
        json.dumps({"history-key": b64encode(b"h" * 32).decode("ascii")}),
    )


async def _create_versioned_asset(kind: str, factory, context: ProjectContext):
    if kind == "mcp":
        module = importlib.import_module("app.shared_assets.mcp_service")
        service = module.McpService(factory)
        asset = await service.create_asset(
            context,
            module.CreateMcpServer("history-mcp", "History MCP"),
        )
        await service.create_version(
            context,
            asset.id,
            module.McpDefinition(
                description="History",
                transport="http",
                url="https://mcp.example.test",
            ),
            expected_asset_version=1,
        )
        return service, asset.id

    module = importlib.import_module("app.shared_assets.credential_service")
    service = module.CredentialService(factory)
    asset = await service.create(
        context,
        module.CreateCredential("history-credential", "History Credential", "token"),
        {"env": {"TOKEN": "write-only-history-secret"}},
    )
    return service, asset.id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["mcp", "credential"])
async def test_history_hides_cross_project_assets(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    _configure_keyring(monkeypatch)
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _seed_project(engine, factory, label=f"history-{kind}-owner")
    outsider = await _seed_project(engine, factory, label=f"history-{kind}-outsider")
    try:
        service, asset_id = await _create_versioned_asset(kind, factory, owner)

        with pytest.raises(AssetNotFound):
            await service.get_version_history(outsider, asset_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["mcp", "credential"])
@pytest.mark.parametrize(
    "expiry_sql",
    [
        "UPDATE project_memberships SET version=version+1 WHERE id=:membership_id",
        "UPDATE project_memberships SET status='left' WHERE id=:membership_id",
        "UPDATE projects SET is_suspended=true WHERE id=:project_id",
        "UPDATE projects SET status='pending_deletion' WHERE id=:project_id",
    ],
    ids=[
        "stale-membership-version",
        "inactive-membership",
        "suspended-project",
        "inactive-project",
    ],
)
async def test_history_rechecks_project_context_in_scoped_version_query(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expiry_sql: str,
) -> None:
    _configure_keyring(monkeypatch)
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    context = await _seed_project(engine, factory, label=f"history-{kind}-expiry")
    service, asset_id = await _create_versioned_asset(kind, factory, context)
    if kind == "mcp":
        repository_module = importlib.import_module("app.shared_assets.mcp_repository")
        repository_type = repository_module.McpRepository
        method_name = "get_project_asset"
    else:
        repository_module = importlib.import_module("app.shared_assets.credential_repository")
        repository_type = repository_module.CredentialRepository
        method_name = "get_project_credential"
    original = getattr(repository_type, method_name)
    expired = False

    async def expire_after_asset_check(repository, actor, requested_asset_id, **kwargs):
        nonlocal expired
        row = await original(repository, actor, requested_asset_id, **kwargs)
        if not expired:
            expired = True
            async with engine.begin() as connection:
                await connection.execute(
                    text(expiry_sql),
                    {
                        "membership_id": context.membership_id,
                        "project_id": context.project_id,
                    },
                )
        return row

    monkeypatch.setattr(repository_type, method_name, expire_after_asset_check)
    try:
        assert await service.get_version_history(context, asset_id) == ()
        assert expired is True
    finally:
        await engine.dispose()
