from __future__ import annotations

import asyncio
import importlib
import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import _get_alembic_config

EXPECTED_TABLES = {
    "agents",
    "agent_versions",
    "agent_version_skill_refs",
    "agent_version_mcp_refs",
    "skills",
    "skill_versions",
    "skill_version_files",
    "mcp_servers",
    "mcp_server_versions",
    "mcp_version_credential_slots",
    "credentials",
    "credential_versions",
    "credential_envelopes",
    "credential_grants",
    "project_system_agent_bindings",
    "project_system_skill_bindings",
    "project_system_mcp_bindings",
    "asset_catalog_state",
}


async def _seed_user_and_project(engine: AsyncEngine) -> tuple[str, uuid.UUID]:
    user_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """INSERT INTO users
                (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'system_admin',:now,false,0)"""
            ),
            {
                "id": user_id,
                "email": f"{user_id}@example.com",
                "now": datetime.now(UTC),
            },
        )
        await conn.execute(
            text(
                """INSERT INTO projects (id,slug,display_name,created_by_user_id)
                VALUES (:id,:slug,'Schema Project',:user_id)"""
            ),
            {
                "id": project_id,
                "slug": f"schema-{str(project_id)[:8]}",
                "user_id": user_id,
            },
        )
    return user_id, project_id


async def _insert_agent(
    engine: AsyncEngine,
    *,
    user_id: str,
    scope: str,
    project_id: uuid.UUID | None,
    slug: str,
) -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """INSERT INTO agents
                (id,scope,project_id,slug,display_name,created_by_user_id)
                VALUES (:id,:scope,:project_id,:slug,:display_name,:user_id)"""
            ),
            {
                "id": agent_id,
                "scope": scope,
                "project_id": project_id,
                "slug": slug,
                "display_name": slug.title(),
                "user_id": user_id,
            },
        )
    return agent_id


async def _insert_agent_version(
    engine: AsyncEngine,
    *,
    user_id: str,
    agent_id: uuid.UUID,
    version_number: int = 1,
    workflow_status: str = "draft",
) -> uuid.UUID:
    version_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """INSERT INTO agent_versions
                (id,agent_id,version_number,workflow_status,description,soul,
                 model_ref,tool_groups,payload_checksum,created_by_user_id)
                VALUES (:id,:agent_id,:version_number,:workflow_status,
                        'description','original soul','default','[]'::jsonb,
                        :checksum,:user_id)"""
            ),
            {
                "id": version_id,
                "agent_id": agent_id,
                "version_number": version_number,
                "workflow_status": workflow_status,
                "checksum": f"{version_number:064x}",
                "user_id": user_id,
            },
        )
    return version_id


@pytest.mark.asyncio
async def test_m3_schema_has_all_typed_tables(migrated_postgres_database_url: str) -> None:
    importlib.import_module("deerflow.persistence.models")
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        assert EXPECTED_TABLES <= tables
        assert revision == "0007_project_shared_assets"
        assert EXPECTED_TABLES <= set(Base.metadata.tables)

        shared_assets = importlib.import_module("deerflow.persistence.shared_assets")
        assert {
            "AgentRow",
            "AgentVersionRow",
            "SkillRow",
            "SkillVersionRow",
            "SkillVersionFileRow",
            "McpServerRow",
            "McpServerVersionRow",
            "McpCredentialSlotRow",
            "CredentialRow",
            "CredentialVersionRow",
            "CredentialEnvelopeRow",
            "CredentialGrantRow",
            "ProjectSystemAgentBindingRow",
            "ProjectSystemSkillBindingRow",
            "ProjectSystemMcpBindingRow",
            "AssetCatalogStateRow",
        } <= set(shared_assets.__all__)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_asset_scope_checks_and_partial_slug_indexes_allow_same_name_across_scopes(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    user_id, project_id = await _seed_user_and_project(engine)
    try:
        await _insert_agent(engine, user_id=user_id, scope="system", project_id=None, slug="analyst")
        await _insert_agent(engine, user_id=user_id, scope="project", project_id=project_id, slug="analyst")

        with pytest.raises(IntegrityError):
            await _insert_agent(engine, user_id=user_id, scope="system", project_id=None, slug="ANALYST")
        with pytest.raises(IntegrityError):
            await _insert_agent(engine, user_id=user_id, scope="project", project_id=project_id, slug="ANALYST")
        with pytest.raises(IntegrityError):
            await _insert_agent(engine, user_id=user_id, scope="system", project_id=project_id, slug="bad-system")
        with pytest.raises(IntegrityError):
            await _insert_agent(engine, user_id=user_id, scope="project", project_id=None, slug="bad-project")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_system_binding_composite_foreign_keys_pin_a_version_of_that_system_asset(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    user_id, project_id = await _seed_user_and_project(engine)
    try:
        system_agent_id = await _insert_agent(engine, user_id=user_id, scope="system", project_id=None, slug="system-agent")
        other_agent_id = await _insert_agent(engine, user_id=user_id, scope="system", project_id=None, slug="other-agent")
        project_agent_id = await _insert_agent(engine, user_id=user_id, scope="project", project_id=project_id, slug="project-agent")
        system_version_id = await _insert_agent_version(engine, user_id=user_id, agent_id=system_agent_id, workflow_status="published")
        other_version_id = await _insert_agent_version(engine, user_id=user_id, agent_id=other_agent_id, workflow_status="published")
        project_version_id = await _insert_agent_version(engine, user_id=user_id, agent_id=project_agent_id, workflow_status="published")

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """INSERT INTO project_system_agent_bindings
                    (project_id,system_agent_id,agent_version_id,created_by_user_id,updated_by_user_id)
                    VALUES (:project_id,:agent_id,:version_id,:user_id,:user_id)"""
                ),
                {
                    "project_id": project_id,
                    "agent_id": system_agent_id,
                    "version_id": system_version_id,
                    "user_id": user_id,
                },
            )

        for agent_id, version_id in (
            (system_agent_id, other_version_id),
            (project_agent_id, project_version_id),
        ):
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM project_system_agent_bindings"))
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            """INSERT INTO project_system_agent_bindings
                            (project_id,system_agent_id,agent_version_id,created_by_user_id,updated_by_user_id)
                            VALUES (:project_id,:agent_id,:version_id,:user_id,:user_id)"""
                        ),
                        {
                            "project_id": project_id,
                            "agent_id": agent_id,
                            "version_id": version_id,
                            "user_id": user_id,
                        },
                    )

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        """UPDATE agents SET current_published_version_id=:version_id
                        WHERE id=:agent_id"""
                    ),
                    {"version_id": other_version_id, "agent_id": system_agent_id},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_version_payload_is_immutable_but_workflow_metadata_can_change(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    user_id, _project_id = await _seed_user_and_project(engine)
    try:
        agent_id = await _insert_agent(engine, user_id=user_id, scope="system", project_id=None, slug="immutable-agent")
        version_id = await _insert_agent_version(engine, user_id=user_id, agent_id=agent_id)

        with pytest.raises(DBAPIError, match="immutable"):
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE agent_versions SET soul='changed' WHERE id=:id"), {"id": version_id})

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """UPDATE agent_versions
                    SET workflow_status='pending_approval',submitted_at=now()
                    WHERE id=:id"""
                ),
                {"id": version_id},
            )
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT soul,workflow_status,submitted_at FROM agent_versions WHERE id=:id"),
                    {"id": version_id},
                )
            ).one()
        assert row.soul == "original soul"
        assert row.workflow_status == "pending_approval"
        assert row.submitted_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_referenced_asset_version_cannot_be_deleted(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    user_id, project_id = await _seed_user_and_project(engine)
    try:
        agent_id = await _insert_agent(engine, user_id=user_id, scope="system", project_id=None, slug="bound-agent")
        version_id = await _insert_agent_version(engine, user_id=user_id, agent_id=agent_id, workflow_status="published")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """INSERT INTO project_system_agent_bindings
                    (project_id,system_agent_id,agent_version_id,created_by_user_id,updated_by_user_id)
                    VALUES (:project_id,:agent_id,:version_id,:user_id,:user_id)"""
                ),
                {
                    "project_id": project_id,
                    "agent_id": agent_id,
                    "version_id": version_id,
                    "user_id": user_id,
                },
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM agent_versions WHERE id=:id"), {"id": version_id})
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_state_is_unseeded_singleton_and_resolution_changes_bump_generation(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    user_id, project_id = await _seed_user_and_project(engine)
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM asset_catalog_state"))).scalar_one() == 0

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO asset_catalog_state (id,generation) VALUES (2,1)"))

        agent_id = await _insert_agent(engine, user_id=user_id, scope="system", project_id=None, slug="generation-agent")
        version_id = await _insert_agent_version(engine, user_id=user_id, agent_id=agent_id, workflow_status="published")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """INSERT INTO project_system_agent_bindings
                    (project_id,system_agent_id,agent_version_id,created_by_user_id,updated_by_user_id)
                    VALUES (:project_id,:agent_id,:version_id,:user_id,:user_id)"""
                ),
                {
                    "project_id": project_id,
                    "agent_id": agent_id,
                    "version_id": version_id,
                    "user_id": user_id,
                },
            )
        async with engine.connect() as conn:
            first_generation = (await conn.execute(text("SELECT generation FROM asset_catalog_state WHERE id=1"))).scalar_one()
        assert first_generation >= 1

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """UPDATE project_system_agent_bindings
                    SET enabled=false,version=version+1,updated_by_user_id=:user_id
                    WHERE project_id=:project_id AND system_agent_id=:agent_id"""
                ),
                {"user_id": user_id, "project_id": project_id, "agent_id": agent_id},
            )
        async with engine.connect() as conn:
            second_generation = (await conn.execute(text("SELECT generation FROM asset_catalog_state WHERE id=1"))).scalar_one()
        assert second_generation > first_generation
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_m3_schema_can_downgrade(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(command.upgrade, cfg, "head")
        await asyncio.to_thread(command.downgrade, cfg, "0006_project_governance")
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        assert not (EXPECTED_TABLES & tables)
        assert revision == "0006_project_governance"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_populated_m3_schema_refuses_downgrade_before_mutation(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(command.upgrade, cfg, "head")
        user_id, _project_id = await _seed_user_and_project(engine)
        await _insert_agent(engine, user_id=user_id, scope="system", project_id=None, slug="keep-schema")

        with pytest.raises(RuntimeError, match="M3 shared asset data exists"):
            await asyncio.to_thread(command.downgrade, cfg, "0006_project_governance")

        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        assert EXPECTED_TABLES <= tables
        assert revision == "0007_project_shared_assets"
    finally:
        await engine.dispose()
