from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.shared_assets.credential_closure import McpCredentialClosureTarget, lock_mcp_credential_closures
from app.shared_assets.keyring import CredentialKeyring
from app.shared_assets.models import AssetScope
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
    AssetCatalogStateRow,
    CredentialEnvelopeRow,
    CredentialGrantRow,
    McpCredentialSlotRow,
    McpServerRow,
    McpServerVersionRow,
    SkillRow,
    SkillVersionRow,
)
from scripts.migrate_assets import (
    AssetMigrationError,
    AssetMigrationRunner,
    InventoryItem,
    MigrationValidationProbes,
    OwnerMap,
    SourceLayout,
    build_inventory,
    render_inventory,
)


@pytest.mark.asyncio
async def test_migration_is_idempotent_and_checksum_change_creates_version(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    actor_id = str(uuid.uuid4())
    source = tmp_path / "SKILL.md"
    source.write_text("---\nname: demo\ndescription: demo\n---\nfirst\n", encoding="utf-8")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'system_admin',:now,false,0)"""
            ),
            {"id": actor_id, "email": f"migration-{actor_id}@example.com", "now": datetime.now(UTC)},
        )
    runner = AssetMigrationRunner(factory, backup_root=tmp_path / "migrations")

    first_item = InventoryItem.for_skill(
        source_key="system-skill:demo",
        slug="demo",
        display_name="Demo",
        scope="system",
        project_id=None,
        owner_user_id=actor_id,
        files=(source,),
    )
    first = await runner.run((first_item,), execute=True)
    second = await runner.run((first_item,), execute=True)

    assert first.created_versions == 1
    assert second.created_versions == 0
    assert second.noop_versions == 1
    source.write_text("---\nname: demo\ndescription: demo\n---\nsecond\n", encoding="utf-8")
    changed_item = InventoryItem.for_skill(
        source_key=first_item.source_key,
        slug=first_item.slug,
        display_name=first_item.display_name,
        scope=first_item.scope,
        project_id=None,
        owner_user_id=actor_id,
        files=(source,),
    )
    changed = await runner.run((changed_item,), execute=True)
    assert changed.created_versions == 1

    async with factory() as session:
        skill = (await session.execute(select(SkillRow).where(SkillRow.source_key == first_item.source_key))).scalar_one()
        versions = int((await session.execute(select(func.count()).select_from(SkillVersionRow).where(SkillVersionRow.skill_id == skill.id))).scalar_one())
        marker = await session.get(AssetCatalogStateRow, 1)
        assert versions == 2
        assert marker is not None and marker.cutover_at is not None
        assert skill.current_published_version_id is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_all_scope_mappings_preflight_before_any_asset_write(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    actor_id = str(uuid.uuid4())
    missing_project_owner = str(uuid.uuid4())
    system_source = tmp_path / "system/SKILL.md"
    project_source = tmp_path / "project/SKILL.md"
    system_source.parent.mkdir(parents=True)
    project_source.parent.mkdir(parents=True)
    system_source.write_text("---\nname: system\ndescription: system\n---\nbody\n", encoding="utf-8")
    project_source.write_text("---\nname: project\ndescription: project\n---\nbody\n", encoding="utf-8")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'system_admin',:now,false,0)"""
            ),
            {"id": actor_id, "email": f"preflight-{actor_id}@example.com", "now": datetime.now(UTC)},
        )
    inventory = (
        InventoryItem.for_skill(
            source_key="system-skill:preflight-system",
            slug="preflight-system",
            display_name="Preflight system",
            scope="system",
            project_id=None,
            owner_user_id=actor_id,
            files=(system_source,),
        ),
        InventoryItem.for_skill(
            source_key="project-skill:preflight-project",
            slug="preflight-project",
            display_name="Preflight project",
            scope="project",
            project_id=uuid.uuid4(),
            owner_user_id=missing_project_owner,
            files=(project_source,),
        ),
    )
    runner = AssetMigrationRunner(factory, backup_root=tmp_path / "migrations")

    with pytest.raises(AssetMigrationError, match="mapped default project"):
        await runner.run(inventory, execute=True, batch_size=1)

    async with factory() as session:
        counts = {model.__tablename__: int((await session.execute(select(func.count()).select_from(model))).scalar_one()) for model in (AgentRow, AgentVersionRow, SkillRow, SkillVersionRow, McpServerRow, McpServerVersionRow)}
    assert counts == {name: 0 for name in counts}
    await engine.dispose()


@pytest.mark.asyncio
async def test_all_visible_agent_dependencies_fail_ambiguity_before_any_asset_write(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    actor_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    repo = tmp_path / "repo"
    data_root = tmp_path / "data"
    system_skill = repo / "skills/public/shared/SKILL.md"
    project_skill = data_root / f"users/{actor_id}/skills/custom/shared/SKILL.md"
    project_agent = data_root / f"users/{actor_id}/agents/project-agent"
    system_skill.parent.mkdir(parents=True)
    project_skill.parent.mkdir(parents=True)
    project_agent.mkdir(parents=True)
    system_skill.write_text("---\nname: shared\ndescription: system\n---\nsystem\n", encoding="utf-8")
    project_skill.write_text("---\nname: shared\ndescription: project\n---\nproject\n", encoding="utf-8")
    (project_agent / "config.yaml").write_text("name: Project Agent\n", encoding="utf-8")
    (project_agent / "SOUL.md").write_text("Use all visible dependencies.", encoding="utf-8")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'system_admin',:now,false,0)"""
            ),
            {"id": actor_id, "email": f"all-visible-{actor_id}@example.com", "now": datetime.now(UTC)},
        )
        await connection.execute(
            text(
                """INSERT INTO projects (id,slug,display_name,created_by_user_id)
                VALUES (:id,:slug,'All Visible',:owner_id)"""
            ),
            {"id": project_id, "slug": f"all-visible-{project_id}", "owner_id": actor_id},
        )
        await connection.execute(
            text(
                """INSERT INTO project_memberships (id,project_id,user_id,role)
                VALUES (:id,:project_id,:user_id,'admin')"""
            ),
            {"id": uuid.uuid4(), "project_id": project_id, "user_id": actor_id},
        )
    inventory = build_inventory(
        SourceLayout(repo_root=repo, data_root=data_root),
        OwnerMap({actor_id: project_id}, system_actor=actor_id),
    )
    assert len(inventory) == 3
    agent = next(item for item in inventory if item.kind == "agent")
    assert agent.payload["skill_slugs"] is None
    runner = AssetMigrationRunner(factory, backup_root=tmp_path / "migrations")

    with pytest.raises(AssetMigrationError, match="agent dependency is missing or ambiguous"):
        await runner.run(inventory, execute=True, batch_size=1)

    async with factory() as session:
        counts = {model.__tablename__: int((await session.execute(select(func.count()).select_from(model))).scalar_one()) for model in (AgentRow, AgentVersionRow, SkillRow, SkillVersionRow, McpServerRow, McpServerVersionRow)}
    assert counts == {name: 0 for name in counts}
    assert not (tmp_path / "migrations").exists()
    await engine.dispose()


@pytest.mark.asyncio
async def test_system_agent_mcp_skill_and_secret_migrate_as_one_validated_catalog(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    actor_id = str(uuid.uuid4())
    repo = tmp_path / "repo"
    skill = repo / "skills/public/demo/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: demo\n---\nbody\n", encoding="utf-8")
    extensions = repo / "extensions_config.json"
    extensions.write_text(
        """{"mcpServers":{"demo-mcp":{"type":"http","url":"https://example.invalid/mcp","headers":{"Authorization":"plain-token"}}}}""",
        encoding="utf-8",
    )
    agent_dir = repo / "agents/research"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text(
        "name: Research\ndescription: demo\nmodel: default\nskills: [demo]\nmcp_servers: [demo-mcp]\n",
        encoding="utf-8",
    )
    (agent_dir / "SOUL.md").write_text("Research safely.", encoding="utf-8")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'system_admin',:now,false,0)"""
            ),
            {"id": actor_id, "email": f"catalog-{actor_id}@example.com", "now": datetime.now(UTC)},
        )
    inventory = build_inventory(
        SourceLayout(repo_root=repo, data_root=tmp_path / "data"),
        OwnerMap({}, system_actor=actor_id),
    )
    assert {item.kind for item in inventory} == {"agent", "skill", "mcp"}
    assert "plain-token" not in render_inventory(inventory)
    runner = AssetMigrationRunner(
        factory,
        backup_root=tmp_path / "migrations",
        keyring=CredentialKeyring(active_key_id="m3", _keys={"m3": b"k" * 32}),
    )

    first = await runner.run(inventory, execute=True)
    second = await runner.run(inventory, execute=True)

    assert first.created_versions == 4
    assert second.created_versions == 0
    assert second.noop_versions == 3
    async with factory() as session:
        async with session.begin():
            mcp = (await session.execute(select(McpServerRow).where(McpServerRow.source_key == "system-mcp:demo-mcp"))).scalar_one()
            assert mcp.current_published_version_id is not None
            slot = (
                await session.execute(
                    select(McpCredentialSlotRow).where(
                        McpCredentialSlotRow.mcp_server_version_id == mcp.current_published_version_id,
                        McpCredentialSlotRow.name == "legacy-secrets",
                        McpCredentialSlotRow.required.is_(True),
                    )
                )
            ).scalar_one()
            grant = (
                await session.execute(
                    select(CredentialGrantRow).where(
                        CredentialGrantRow.mcp_server_version_id == mcp.current_published_version_id,
                        CredentialGrantRow.credential_slot_id == slot.id,
                        CredentialGrantRow.status == "active",
                    )
                )
            ).scalar_one()
            envelope = (
                await session.execute(
                    select(CredentialEnvelopeRow).where(
                        CredentialEnvelopeRow.credential_version_id == grant.credential_version_id,
                        CredentialEnvelopeRow.is_active.is_(True),
                    )
                )
            ).scalar_one()
            assert envelope.credential_version_id == grant.credential_version_id
            closures = await lock_mcp_credential_closures(
                session,
                (
                    McpCredentialClosureTarget(
                        mcp.current_published_version_id,
                        AssetScope.SYSTEM,
                        None,
                    ),
                ),
                load_envelopes=True,
            )
            closure = closures[mcp.current_published_version_id]
            assert closure.grants == (grant,)
            assert closure.materials[0].slot.name == "legacy-secrets"
    await engine.dispose()


@pytest.mark.asyncio
async def test_identical_agent_source_reuses_frozen_dependency_versions(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    actor_id = str(uuid.uuid4())
    repo = tmp_path / "repo"
    skill_path = repo / "skills/public/demo/SKILL.md"
    agent_dir = repo / "agents/research"
    skill_path.parent.mkdir(parents=True)
    agent_dir.mkdir(parents=True)
    skill_path.write_text("---\nname: demo\ndescription: demo\n---\none\n", encoding="utf-8")
    (agent_dir / "config.yaml").write_text("name: Research\nskills: [demo]\n", encoding="utf-8")
    (agent_dir / "SOUL.md").write_text("source-one", encoding="utf-8")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'system_admin',:now,false,0)"""
            ),
            {"id": actor_id, "email": f"frozen-{actor_id}@example.com", "now": datetime.now(UTC)},
        )
    owners = OwnerMap({}, system_actor=actor_id)
    first_inventory = build_inventory(SourceLayout(repo, tmp_path / "data"), owners)
    first_skill = next(item for item in first_inventory if item.source_key == "system-skill:demo")
    first_agent = next(item for item in first_inventory if item.source_key == "system-agent:research")
    runner = AssetMigrationRunner(factory, backup_root=tmp_path / "migrations")
    await runner.run((first_skill, first_agent), execute=True)

    async with factory() as session:
        agent = (await session.execute(select(AgentRow).where(AgentRow.source_key == first_agent.source_key))).scalar_one()
        original_agent_version_id = agent.current_published_version_id
        original_ref = (await session.execute(select(AgentVersionSkillRefRow.skill_version_id).where(AgentVersionSkillRefRow.agent_version_id == original_agent_version_id))).scalar_one()

    skill_path.write_text("---\nname: demo\ndescription: demo\n---\ntwo\n", encoding="utf-8")
    changed_inventory = build_inventory(SourceLayout(repo, tmp_path / "data"), owners)
    changed_skill = next(item for item in changed_inventory if item.source_key == first_skill.source_key)
    await runner.run((changed_skill,), execute=True)
    same_source = await runner.run((first_agent,), execute=True)

    assert same_source.created_versions == 0 and same_source.noop_versions == 1
    async with factory() as session:
        agent = (await session.execute(select(AgentRow).where(AgentRow.source_key == first_agent.source_key))).scalar_one()
        assert agent.current_published_version_id == original_agent_version_id
        assert int((await session.execute(select(func.count()).select_from(AgentVersionRow).where(AgentVersionRow.agent_id == agent.id))).scalar_one()) == 1
        frozen_ref = (await session.execute(select(AgentVersionSkillRefRow.skill_version_id).where(AgentVersionSkillRefRow.agent_version_id == original_agent_version_id))).scalar_one()
        assert frozen_ref == original_ref

    (agent_dir / "SOUL.md").write_text("source-two", encoding="utf-8")
    source_changed_inventory = build_inventory(SourceLayout(repo, tmp_path / "data"), owners)
    source_changed_agent = next(item for item in source_changed_inventory if item.source_key == first_agent.source_key)
    source_changed = await runner.run((source_changed_agent,), execute=True)
    assert source_changed.created_versions == 1
    async with factory() as session:
        agent = (await session.execute(select(AgentRow).where(AgentRow.source_key == first_agent.source_key))).scalar_one()
        assert int((await session.execute(select(func.count()).select_from(AgentVersionRow).where(AgentVersionRow.agent_id == agent.id))).scalar_one()) == 2
    async with factory() as session:
        async with session.begin():
            skill = (await session.execute(select(SkillRow).where(SkillRow.source_key == first_skill.source_key))).scalar_one()
            skill.status = "archived"
    async with factory() as session:
        async with session.begin():
            assert await runner._default_dependencies_probe(session, (source_changed_agent,)) is False
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_probe", ["counts", "checksums", "dependencies", "decrypt"])
async def test_cutover_requires_every_validation_probe(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    failed_probe: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    actor_id = str(uuid.uuid4())
    source = tmp_path / "SKILL.md"
    source.write_text("---\nname: invalid\ndescription: probe gate\n---\nbody\n", encoding="utf-8")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'system_admin',:now,false,0)"""
            ),
            {"id": actor_id, "email": f"invalid-{actor_id}@example.com", "now": datetime.now(UTC)},
        )
    item = InventoryItem.for_skill(
        source_key="system-skill:invalid",
        slug="invalid",
        display_name="Invalid",
        scope="system",
        project_id=None,
        owner_user_id=actor_id,
        files=(source,),
    )
    calls: list[str] = []

    def probe(name: str):
        def run(*_):
            calls.append(name)
            return name != failed_probe

        return run

    runner = AssetMigrationRunner(
        factory,
        backup_root=tmp_path / "migrations",
        validation_probes=MigrationValidationProbes(
            counts=probe("counts"),
            checksums=probe("checksums"),
            dependencies=probe("dependencies"),
            decrypt=probe("decrypt"),
        ),
    )

    with pytest.raises(RuntimeError, match=failed_probe):
        await runner.run((item,), execute=True)

    assert failed_probe in calls

    async with factory() as session:
        marker = await session.get(AssetCatalogStateRow, 1)
        assert marker is None or marker.cutover_at is None
    await engine.dispose()
