from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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


async def _seed_project_member(
    engine: AsyncEngine,
    factory: async_sessionmaker,
    *,
    project_id: uuid.UUID,
    label: str,
    role: str,
) -> ProjectContext:
    user_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
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
        return await resolve_project_context(
            session,
            user_id,
            project_id,
            f"req-{label}",
        )


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
async def test_catalog_state_empty_read_and_concurrent_bumps_are_monotonic(
    migrated_postgres_database_url: str,
) -> None:
    from app.shared_assets.catalog_state_repository import CatalogStateRepository

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM asset_catalog_state"))

        async with factory() as session:
            async with session.begin():
                repository = CatalogStateRepository(session)
                assert await repository.read_generation() == 0
                assert await repository.read_generation(for_update=True) == 0

        async def bump() -> int:
            async with factory() as session:
                async with session.begin():
                    return await CatalogStateRepository(session).bump_generation()

        first_two = await asyncio.wait_for(
            asyncio.gather(bump(), bump()),
            timeout=10,
        )
        assert sorted(first_two) == [1, 2]
        assert await bump() == 3
        assert await _generation(engine) == 3
    finally:
        await engine.dispose()


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
async def test_agent_binding_dependency_lock_serializes_system_skill_suspend(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.shared_assets.binding_repository as binding_repository_module
    from app.shared_assets.binding_service import BindingService
    from app.shared_assets.resolver import ProjectAssetResolver
    from app.shared_assets.skill_service import SkillService

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="dependency-suspend-race")
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
    bindings = BindingService(factory)
    skills = SkillService(factory)
    resolver = ProjectAssetResolver(factory)
    try:
        await bindings.enable(
            admin,
            AssetSelection(AssetKind.SKILL, skill_id, skill_versions[0]),
        )
        dependency_checked = asyncio.Event()
        release_binding = asyncio.Event()
        original_check = binding_repository_module.BindingRepository._system_versions_are_bound

        async def pause_after_dependency_check(
            repository,
            context,
            kind,
            version_ids,
        ):
            result = await original_check(
                repository,
                context,
                kind,
                version_ids,
            )
            if kind is AssetKind.SKILL:
                dependency_checked.set()
                await release_binding.wait()
            return result

        monkeypatch.setattr(
            binding_repository_module.BindingRepository,
            "_system_versions_are_bound",
            pause_after_dependency_check,
        )
        binding_task = asyncio.create_task(
            bindings.enable(
                admin,
                AssetSelection(
                    AssetKind.AGENT,
                    agent_id,
                    agent_versions[0],
                ),
            )
        )
        await asyncio.wait_for(dependency_checked.wait(), timeout=5)
        suspend_task = asyncio.create_task(
            skills.suspend(
                system,
                skill_id,
                expected_asset_version=1,
            )
        )
        suspended_while_binding_open = False
        try:
            await asyncio.wait_for(asyncio.shield(suspend_task), timeout=0.2)
        except TimeoutError:
            pass
        else:
            suspended_while_binding_open = True
        finally:
            release_binding.set()
        binding, suspended = await asyncio.wait_for(
            asyncio.gather(binding_task, suspend_task),
            timeout=10,
        )
        assert not suspended_while_binding_open
        assert binding.enabled is True
        assert suspended.status == "suspended"
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.resolve_project_asset_snapshot(
                admin,
                AssetSelection(AssetKind.AGENT, agent_id),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolver_reads_catalog_generation_after_snapshot_validation(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.shared_assets.resolver as resolver_module
    from app.shared_assets.mcp_service import CreateMcpServer, McpDefinition, McpService
    from app.shared_assets.resolver import ProjectAssetResolver

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="generation-last")
    mcp = McpService(factory)
    resolver = ProjectAssetResolver(factory)
    try:
        asset = await mcp.create_asset(
            admin,
            CreateMcpServer("generation-last", "Generation Last"),
        )
        draft = await mcp.create_version(
            admin,
            asset.id,
            McpDefinition(
                description="Generation ordering",
                transport="http",
                url="https://generation.example.test",
            ),
            expected_asset_version=1,
        )
        published = await mcp.publish(
            admin,
            asset.id,
            draft.id,
            expected_asset_version=2,
        )
        original_usable = resolver_module.ProjectAssetResolver._usable_grant_ids

        async def bump_after_closure(self, session, record, request_id):
            grant_ids = await original_usable(self, session, record, request_id)
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO asset_catalog_state (id,generation)
                        VALUES (1,1)
                        ON CONFLICT (id) DO UPDATE
                        SET generation=asset_catalog_state.generation+1"""
                    )
                )
            return grant_ids

        monkeypatch.setattr(
            resolver_module.ProjectAssetResolver,
            "_usable_grant_ids",
            bump_after_closure,
        )
        snapshot = await resolver.resolve_project_asset_snapshot(
            admin,
            AssetSelection(AssetKind.MCP, asset.id),
        )
        assert snapshot.version_id == published.id
        assert snapshot.catalog_generation == await _generation(engine)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_current_pointer_and_mcp_secrets_recheck_revocation(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.shared_assets.resolver as resolver_module
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
    runner = await _seed_project_member(
        engine,
        factory,
        project_id=admin.project_id,
        label="materialize-runner",
        role="runner",
    )
    viewer = await _seed_project_member(
        engine,
        factory,
        project_id=admin.project_id,
        label="materialize-viewer",
        role="viewer",
    )
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
        secondary_credential = await credentials.create(
            admin,
            CreateCredential("erp-aux", "ERP Aux", "token"),
            {"headers": {"X_AUX_TOKEN": "secondary-secret"}},
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
                    McpCredentialSlot(
                        "secondary",
                        "ERP auxiliary authentication",
                        {"headers": ["X_AUX_TOKEN"]},
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
            {
                "primary": credential.current_version_id,
                "secondary": secondary_credential.current_version_id,
            },
            expected_asset_version=3,
        )

        snapshot = await resolver.resolve_project_asset_snapshot(
            admin,
            AssetSelection(AssetKind.MCP, asset.id),
        )
        assert isinstance(snapshot, ResolvedMcpSnapshot)
        assert snapshot.version_id == approved.id
        assert snapshot.credential_grant_ids == tuple(grant.id for grant in approved.credential_grants)
        assert "short-lived-secret" not in repr(snapshot)
        viewer_snapshot = await resolver.resolve_project_asset_snapshot(
            viewer,
            AssetSelection(AssetKind.MCP, asset.id),
        )
        assert viewer_snapshot.version_id == snapshot.version_id

        materialized = await resolver.materialize_mcp_secrets(admin, snapshot)
        assert materialized.by_slot["primary"]["env"]["ERP_TOKEN"] == "short-lived-secret"
        assert materialized.by_slot["secondary"]["headers"]["X_AUX_TOKEN"] == "secondary-secret"
        assert "short-lived-secret" not in repr(materialized)
        runner_materialized = await resolver.materialize_mcp_secrets(runner, snapshot)
        assert runner_materialized.by_slot["primary"]["env"]["ERP_TOKEN"] == "short-lived-secret"

        decrypt_calls = 0
        original_decrypt = resolver_module.decrypt_credential_payload

        def track_decrypt(*args, **kwargs):
            nonlocal decrypt_calls
            decrypt_calls += 1
            return original_decrypt(*args, **kwargs)

        monkeypatch.setattr(
            resolver_module,
            "decrypt_credential_payload",
            track_decrypt,
        )
        forged = replace(
            snapshot,
            definition={
                "description": "forged",
                "transport": "stdio",
                "command": "unsafe",
                "credential_slots": (),
            },
        )
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.materialize_mcp_secrets(admin, forged)
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.materialize_mcp_secrets(outsider, snapshot)

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE project_memberships SET version=version+1 WHERE id=:membership"),
                {"membership": admin.membership_id},
            )
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.materialize_mcp_secrets(admin, snapshot)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE project_memberships SET version=:version,status='removed' WHERE id=:membership"),
                {
                    "membership": admin.membership_id,
                    "version": admin.membership_version,
                },
            )
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.materialize_mcp_secrets(admin, snapshot)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE project_memberships SET status='active' WHERE id=:membership"),
                {"membership": admin.membership_id},
            )
            await connection.execute(
                text("UPDATE projects SET is_suspended=true WHERE id=:project"),
                {"project": admin.project_id},
            )
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.materialize_mcp_secrets(admin, snapshot)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE projects SET is_suspended=false WHERE id=:project"),
                {"project": admin.project_id},
            )
        assert decrypt_calls == 0

        await credentials.replace(
            admin,
            credential.id,
            {"env": {"ERP_TOKEN": "replacement-secret"}},
            expected_credential_version=1,
        )
        retired_materialized = await resolver.materialize_mcp_secrets(admin, snapshot)
        assert retired_materialized.by_slot["primary"]["env"]["ERP_TOKEN"] == "short-lived-secret"

        outcomes = await asyncio.gather(
            resolver.materialize_mcp_secrets(admin, snapshot),
            credentials.revoke(admin, credential.id, expected_credential_version=2),
            return_exceptions=True,
        )
        assert isinstance(outcomes[0], (MaterializedMcpSecrets, AssetResolutionUnavailable))
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.materialize_mcp_secrets(admin, snapshot)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_system_mcp_binding_materializes_system_grant_and_rechecks_revoke(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.shared_assets.credential_repository as credential_repository_module
    import app.shared_assets.resolver as resolver_module
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
        definition = McpDefinition(
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
        )
        draft = await mcp.create_version(
            system,
            asset.id,
            definition,
            expected_asset_version=1,
        )
        approved = await mcp.approve(
            system,
            asset.id,
            draft.id,
            {"primary": credential.current_version_id},
            expected_asset_version=2,
        )
        second_draft = await mcp.create_version(
            system,
            asset.id,
            definition,
            expected_asset_version=3,
        )
        second_approved = await mcp.approve(
            system,
            asset.id,
            second_draft.id,
            {"primary": credential.current_version_id},
            expected_asset_version=4,
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
        materialized = await resolver.materialize_mcp_secrets(admin, snapshot)
        assert materialized.by_slot["primary"]["headers"]["X_ERP_TOKEN"] == "system-short-lived"

        decrypt_calls = 0
        original_decrypt = resolver_module.decrypt_credential_payload

        def track_decrypt(*args, **kwargs):
            nonlocal decrypt_calls
            decrypt_calls += 1
            return original_decrypt(*args, **kwargs)

        monkeypatch.setattr(
            resolver_module,
            "decrypt_credential_payload",
            track_decrypt,
        )
        await bindings.disable(
            admin,
            AssetSelection(AssetKind.MCP, asset.id),
            expected_binding_version=1,
        )
        before_failure = decrypt_calls
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.materialize_mcp_secrets(admin, snapshot)
        assert decrypt_calls == before_failure
        await bindings.enable(
            admin,
            AssetSelection(AssetKind.MCP, asset.id, approved.id),
            expected_binding_version=2,
        )
        await bindings.upgrade(
            admin,
            AssetSelection(AssetKind.MCP, asset.id, second_approved.id),
            expected_binding_version=3,
        )
        before_failure = decrypt_calls
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.materialize_mcp_secrets(admin, snapshot)
        assert decrypt_calls == before_failure

        current_snapshot = await resolver.resolve_project_asset_snapshot(
            admin,
            AssetSelection(AssetKind.MCP, asset.id),
        )
        assert current_snapshot.version_id == second_approved.id
        current_materialized = await resolver.materialize_mcp_secrets(
            admin,
            current_snapshot,
        )
        assert current_materialized.by_slot["primary"]["headers"]["X_ERP_TOKEN"] == "system-short-lived"

        closure_checked = asyncio.Event()
        release_resolver = asyncio.Event()
        revoke_attempted = asyncio.Event()
        original_usable = resolver_module.ProjectAssetResolver._usable_grant_ids

        async def pause_after_closure(self, session, record, request_id):
            grant_ids = await original_usable(self, session, record, request_id)
            closure_checked.set()
            await release_resolver.wait()
            return grant_ids

        original_get_system = credential_repository_module.CredentialRepository.get_system_credential

        async def signal_revoke_attempt(
            repository,
            context,
            credential_id,
            *,
            for_update=False,
        ):
            if for_update:
                revoke_attempted.set()
            return await original_get_system(
                repository,
                context,
                credential_id,
                for_update=for_update,
            )

        monkeypatch.setattr(
            resolver_module.ProjectAssetResolver,
            "_usable_grant_ids",
            pause_after_closure,
        )
        monkeypatch.setattr(
            credential_repository_module.CredentialRepository,
            "get_system_credential",
            signal_revoke_attempt,
        )
        resolve_task = asyncio.create_task(
            resolver.resolve_project_asset_snapshot(
                admin,
                AssetSelection(AssetKind.MCP, asset.id),
            )
        )
        await asyncio.wait_for(closure_checked.wait(), timeout=5)
        revoke_task = asyncio.create_task(
            credentials.revoke(
                system,
                credential.id,
                expected_credential_version=1,
            )
        )
        await asyncio.wait_for(revoke_attempted.wait(), timeout=5)
        revoked_while_snapshot_open = False
        try:
            await asyncio.wait_for(asyncio.shield(revoke_task), timeout=0.2)
        except TimeoutError:
            pass
        else:
            revoked_while_snapshot_open = True
        finally:
            release_resolver.set()
        resolved_after_barrier, _revoked = await asyncio.wait_for(
            asyncio.gather(resolve_task, revoke_task),
            timeout=10,
        )
        assert not revoked_while_snapshot_open
        assert resolved_after_barrier.version_id == second_approved.id

        with pytest.raises(AssetResolutionUnavailable):
            await resolver.resolve_project_asset_snapshot(
                admin,
                AssetSelection(AssetKind.MCP, asset.id),
            )
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.materialize_mcp_secrets(admin, current_snapshot)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_binding_enable_serializes_with_system_credential_revoke(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.shared_assets.binding_repository as binding_repository_module
    import app.shared_assets.credential_repository as credential_repository_module
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
    admin = await _seed_project(engine, factory, label="binding-revoke-race")
    system = await _seed_system_admin(engine)
    keyring = CredentialKeyring(
        active_key_id="binding-race-key",
        _keys={"binding-race-key": b"r" * 32},
    )
    credentials = CredentialService(factory, keyring=keyring)
    mcp = McpService(factory)
    bindings = BindingService(factory)
    resolver = ProjectAssetResolver(factory, keyring=keyring)
    try:
        credential = await credentials.create(
            system,
            CreateCredential("binding-race", "Binding Race", "token"),
            {"env": {"RACE_TOKEN": "race-secret"}},
        )
        asset = await mcp.create_asset(
            system,
            CreateMcpServer("binding-race", "Binding Race"),
        )
        draft = await mcp.create_version(
            system,
            asset.id,
            McpDefinition(
                description="Binding and revoke serialization",
                transport="http",
                url="https://binding-race.example.test",
                credential_slots=(
                    McpCredentialSlot(
                        "primary",
                        "Race credential",
                        {"env": ["RACE_TOKEN"]},
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

        closure_checked = asyncio.Event()
        release_binding = asyncio.Event()
        revoke_attempted = asyncio.Event()
        original_validate = binding_repository_module.BindingRepository._validate_mcp_versions

        async def pause_after_closure(repository, version_ids, request_id):
            await original_validate(repository, version_ids, request_id)
            closure_checked.set()
            await release_binding.wait()

        original_get_system = credential_repository_module.CredentialRepository.get_system_credential

        async def signal_revoke_attempt(
            repository,
            context,
            credential_id,
            *,
            for_update=False,
        ):
            if for_update:
                revoke_attempted.set()
            return await original_get_system(
                repository,
                context,
                credential_id,
                for_update=for_update,
            )

        monkeypatch.setattr(
            binding_repository_module.BindingRepository,
            "_validate_mcp_versions",
            pause_after_closure,
        )
        monkeypatch.setattr(
            credential_repository_module.CredentialRepository,
            "get_system_credential",
            signal_revoke_attempt,
        )
        binding_task = asyncio.create_task(
            bindings.enable(
                admin,
                AssetSelection(AssetKind.MCP, asset.id, approved.id),
            )
        )
        await asyncio.wait_for(closure_checked.wait(), timeout=5)
        revoke_task = asyncio.create_task(
            credentials.revoke(
                system,
                credential.id,
                expected_credential_version=1,
            )
        )
        await asyncio.wait_for(revoke_attempted.wait(), timeout=5)
        revoked_while_binding_open = False
        try:
            await asyncio.wait_for(asyncio.shield(revoke_task), timeout=0.2)
        except TimeoutError:
            pass
        else:
            revoked_while_binding_open = True
        finally:
            release_binding.set()
        binding, _revoked = await asyncio.wait_for(
            asyncio.gather(binding_task, revoke_task),
            timeout=10,
        )
        assert not revoked_while_binding_open
        assert binding.enabled is True
        assert binding.version_id == approved.id
        with pytest.raises(AssetResolutionUnavailable):
            await resolver.resolve_project_asset_snapshot(
                admin,
                AssetSelection(AssetKind.MCP, asset.id),
            )

        async with factory() as session:
            binding_row = await session.get(
                binding_repository_module.ProjectSystemMcpBindingRow,
                (admin.project_id, asset.id),
            )
            credential_row = await session.get(
                credential_repository_module.CredentialRow,
                credential.id,
            )
        assert binding_row is not None and binding_row.enabled is True
        assert credential_row is not None and credential_row.status == "revoked"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_two_mcp_closure_uses_global_credential_lock_order_with_bulk_approval(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
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
    from deerflow.persistence.shared_assets import (
        CredentialGrantRow,
        CredentialRow,
        McpServerRow,
        McpServerVersionRow,
    )

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="agent-two-mcp-lock-order")
    system = await _seed_system_admin(engine)
    keyring = CredentialKeyring(
        active_key_id="agent-two-mcp-key",
        _keys={"agent-two-mcp-key": b"g" * 32},
    )
    credentials = CredentialService(factory, keyring=keyring)
    mcp = McpService(factory)
    bindings = BindingService(factory)
    resolver = ProjectAssetResolver(factory, keyring=keyring)
    approval_task: asyncio.Task | None = None
    resolver_task: asyncio.Task | None = None
    release_approval = asyncio.Event()
    try:
        first_credential = await credentials.create(
            system,
            CreateCredential("agent-lock-a", "Agent Lock A", "token"),
            {"env": {"SHARED_TOKEN": "agent-lock-secret-a"}},
        )
        second_credential = await credentials.create(
            system,
            CreateCredential("agent-lock-b", "Agent Lock B", "token"),
            {"env": {"SHARED_TOKEN": "agent-lock-secret-b"}},
        )
        credential_low, credential_high = sorted(
            (first_credential, second_credential),
            key=lambda item: item.id.int,
        )

        async def publish_single_credential_mcp(
            slug: str,
            credential_version_id: uuid.UUID,
        ):
            asset = await mcp.create_asset(
                system,
                CreateMcpServer(slug, slug.replace("-", " ").title()),
            )
            draft = await mcp.create_version(
                system,
                asset.id,
                McpDefinition(
                    description=f"Single credential {slug}",
                    transport="http",
                    url=f"https://{slug}.example.test",
                    credential_slots=(
                        McpCredentialSlot(
                            "primary",
                            "Shared credential",
                            {"env": ["SHARED_TOKEN"]},
                        ),
                    ),
                ),
                expected_asset_version=1,
            )
            published = await mcp.approve(
                system,
                asset.id,
                draft.id,
                {"primary": credential_version_id},
                expected_asset_version=2,
            )
            return asset, published

        high_asset, high_mcp = await publish_single_credential_mcp(
            "agent-lock-high",
            credential_high.current_version_id,
        )
        low_asset, low_mcp = await publish_single_credential_mcp(
            "agent-lock-low",
            credential_low.current_version_id,
        )
        await bindings.enable(
            admin,
            AssetSelection(AssetKind.MCP, high_asset.id, high_mcp.id),
        )
        await bindings.enable(
            admin,
            AssetSelection(AssetKind.MCP, low_asset.id, low_mcp.id),
        )
        agent_id, agent_versions = await _seed_agent(
            engine,
            owner_id=admin.user_id,
            scope="project",
            project_id=admin.project_id,
            versions=1,
            mcp_version_ids=(high_mcp.id, low_mcp.id),
        )

        approval_asset = await mcp.create_asset(
            system,
            CreateMcpServer("agent-lock-bulk", "Agent Lock Bulk"),
        )
        approval_version = await mcp.create_version(
            system,
            approval_asset.id,
            McpDefinition(
                description="Bulk approval lock-order competitor",
                transport="http",
                url="https://agent-lock-bulk.example.test",
                credential_slots=(
                    McpCredentialSlot(
                        "first",
                        "First shared credential",
                        {"env": ["SHARED_TOKEN"]},
                    ),
                    McpCredentialSlot(
                        "second",
                        "Second shared credential",
                        {"env": ["SHARED_TOKEN"]},
                    ),
                ),
            ),
            expected_asset_version=1,
        )

        approval_holds_low = asyncio.Event()
        approval_attempts_high = asyncio.Event()
        resolver_holds_high = asyncio.Event()
        original_execute = AsyncSession.execute

        def locks_credential(statement: object, credential_id: uuid.UUID) -> bool:
            descriptions = getattr(statement, "column_descriptions", ())
            if len(descriptions) != 1 or descriptions[0].get("entity") is not CredentialRow or getattr(statement, "_for_update_arg", None) is None:
                return False
            return credential_id in {value for value in statement.compile().params.values() if isinstance(value, uuid.UUID)}

        async def instrument_credential_locks(
            session: AsyncSession,
            statement,
            *args,
            **kwargs,
        ):
            current = asyncio.current_task()
            if current is approval_task and locks_credential(
                statement,
                credential_high.id,
            ):
                approval_attempts_high.set()
            result = await original_execute(session, statement, *args, **kwargs)
            if current is approval_task and locks_credential(
                statement,
                credential_low.id,
            ):
                approval_holds_low.set()
                await release_approval.wait()
            if current is resolver_task and locks_credential(
                statement,
                credential_high.id,
            ):
                resolver_holds_high.set()
                await approval_attempts_high.wait()
            return result

        monkeypatch.setattr(AsyncSession, "execute", instrument_credential_locks)
        approval_task = asyncio.create_task(
            mcp.approve(
                system,
                approval_asset.id,
                approval_version.id,
                {
                    "first": credential_low.current_version_id,
                    "second": credential_high.current_version_id,
                },
                expected_asset_version=2,
            )
        )
        await asyncio.wait_for(approval_holds_low.wait(), timeout=5)
        resolver_task = asyncio.create_task(
            resolver.resolve_project_asset_snapshot(
                admin,
                AssetSelection(AssetKind.AGENT, agent_id),
            )
        )
        try:
            await asyncio.wait_for(resolver_holds_high.wait(), timeout=0.75)
        except TimeoutError:
            pass
        finally:
            release_approval.set()

        approval_result, agent_snapshot = await asyncio.wait_for(
            asyncio.gather(approval_task, resolver_task),
            timeout=10,
        )
        assert approval_result.workflow_status.value == "published"
        assert agent_snapshot.version_id == agent_versions[0]
        assert agent_snapshot.dependency_version_ids == (high_mcp.id, low_mcp.id)

        async with factory() as session:
            stored_asset = await session.get(McpServerRow, approval_asset.id)
            stored_version = await session.get(
                McpServerVersionRow,
                approval_version.id,
            )
            stored_grants = tuple((await session.execute(select(CredentialGrantRow).where(CredentialGrantRow.mcp_server_version_id == approval_version.id))).scalars().all())
        assert stored_asset is not None
        assert stored_asset.current_published_version_id == approval_version.id
        assert stored_version is not None
        assert stored_version.workflow_status == "published"
        assert len(stored_grants) == 2
        assert all(grant.status == "active" for grant in stored_grants)
    finally:
        release_approval.set()
        pending = [task for task in (approval_task, resolver_task) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_materializer_blocks_grant_repin_after_reference_read(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.shared_assets.resolver as resolver_module
    from app.shared_assets.credential_service import CreateCredential, CredentialService
    from app.shared_assets.mcp_service import (
        CreateMcpServer,
        McpCredentialSlot,
        McpDefinition,
        McpService,
    )
    from app.shared_assets.resolver import ProjectAssetResolver
    from deerflow.persistence.shared_assets import (
        CredentialGrantRow,
        CredentialVersionRow,
    )

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="materialize-grant-repin")
    marker = "grant-repin-plaintext-must-stay-hidden"
    keyring = CredentialKeyring(
        active_key_id="grant-repin-key",
        _keys={"grant-repin-key": b"p" * 32},
    )
    credentials = CredentialService(factory, keyring=keyring)
    mcp = McpService(factory)
    resolver = ProjectAssetResolver(factory, keyring=keyring)
    materialize_task: asyncio.Task | None = None
    repin_task: asyncio.Task | None = None
    release_materializer = asyncio.Event()
    try:
        original_credential = await credentials.create(
            admin,
            CreateCredential("repin-original", "Repin Original", "token"),
            {"env": {"REP_TOKEN": marker}},
        )
        replacement_credential = await credentials.create(
            admin,
            CreateCredential("repin-replacement", "Repin Replacement", "token"),
            {"env": {"REP_TOKEN": "replacement-value"}},
        )
        asset = await mcp.create_asset(
            admin,
            CreateMcpServer("repin-mcp", "Repin MCP"),
        )
        draft = await mcp.create_version(
            admin,
            asset.id,
            McpDefinition(
                description="Grant re-pin materializer barrier",
                transport="http",
                url="https://repin.example.test",
                credential_slots=(
                    McpCredentialSlot(
                        "primary",
                        "Original slot",
                        {"env": ["REP_TOKEN"]},
                    ),
                    McpCredentialSlot(
                        "alternate",
                        "Alternate slot",
                        {"env": ["REP_TOKEN"]},
                        required=False,
                    ),
                ),
            ),
            expected_asset_version=1,
        )
        await mcp.submit_approval(
            admin,
            asset.id,
            draft.id,
            expected_asset_version=2,
        )
        approved = await mcp.approve(
            admin,
            asset.id,
            draft.id,
            {"primary": original_credential.current_version_id},
            expected_asset_version=3,
        )
        snapshot = await resolver.resolve_project_asset_snapshot(
            admin,
            AssetSelection(AssetKind.MCP, asset.id),
        )
        grant = approved.credential_grants[0]
        slots_by_name = {slot.name: slot for slot in approved.credential_slots}

        references_read = asyncio.Event()
        repin_attempted = asyncio.Event()
        repin_committed = asyncio.Event()
        original_execute = AsyncSession.execute
        paused = False

        def is_grant_reference_query(statement: object) -> bool:
            descriptions = getattr(statement, "column_descriptions", ())
            entities = tuple(item.get("entity") for item in descriptions)
            return (
                entities
                == (
                    CredentialGrantRow,
                    CredentialGrantRow,
                    CredentialGrantRow,
                    CredentialVersionRow,
                )
                and getattr(statement, "_for_update_arg", None) is None
                and approved.id in {value for value in statement.compile().params.values() if isinstance(value, uuid.UUID)}
            )

        async def pause_after_reference_read(
            session: AsyncSession,
            statement,
            *args,
            **kwargs,
        ):
            nonlocal paused
            result = await original_execute(session, statement, *args, **kwargs)
            if not paused and asyncio.current_task() is materialize_task and is_grant_reference_query(statement):
                paused = True
                references_read.set()
                await release_materializer.wait()
            return result

        decrypt_calls = 0
        original_decrypt = resolver_module.decrypt_credential_payload

        def track_decrypt(*args, **kwargs):
            nonlocal decrypt_calls
            decrypt_calls += 1
            return original_decrypt(*args, **kwargs)

        async def repin_grant() -> None:
            repin_attempted.set()
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """SELECT id FROM mcp_version_credential_slots
                        WHERE id IN (:old_slot, :new_slot)
                        ORDER BY id
                        FOR KEY SHARE"""
                    ),
                    {
                        "old_slot": slots_by_name["primary"].id,
                        "new_slot": slots_by_name["alternate"].id,
                    },
                )
                await connection.execute(
                    text(
                        """UPDATE credential_grants
                        SET credential_slot_id=:slot, credential_version_id=:version
                        WHERE id=:grant"""
                    ),
                    {
                        "slot": slots_by_name["alternate"].id,
                        "version": replacement_credential.current_version_id,
                        "grant": grant.id,
                    },
                )
            repin_committed.set()

        monkeypatch.setattr(AsyncSession, "execute", pause_after_reference_read)
        monkeypatch.setattr(
            resolver_module,
            "decrypt_credential_payload",
            track_decrypt,
        )
        materialize_task = asyncio.create_task(resolver.materialize_mcp_secrets(admin, snapshot))
        await asyncio.wait_for(references_read.wait(), timeout=5)
        repin_task = asyncio.create_task(repin_grant())
        await asyncio.wait_for(repin_attempted.wait(), timeout=5)

        committed_while_materializer_open = False
        try:
            await asyncio.wait_for(asyncio.shield(repin_task), timeout=0.2)
        except TimeoutError:
            pass
        else:
            committed_while_materializer_open = True
        assert decrypt_calls == 0
        release_materializer.set()

        materialized, _repinned = await asyncio.wait_for(
            asyncio.gather(materialize_task, repin_task),
            timeout=10,
        )
        assert materialized.by_slot["primary"]["env"]["REP_TOKEN"] == marker
        assert "alternate" not in materialized.by_slot
        assert decrypt_calls == 1
        assert not committed_while_materializer_open
        assert repin_committed.is_set()

        async with factory() as session:
            stored_grant = await session.get(CredentialGrantRow, grant.id)
        assert stored_grant is not None
        assert stored_grant.credential_slot_id == slots_by_name["alternate"].id
        assert stored_grant.credential_version_id == replacement_credential.current_version_id
    finally:
        release_materializer.set()
        pending = [task for task in (materialize_task, repin_task) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_materializer_blocks_new_optional_grant_after_reference_read(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.shared_assets.resolver as resolver_module
    from app.shared_assets.credential_service import CreateCredential, CredentialService
    from app.shared_assets.mcp_service import (
        CreateMcpServer,
        McpCredentialSlot,
        McpDefinition,
        McpService,
    )
    from app.shared_assets.resolver import ProjectAssetResolver
    from deerflow.persistence.shared_assets import (
        CredentialGrantRow,
        CredentialVersionRow,
    )

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="optional-grant-barrier")
    keyring = CredentialKeyring(
        active_key_id="optional-grant-key",
        _keys={"optional-grant-key": b"o" * 32},
    )
    credentials = CredentialService(factory, keyring=keyring)
    mcp = McpService(factory)
    resolver = ProjectAssetResolver(factory, keyring=keyring)
    materialize_task: asyncio.Task | None = None
    insert_task: asyncio.Task | None = None
    release_materializer = asyncio.Event()
    try:
        required_credential = await credentials.create(
            admin,
            CreateCredential("optional-required", "Optional Required", "token"),
            {"env": {"OPTIONAL_TOKEN": "required-secret"}},
        )
        optional_credential = await credentials.create(
            admin,
            CreateCredential("optional-late", "Optional Late", "token"),
            {"env": {"OPTIONAL_TOKEN": "late-secret"}},
        )
        asset = await mcp.create_asset(
            admin,
            CreateMcpServer("optional-grant-mcp", "Optional Grant MCP"),
        )
        draft = await mcp.create_version(
            admin,
            asset.id,
            McpDefinition(
                description="Optional grant insertion barrier",
                transport="http",
                url="https://optional-grant.example.test",
                credential_slots=(
                    McpCredentialSlot(
                        "required",
                        "Required credential",
                        {"env": ["OPTIONAL_TOKEN"]},
                    ),
                    McpCredentialSlot(
                        "optional",
                        "Late optional credential",
                        {"env": ["OPTIONAL_TOKEN"]},
                        required=False,
                    ),
                ),
            ),
            expected_asset_version=1,
        )
        await mcp.submit_approval(
            admin,
            asset.id,
            draft.id,
            expected_asset_version=2,
        )
        approved = await mcp.approve(
            admin,
            asset.id,
            draft.id,
            {"required": required_credential.current_version_id},
            expected_asset_version=3,
        )
        snapshot = await resolver.resolve_project_asset_snapshot(
            admin,
            AssetSelection(AssetKind.MCP, asset.id),
        )
        slots_by_name = {slot.name: slot for slot in approved.credential_slots}
        late_grant_id = uuid.uuid4()

        references_read = asyncio.Event()
        insert_attempted = asyncio.Event()
        insert_committed = asyncio.Event()
        original_execute = AsyncSession.execute
        paused = False

        def is_grant_reference_query(statement: object) -> bool:
            descriptions = getattr(statement, "column_descriptions", ())
            entities = tuple(item.get("entity") for item in descriptions)
            return (
                entities
                == (
                    CredentialGrantRow,
                    CredentialGrantRow,
                    CredentialGrantRow,
                    CredentialVersionRow,
                )
                and getattr(statement, "_for_update_arg", None) is None
                and approved.id in {value for value in statement.compile().params.values() if isinstance(value, uuid.UUID)}
            )

        async def pause_after_reference_read(
            session: AsyncSession,
            statement,
            *args,
            **kwargs,
        ):
            nonlocal paused
            result = await original_execute(session, statement, *args, **kwargs)
            if not paused and asyncio.current_task() is materialize_task and is_grant_reference_query(statement):
                paused = True
                references_read.set()
                await release_materializer.wait()
            return result

        decrypt_calls = 0
        original_decrypt = resolver_module.decrypt_credential_payload

        def track_decrypt(*args, **kwargs):
            nonlocal decrypt_calls
            decrypt_calls += 1
            return original_decrypt(*args, **kwargs)

        async def insert_optional_grant() -> None:
            insert_attempted.set()
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO credential_grants
                        (id,mcp_server_version_id,credential_slot_id,
                         credential_version_id,created_by_user_id)
                        VALUES (:id,:mcp_version,:slot,:credential_version,:user)"""
                    ),
                    {
                        "id": late_grant_id,
                        "mcp_version": approved.id,
                        "slot": slots_by_name["optional"].id,
                        "credential_version": optional_credential.current_version_id,
                        "user": str(admin.user_id),
                    },
                )
            insert_committed.set()

        monkeypatch.setattr(AsyncSession, "execute", pause_after_reference_read)
        monkeypatch.setattr(
            resolver_module,
            "decrypt_credential_payload",
            track_decrypt,
        )
        materialize_task = asyncio.create_task(resolver.materialize_mcp_secrets(admin, snapshot))
        await asyncio.wait_for(references_read.wait(), timeout=5)
        insert_task = asyncio.create_task(insert_optional_grant())
        await asyncio.wait_for(insert_attempted.wait(), timeout=5)

        committed_while_materializer_open = False
        try:
            await asyncio.wait_for(asyncio.shield(insert_task), timeout=0.2)
        except TimeoutError:
            pass
        else:
            committed_while_materializer_open = True
        assert decrypt_calls == 0
        release_materializer.set()

        materialized, _inserted = await asyncio.wait_for(
            asyncio.gather(materialize_task, insert_task),
            timeout=10,
        )
        assert materialized.by_slot["required"]["env"]["OPTIONAL_TOKEN"] == "required-secret"
        assert "optional" not in materialized.by_slot
        assert decrypt_calls == 1
        assert not committed_while_materializer_open
        assert insert_committed.is_set()

        async with factory() as session:
            stored_grants = tuple(
                (
                    await session.execute(
                        select(CredentialGrantRow)
                        .where(
                            CredentialGrantRow.mcp_server_version_id == approved.id,
                            CredentialGrantRow.status == "active",
                        )
                        .order_by(CredentialGrantRow.credential_slot_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(stored_grants) == 2
        assert {grant.id for grant in stored_grants} == {
            *snapshot.credential_grant_ids,
            late_grant_id,
        }
        assert await _generation(engine) > snapshot.catalog_generation
    finally:
        release_materializer.set()
        pending = [task for task in (materialize_task, insert_task) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await engine.dispose()
