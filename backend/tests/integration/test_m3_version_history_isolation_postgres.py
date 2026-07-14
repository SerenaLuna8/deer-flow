from __future__ import annotations

import importlib
import json
import uuid
from base64 import b64encode
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.projects.context import ProjectContext, resolve_project_context
from app.shared_assets.binding_service import BindingService
from app.shared_assets.contexts import SystemAssetGovernanceContext, SystemAssetReadContext
from app.shared_assets.errors import AssetForbidden, AssetNotFound
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetSelection,
    SkillArchiveFile,
)
from deerflow.persistence.shared_assets import CredentialRow


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


async def _seed_system_admin(engine: AsyncEngine) -> SystemAssetGovernanceContext:
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users
                (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'system_admin',:now,false,0)"""
            ),
            {
                "id": str(user_id),
                "email": f"history-system-{user_id}@example.com",
                "now": now,
            },
        )
    return SystemAssetGovernanceContext(
        user_id=user_id,
        request_id="req-history-system",
    )


def _agent_payload() -> AgentPayload:
    return AgentPayload(
        description="History",
        soul="Keep exact version history.",
        model_ref="default",
        tool_groups=(),
        skill_version_ids=(),
        mcp_version_ids=(),
    )


def _skill_archive() -> tuple[SkillArchiveFile, ...]:
    return (
        SkillArchiveFile(
            "SKILL.md",
            b"---\nname: history-skill\ndescription: History\n---\n\nRead history.\n",
            "text/markdown",
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "skill", "mcp"])
async def test_authenticated_reader_lists_real_postgres_system_catalog_only(
    migrated_postgres_database_url: str,
    kind: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_system_admin(engine)
    reader = SystemAssetReadContext(
        user_id=uuid.uuid4(),
        request_id=f"req-catalog-reader-{kind}",
    )
    try:
        service, asset, _draft = await _create_asset_with_version(
            kind,
            factory,
            admin,
            label=f"catalog-{kind}",
        )

        visible = await service.list_visible(reader)

        assert [item.id for item in visible] == [asset.id]
        with pytest.raises(AssetForbidden):
            await service.get_version_history(reader, asset.id)
    finally:
        await engine.dispose()


async def _create_asset_with_version(
    kind: str,
    factory,
    actor: ProjectContext | SystemAssetGovernanceContext,
    *,
    label: str,
):
    if kind == "agent":
        module = importlib.import_module("app.shared_assets.agent_service")
        service = module.AgentService(factory)
        asset = await service.create_asset(
            actor,
            module.CreateAgent(f"{label}-agent", f"{label} Agent"),
        )
        draft = await service.create_version(
            actor,
            asset.id,
            _agent_payload(),
            expected_asset_version=1,
        )
    elif kind == "skill":
        module = importlib.import_module("app.shared_assets.skill_service")
        service = module.SkillService(factory)
        asset = await service.create_asset(
            actor,
            module.CreateSkill(f"{label}-skill", f"{label} Skill"),
        )
        draft = await service.create_version_from_archive(
            actor,
            asset.id,
            _skill_archive(),
            expected_asset_version=1,
        )
    else:
        module = importlib.import_module("app.shared_assets.mcp_service")
        service = module.McpService(factory)
        asset = await service.create_asset(
            actor,
            module.CreateMcpServer(f"{label}-mcp", f"{label} MCP"),
        )
        draft = await service.create_version(
            actor,
            asset.id,
            module.McpDefinition(
                description="History",
                transport="http",
                url="https://history.example.test",
            ),
            expected_asset_version=1,
        )
    return service, asset, draft


async def _create_next_draft(kind: str, service, actor, asset_id: uuid.UUID):
    if kind == "agent":
        return await service.create_version(
            actor,
            asset_id,
            _agent_payload(),
            expected_asset_version=3,
        )
    if kind == "skill":
        return await service.create_version_from_archive(
            actor,
            asset_id,
            _skill_archive(),
            expected_asset_version=3,
        )
    module = importlib.import_module("app.shared_assets.mcp_service")
    return await service.create_version(
        actor,
        asset_id,
        module.McpDefinition(
            description="New draft",
            transport="http",
            url="https://new-history.example.test",
        ),
        expected_asset_version=3,
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


async def _create_empty_asset(kind: str, factory, context: ProjectContext):
    if kind == "mcp":
        module = importlib.import_module("app.shared_assets.mcp_service")
        service = module.McpService(factory)
        asset = await service.create_asset(
            context,
            module.CreateMcpServer("empty-history-mcp", "Empty History MCP"),
        )
        return service, asset.id

    module = importlib.import_module("app.shared_assets.credential_service")
    service = module.CredentialService(factory)
    row = CredentialRow(
        scope="project",
        project_id=context.project_id,
        name="empty-history-credential",
        display_name="Empty History Credential",
        credential_type="token",
        created_by_user_id=str(context.user_id),
    )
    async with factory() as session, session.begin():
        session.add(row)
    return service, row.id


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
async def test_authorized_parent_without_versions_returns_empty_history(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    _configure_keyring(monkeypatch)
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    context = await _seed_project(engine, factory, label=f"empty-history-{kind}")
    statements: list[str] = []

    def record_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    try:
        service, asset_id = await _create_empty_asset(kind, factory, context)
        event.listen(engine.sync_engine, "before_cursor_execute", record_statement)

        assert await service.get_version_history(context, asset_id) == ()
        assert len(statements) == 1
        assert "LEFT OUTER JOIN" in statements[0].upper()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_statement)
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
    async with engine.begin() as connection:
        await connection.execute(
            text(expiry_sql),
            {
                "membership_id": context.membership_id,
                "project_id": context.project_id,
            },
        )
    try:
        with pytest.raises(AssetNotFound):
            await service.get_version_history(context, asset_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "skill", "mcp"])
async def test_project_history_exposes_only_published_system_versions(
    migrated_postgres_database_url: str,
    kind: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    context = await _seed_project(engine, factory, label=f"system-history-{kind}")
    system = await _seed_system_admin(engine)
    try:
        service, asset, draft = await _create_asset_with_version(
            kind,
            factory,
            system,
            label=f"system-history-{kind}",
        )
        published = await service.publish(
            system,
            asset.id,
            draft.id,
            expected_asset_version=2,
        )
        newer_draft = await _create_next_draft(kind, service, system, asset.id)

        history = await service.get_version_history(context, asset.id)

        assert [version.id for version in history] == [published.id]
        assert newer_draft.id not in {version.id for version in history}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "skill", "mcp"])
async def test_project_history_rejects_cross_project_and_stale_context(
    migrated_postgres_database_url: str,
    kind: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = await _seed_project(engine, factory, label=f"project-history-{kind}")
    outsider = await _seed_project(engine, factory, label=f"project-outsider-{kind}")
    try:
        service, asset, _draft = await _create_asset_with_version(
            kind,
            factory,
            owner,
            label=f"project-history-{kind}",
        )

        with pytest.raises(AssetNotFound):
            await service.get_version_history(outsider, asset.id)

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE project_memberships SET version=version+1 WHERE id=:membership_id"),
                {"membership_id": owner.membership_id},
            )
        with pytest.raises(AssetNotFound):
            await service.get_version_history(owner, asset.id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_asset_read_model_keeps_unbound_system_asset_and_persisted_binding_scoped(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    context = await _seed_project(engine, factory, label="binding-read-model")
    outsider = await _seed_project(engine, factory, label="binding-read-outsider")
    system = await _seed_system_admin(engine)
    try:
        agent_service, asset, draft = await _create_asset_with_version(
            "agent",
            factory,
            system,
            label="binding-read-model",
        )
        published = await agent_service.publish(
            system,
            asset.id,
            draft.id,
            expected_asset_version=2,
        )

        assert asset.id in {visible.id for visible in await agent_service.list_visible(context)}

        binding_service = BindingService(factory)
        created = await binding_service.enable(
            context,
            AssetSelection(AssetKind.AGENT, asset.id, published.id),
        )
        persisted = await binding_service.list_visible(context, AssetKind.AGENT)
        assert persisted == (created,)
        assert await binding_service.list_visible(outsider, AssetKind.AGENT) == ()

        forged = replace(outsider, project_id=context.project_id)
        with pytest.raises(AssetNotFound):
            await binding_service.list_visible(forged, AssetKind.AGENT)

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE project_memberships SET version=version+1 WHERE id=:membership_id"),
                {"membership_id": context.membership_id},
            )
        with pytest.raises(AssetNotFound):
            await binding_service.list_visible(context, AssetKind.AGENT)
    finally:
        await engine.dispose()
