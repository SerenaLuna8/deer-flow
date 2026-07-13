from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.shared_assets.keyring import CredentialKeyring
from deerflow.persistence.shared_assets import AssetCatalogStateRow, SkillRow, SkillVersionRow
from scripts.migrate_assets import (
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
