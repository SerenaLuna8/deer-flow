from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.projects.context import ProjectContext, resolve_project_context
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import (
    AssetConflict,
    AssetResolutionUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.keyring import CredentialKeyring
from app.shared_assets.models import AssetKind, AssetSelection, ResolvedMcpSnapshot


async def _seed_project(
    engine: AsyncEngine,
    factory: async_sessionmaker,
    *,
    label: str,
    role: str = "admin",
) -> ProjectContext:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'user',:now,false,0)"""
            ),
            {"id": str(user_id), "email": f"{label}-{user_id}@example.com", "now": now},
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
                VALUES (:id,:project,:user,:role,'active',1)"""
            ),
            {
                "id": membership_id,
                "project": project_id,
                "user": str(user_id),
                "role": role,
            },
        )
    async with factory() as session:
        return await resolve_project_context(session, user_id, project_id, f"req-{label}")


async def _seed_system_admin(engine: AsyncEngine) -> SystemAssetGovernanceContext:
    user_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'system_admin',:now,false,0)"""
            ),
            {
                "id": str(user_id),
                "email": f"system-{user_id}@example.com",
                "now": datetime.now(UTC),
            },
        )
    return SystemAssetGovernanceContext(user_id=user_id, request_id="req-system")


async def _seed_agent(
    engine: AsyncEngine,
    *,
    owner_id: uuid.UUID,
    scope: str,
    project_id: uuid.UUID | None,
    versions: int = 2,
    skill_version_ids: tuple[uuid.UUID, ...] = (),
    mcp_version_ids: tuple[uuid.UUID, ...] = (),
) -> tuple[uuid.UUID, tuple[uuid.UUID, ...]]:
    asset_id = uuid.uuid4()
    version_ids = tuple(uuid.uuid4() for _ in range(versions))
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO agents
                (id,scope,project_id,slug,display_name,status,created_by_user_id)
                VALUES (:id,:scope,:project,:slug,:slug,'active',:user)"""
            ),
            {
                "id": asset_id,
                "scope": scope,
                "project": project_id,
                "slug": f"agent-{str(asset_id)[:8]}",
                "user": str(owner_id),
            },
        )
        for number, version_id in enumerate(version_ids, 1):
            await connection.execute(
                text(
                    """INSERT INTO agent_versions
                    (id,agent_id,version_number,workflow_status,description,soul,
                     model_ref,tool_groups,payload_checksum,created_by_user_id)
                    VALUES (:id,:asset,:number,'draft',:description,:soul,
                            'default','[\"research\"]'::jsonb,:checksum,:user)"""
                ),
                {
                    "id": version_id,
                    "asset": asset_id,
                    "number": number,
                    "description": f"version {number}",
                    "soul": f"agent soul {number}",
                    "checksum": f"{number:x}" * 64,
                    "user": str(owner_id),
                },
            )
            for index, dependency_id in enumerate(skill_version_ids):
                await connection.execute(
                    text(
                        """INSERT INTO agent_version_skill_refs
                        (agent_version_id,skill_version_id,sort_order)
                        VALUES (:version,:dependency,:position)"""
                    ),
                    {"version": version_id, "dependency": dependency_id, "position": index},
                )
            for index, dependency_id in enumerate(mcp_version_ids):
                await connection.execute(
                    text(
                        """INSERT INTO agent_version_mcp_refs
                        (agent_version_id,mcp_server_version_id,sort_order)
                        VALUES (:version,:dependency,:position)"""
                    ),
                    {"version": version_id, "dependency": dependency_id, "position": index},
                )
            await connection.execute(
                text("UPDATE agent_versions SET workflow_status='published' WHERE id=:version"),
                {"version": version_id},
            )
        await connection.execute(
            text("UPDATE agents SET current_published_version_id=:version WHERE id=:asset"),
            {"version": version_ids[-1], "asset": asset_id},
        )
    return asset_id, version_ids


async def _seed_skill(
    engine: AsyncEngine,
    *,
    owner_id: uuid.UUID,
    scope: str,
    project_id: uuid.UUID | None,
    versions: int = 1,
) -> tuple[uuid.UUID, tuple[uuid.UUID, ...]]:
    asset_id = uuid.uuid4()
    version_ids = tuple(uuid.uuid4() for _ in range(versions))
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO skills
                (id,scope,project_id,slug,display_name,status,created_by_user_id)
                VALUES (:id,:scope,:project,:slug,:slug,'active',:user)"""
            ),
            {
                "id": asset_id,
                "scope": scope,
                "project": project_id,
                "slug": f"skill-{str(asset_id)[:8]}",
                "user": str(owner_id),
            },
        )
        for number, version_id in enumerate(version_ids, 1):
            content = f"---\nname: demo-{number}\ndescription: demo\n---\nbody\n".encode()
            file_sha = hashlib.sha256(content).hexdigest()
            canonical = json.dumps(
                [{"path": "SKILL.md", "sha256": file_sha, "size_bytes": len(content)}],
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            checksum = hashlib.sha256(canonical).hexdigest()
            await connection.execute(
                text(
                    """INSERT INTO skill_versions
                    (id,skill_id,version_number,workflow_status,description,frontmatter,
                     secret_requirements,scan_decision,scan_summary,payload_checksum,
                     created_by_user_id)
                    VALUES (:id,:asset,:number,'draft','demo','{}'::jsonb,
                            '[]'::jsonb,'allow','{}'::jsonb,:checksum,:user)"""
                ),
                {
                    "id": version_id,
                    "asset": asset_id,
                    "number": number,
                    "checksum": checksum,
                    "user": str(owner_id),
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO skill_version_files
                    (skill_version_id,path,media_type,size_bytes,sha256,content)
                    VALUES (:version,'SKILL.md','text/markdown',:size,:sha,:content)"""
                ),
                {
                    "version": version_id,
                    "size": len(content),
                    "sha": file_sha,
                    "content": content,
                },
            )
            await connection.execute(
                text("UPDATE skill_versions SET workflow_status='published' WHERE id=:version"),
                {"version": version_id},
            )
        await connection.execute(
            text("UPDATE skills SET current_published_version_id=:version WHERE id=:asset"),
            {"version": version_ids[-1], "asset": asset_id},
        )
    return asset_id, version_ids


async def _generation(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return int((await connection.execute(text("SELECT generation FROM asset_catalog_state WHERE id=1"))).scalar_one())


@pytest.mark.asyncio
async def test_system_binding_is_pinned_and_archived_resolves_but_suspended_fails(
    migrated_postgres_database_url: str,
) -> None:
    from app.shared_assets.binding_service import BindingService
    from app.shared_assets.resolver import ProjectAssetResolver

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="pinning")
    outsider = await _seed_project(engine, factory, label="unbound")
    system = await _seed_system_admin(engine)
    asset_id, versions = await _seed_agent(
        engine,
        owner_id=system.user_id,
        scope="system",
        project_id=None,
    )
    service = BindingService(factory)
    resolver = ProjectAssetResolver(factory)
    try:
        before = await _generation(engine)
        binding = await service.enable(admin, AssetSelection(AssetKind.AGENT, asset_id, versions[0]))
        assert binding.version_id == versions[0]
        assert binding.version == 1
        assert await _generation(engine) > before

        snapshot = await resolver.resolve_project_asset_snapshot(
            admin,
            AssetSelection(AssetKind.AGENT, asset_id),
        )
        assert snapshot.version_id == versions[0]
        assert snapshot.payload.soul == "agent soul 1"

        with pytest.raises(AssetResolutionUnavailable):
            await resolver.resolve_project_asset_snapshot(
                outsider,
                AssetSelection(AssetKind.AGENT, asset_id),
            )

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE agents SET status='archived' WHERE id=:asset"),
                {"asset": asset_id},
            )
        archived = await resolver.resolve_project_asset_snapshot(
            admin,
            AssetSelection(AssetKind.AGENT, asset_id),
        )
        assert archived.version_id == versions[0]

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE agents SET status='suspended' WHERE id=:asset"),
                {"asset": asset_id},
            )
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.resolve_project_asset_snapshot(
                admin,
                AssetSelection(AssetKind.AGENT, asset_id),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_binding_dependency_closure_and_concurrent_upgrade_are_fail_closed(
    migrated_postgres_database_url: str,
) -> None:
    from app.shared_assets.binding_service import BindingService, SystemAssetBinding
    from app.shared_assets.resolver import ProjectAssetResolver

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="closure")
    system = await _seed_system_admin(engine)
    skill_id, skill_versions = await _seed_skill(
        engine,
        owner_id=system.user_id,
        scope="system",
        project_id=None,
    )
    agent_id, agent_versions = await _seed_agent(
        engine,
        owner_id=system.user_id,
        scope="system",
        project_id=None,
        skill_version_ids=(skill_versions[0],),
    )
    first = BindingService(factory)
    second = BindingService(factory)
    resolver = ProjectAssetResolver(factory)
    try:
        with pytest.raises(AssetValidationFailed):
            await first.enable(
                admin,
                AssetSelection(AssetKind.AGENT, agent_id, agent_versions[0]),
            )

        await first.enable(
            admin,
            AssetSelection(AssetKind.SKILL, skill_id, skill_versions[0]),
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE skills SET status='suspended' WHERE id=:asset"),
                {"asset": skill_id},
            )
        with pytest.raises(AssetValidationFailed):
            await first.enable(
                admin,
                AssetSelection(AssetKind.AGENT, agent_id, agent_versions[0]),
            )
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE skills SET status='active' WHERE id=:asset"),
                {"asset": skill_id},
            )
        await first.enable(
            admin,
            AssetSelection(AssetKind.AGENT, agent_id, agent_versions[0]),
        )

        outcomes = await asyncio.gather(
            first.upgrade(
                admin,
                AssetSelection(AssetKind.AGENT, agent_id, agent_versions[1]),
                expected_binding_version=1,
            ),
            second.upgrade(
                admin,
                AssetSelection(AssetKind.AGENT, agent_id, agent_versions[1]),
                expected_binding_version=1,
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(item, SystemAssetBinding) for item in outcomes) == 1
        assert sum(isinstance(item, AssetConflict) for item in outcomes) == 1
        assert (
            await resolver.resolve_project_asset_snapshot(
                admin,
                AssetSelection(AssetKind.AGENT, agent_id),
            )
        ).version_id == agent_versions[1]

        with pytest.raises(AssetConflict):
            await first.upgrade(
                admin,
                AssetSelection(AssetKind.AGENT, agent_id, agent_versions[0]),
                expected_binding_version=2,
            )
        rolled_back = await first.rollback(
            admin,
            AssetSelection(AssetKind.AGENT, agent_id, agent_versions[0]),
            expected_binding_version=2,
        )
        assert rolled_back.version_id == agent_versions[0]
        assert rolled_back.version == 3
        with pytest.raises(AssetConflict):
            await first.rollback(
                admin,
                AssetSelection(AssetKind.AGENT, agent_id, agent_versions[1]),
                expected_binding_version=3,
            )

        disabled = await first.disable(
            admin,
            AssetSelection(AssetKind.AGENT, agent_id),
            expected_binding_version=3,
        )
        assert disabled.enabled is False
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.resolve_project_asset_snapshot(
                admin,
                AssetSelection(AssetKind.AGENT, agent_id),
            )
        reenabled = await first.enable(
            admin,
            AssetSelection(AssetKind.AGENT, agent_id, agent_versions[1]),
            expected_binding_version=4,
        )
        assert reenabled.enabled is True
        assert reenabled.version == 5
        assert (
            await resolver.resolve_project_asset_snapshot(
                admin,
                AssetSelection(AssetKind.AGENT, agent_id),
            )
        ).version_id == agent_versions[1]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_current_pointer_and_mcp_secrets_recheck_revocation(
    migrated_postgres_database_url: str,
) -> None:
    from app.shared_assets.credential_service import CreateCredential, CredentialService
    from app.shared_assets.mcp_service import (
        CreateMcpServer,
        McpCredentialSlot,
        McpDefinition,
        McpService,
    )
    from app.shared_assets.resolver import MaterializedMcpSecrets, ProjectAssetResolver

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="materialize")
    outsider = await _seed_project(engine, factory, label="materialize-other")
    skill_id, skill_versions = await _seed_skill(
        engine,
        owner_id=admin.user_id,
        scope="project",
        project_id=admin.project_id,
        versions=2,
    )
    keyring = CredentialKeyring(active_key_id="test-key", _keys={"test-key": b"m" * 32})
    credentials = CredentialService(factory, keyring=keyring)
    mcp = McpService(factory)
    resolver = ProjectAssetResolver(factory, keyring=keyring)
    try:
        skill = await resolver.resolve_project_asset_snapshot(
            admin,
            AssetSelection(AssetKind.SKILL, skill_id),
        )
        assert skill.version_id == skill_versions[1]
        assert skill.files[0].path == "SKILL.md"
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.resolve_project_asset_snapshot(
                admin,
                AssetSelection(AssetKind.SKILL, skill_id, skill_versions[0]),
            )
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.resolve_project_asset_snapshot(
                outsider,
                AssetSelection(AssetKind.SKILL, skill_id),
            )

        credential = await credentials.create(
            admin,
            CreateCredential("erp", "ERP", "token"),
            {"env": {"ERP_TOKEN": "short-lived-secret"}},
        )
        asset = await mcp.create_asset(admin, CreateMcpServer("erp", "ERP"))
        draft = await mcp.create_version(
            admin,
            asset.id,
            McpDefinition(
                description="ERP tools",
                transport="http",
                url="https://mcp.example.test",
                credential_slots=(
                    McpCredentialSlot(
                        "primary",
                        "ERP authentication",
                        {"env": ["ERP_TOKEN"]},
                    ),
                ),
            ),
            expected_asset_version=1,
        )
        await mcp.submit_approval(admin, asset.id, draft.id, expected_asset_version=2)
        approved = await mcp.approve(
            admin,
            asset.id,
            draft.id,
            {"primary": credential.current_version_id},
            expected_asset_version=3,
        )

        snapshot = await resolver.resolve_project_asset_snapshot(
            admin,
            AssetSelection(AssetKind.MCP, asset.id),
        )
        assert isinstance(snapshot, ResolvedMcpSnapshot)
        assert snapshot.version_id == approved.id
        assert snapshot.credential_grant_ids == (approved.credential_grants[0].id,)
        assert "short-lived-secret" not in repr(snapshot)

        materialized = await resolver.materialize_mcp_secrets(snapshot)
        assert materialized.by_slot["primary"]["env"]["ERP_TOKEN"] == "short-lived-secret"
        assert "short-lived-secret" not in repr(materialized)

        await credentials.replace(
            admin,
            credential.id,
            {"env": {"ERP_TOKEN": "replacement-secret"}},
            expected_credential_version=1,
        )
        retired_materialized = await resolver.materialize_mcp_secrets(snapshot)
        assert retired_materialized.by_slot["primary"]["env"]["ERP_TOKEN"] == "short-lived-secret"

        outcomes = await asyncio.gather(
            resolver.materialize_mcp_secrets(snapshot),
            credentials.revoke(admin, credential.id, expected_credential_version=2),
            return_exceptions=True,
        )
        assert isinstance(outcomes[0], (MaterializedMcpSecrets, AssetResolutionUnavailable))
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.materialize_mcp_secrets(snapshot)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_system_mcp_binding_materializes_system_grant_and_rechecks_revoke(
    migrated_postgres_database_url: str,
) -> None:
    from app.shared_assets.binding_service import BindingService
    from app.shared_assets.credential_service import CreateCredential, CredentialService
    from app.shared_assets.mcp_service import (
        CreateMcpServer,
        McpCredentialSlot,
        McpDefinition,
        McpService,
    )
    from app.shared_assets.resolver import ProjectAssetResolver

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="system-mcp")
    system = await _seed_system_admin(engine)
    keyring = CredentialKeyring(active_key_id="system-key", _keys={"system-key": b"s" * 32})
    credentials = CredentialService(factory, keyring=keyring)
    mcp = McpService(factory)
    bindings = BindingService(factory)
    resolver = ProjectAssetResolver(factory, keyring=keyring)
    try:
        credential = await credentials.create(
            system,
            CreateCredential("system-erp", "System ERP", "token"),
            {"headers": {"X_ERP_TOKEN": "system-short-lived"}},
        )
        asset = await mcp.create_asset(system, CreateMcpServer("system-erp", "System ERP"))
        draft = await mcp.create_version(
            system,
            asset.id,
            McpDefinition(
                description="System ERP tools",
                transport="http",
                url="https://system-mcp.example.test",
                credential_slots=(
                    McpCredentialSlot(
                        "primary",
                        "System ERP authentication",
                        {"headers": ["X_ERP_TOKEN"]},
                    ),
                ),
            ),
            expected_asset_version=1,
        )
        approved = await mcp.approve(
            system,
            asset.id,
            draft.id,
            {"primary": credential.current_version_id},
            expected_asset_version=2,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE credential_envelopes SET is_active=false
                    WHERE credential_version_id=:version"""
                ),
                {"version": credential.current_version_id},
            )
        with pytest.raises(AssetValidationFailed):
            await bindings.enable(
                admin,
                AssetSelection(AssetKind.MCP, asset.id, approved.id),
            )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE credential_envelopes SET is_active=true
                    WHERE credential_version_id=:version"""
                ),
                {"version": credential.current_version_id},
            )
        await bindings.enable(
            admin,
            AssetSelection(AssetKind.MCP, asset.id, approved.id),
        )
        snapshot = await resolver.resolve_project_asset_snapshot(
            admin,
            AssetSelection(AssetKind.MCP, asset.id),
        )
        materialized = await resolver.materialize_mcp_secrets(snapshot)
        assert materialized.by_slot["primary"]["headers"]["X_ERP_TOKEN"] == "system-short-lived"

        await credentials.revoke(
            system,
            credential.id,
            expected_credential_version=1,
        )
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.resolve_project_asset_snapshot(
                admin,
                AssetSelection(AssetKind.MCP, asset.id),
            )
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.materialize_mcp_secrets(snapshot)
    finally:
        await engine.dispose()
