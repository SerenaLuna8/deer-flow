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


async def _insert_system_asset_version(
    engine: AsyncEngine,
    *,
    user_id: str,
    kind: str,
    workflow_status: str,
    version_number: int = 1,
) -> tuple[uuid.UUID, uuid.UUID]:
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    slug = f"{kind}-{str(asset_id)[:8]}"
    if kind == "agent":
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """INSERT INTO agents
                    (id,scope,slug,display_name,created_by_user_id)
                    VALUES (:id,'system',:slug,:slug,:user_id)"""
                ),
                {"id": asset_id, "slug": slug, "user_id": user_id},
            )
        await _insert_agent_version(
            engine,
            user_id=user_id,
            agent_id=asset_id,
            version_number=version_number,
            workflow_status=workflow_status,
        )
        async with engine.connect() as conn:
            version_id = (
                await conn.execute(
                    text("SELECT id FROM agent_versions WHERE agent_id=:asset_id AND version_number=:number"),
                    {"asset_id": asset_id, "number": version_number},
                )
            ).scalar_one()
    elif kind == "skill":
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """INSERT INTO skills
                    (id,scope,slug,display_name,created_by_user_id)
                    VALUES (:id,'system',:slug,:slug,:user_id)"""
                ),
                {"id": asset_id, "slug": slug, "user_id": user_id},
            )
            await conn.execute(
                text(
                    """INSERT INTO skill_versions
                    (id,skill_id,version_number,workflow_status,description,
                     frontmatter,secret_requirements,scan_decision,scan_summary,
                     payload_checksum,created_by_user_id)
                    VALUES (:id,:asset_id,:number,:status,'','{}'::jsonb,
                            '[]'::jsonb,'allow','{}'::jsonb,:checksum,:user_id)"""
                ),
                {
                    "id": version_id,
                    "asset_id": asset_id,
                    "number": version_number,
                    "status": workflow_status,
                    "checksum": f"{version_number + 100:064x}",
                    "user_id": user_id,
                },
            )
    elif kind == "mcp":
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """INSERT INTO mcp_servers
                    (id,scope,slug,display_name,created_by_user_id)
                    VALUES (:id,'system',:slug,:slug,:user_id)"""
                ),
                {"id": asset_id, "slug": slug, "user_id": user_id},
            )
            await conn.execute(
                text(
                    """INSERT INTO mcp_server_versions
                    (id,mcp_server_id,version_number,workflow_status,description,
                     transport,args,non_secret_env,non_secret_headers,
                     oauth_metadata,routing,tool_overrides,timeout_seconds,
                     payload_checksum,created_by_user_id)
                    VALUES (:id,:asset_id,:number,:status,'','stdio','[]'::jsonb,
                            '{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'{}'::jsonb,
                            '{}'::jsonb,30,:checksum,:user_id)"""
                ),
                {
                    "id": version_id,
                    "asset_id": asset_id,
                    "number": version_number,
                    "status": workflow_status,
                    "checksum": f"{version_number + 200:064x}",
                    "user_id": user_id,
                },
            )
    else:  # pragma: no cover - test helper guard
        raise AssertionError(f"unknown asset kind: {kind}")
    return asset_id, version_id


async def _insert_system_binding(
    engine: AsyncEngine,
    *,
    kind: str,
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    user_id: str,
) -> None:
    table, asset_column, version_column = {
        "agent": ("project_system_agent_bindings", "system_agent_id", "agent_version_id"),
        "skill": ("project_system_skill_bindings", "system_skill_id", "skill_version_id"),
        "mcp": ("project_system_mcp_bindings", "system_mcp_server_id", "mcp_server_version_id"),
    }[kind]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"""INSERT INTO {table}
                (project_id,{asset_column},{version_column},created_by_user_id,updated_by_user_id)
                VALUES (:project_id,:asset_id,:version_id,:user_id,:user_id)"""  # noqa: S608 - fixed test-only allowlist
            ),
            {
                "project_id": project_id,
                "asset_id": asset_id,
                "version_id": version_id,
                "user_id": user_id,
            },
        )


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
@pytest.mark.parametrize("kind", ["agent", "skill", "mcp"])
async def test_system_bindings_accept_only_published_versions(
    migrated_postgres_database_url: str,
    kind: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    user_id, project_id = await _seed_user_and_project(engine)
    try:
        for number, status in enumerate(("draft", "pending_approval", "rejected"), start=1):
            asset_id, version_id = await _insert_system_asset_version(
                engine,
                user_id=user_id,
                kind=kind,
                workflow_status=status,
                version_number=number,
            )
            with pytest.raises(IntegrityError, match="published"):
                await _insert_system_binding(
                    engine,
                    kind=kind,
                    project_id=project_id,
                    asset_id=asset_id,
                    version_id=version_id,
                    user_id=user_id,
                )

        published_asset_id, published_version_id = await _insert_system_asset_version(
            engine,
            user_id=user_id,
            kind=kind,
            workflow_status="published",
            version_number=4,
        )
        await _insert_system_binding(
            engine,
            kind=kind,
            project_id=project_id,
            asset_id=published_asset_id,
            version_id=published_version_id,
            user_id=user_id,
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "version_table"),
    [
        ("agent", "agent_versions"),
        ("skill", "skill_versions"),
        ("mcp", "mcp_server_versions"),
    ],
)
async def test_bound_published_version_cannot_be_downgraded(
    migrated_postgres_database_url: str,
    kind: str,
    version_table: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    user_id, project_id = await _seed_user_and_project(engine)
    try:
        asset_id, version_id = await _insert_system_asset_version(
            engine,
            user_id=user_id,
            kind=kind,
            workflow_status="published",
        )
        await _insert_system_binding(
            engine,
            kind=kind,
            project_id=project_id,
            asset_id=asset_id,
            version_id=version_id,
            user_id=user_id,
        )

        with pytest.raises(DBAPIError, match="bound published"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(f"UPDATE {version_table} SET workflow_status='rejected' WHERE id=:id"),  # noqa: S608 - fixed parametrized table allowlist
                    {"id": version_id},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "skill", "mcp"])
async def test_asset_version_workflow_state_machine_allows_only_declared_transitions(
    migrated_postgres_database_url: str,
    kind: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    user_id, _project_id = await _seed_user_and_project(engine)
    version_table = {
        "agent": "agent_versions",
        "skill": "skill_versions",
        "mcp": "mcp_server_versions",
    }[kind]
    allowed = (
        ("draft", "pending_approval"),
        ("draft", "published"),
        ("pending_approval", "published"),
        ("pending_approval", "rejected"),
    )
    forbidden = (
        ("draft", "rejected"),
        ("pending_approval", "draft"),
        ("published", "draft"),
        ("published", "pending_approval"),
        ("published", "rejected"),
        ("rejected", "draft"),
        ("rejected", "pending_approval"),
        ("rejected", "published"),
    )
    try:
        for number, (old_status, new_status) in enumerate(allowed, start=1):
            _asset_id, version_id = await _insert_system_asset_version(
                engine,
                user_id=user_id,
                kind=kind,
                workflow_status=old_status,
                version_number=number,
            )
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        f"""UPDATE {version_table}
                        SET workflow_status=:new_status,review_note='reviewed'
                        WHERE id=:id"""  # noqa: S608 - fixed test allowlist
                    ),
                    {"new_status": new_status, "id": version_id},
                )

        for number, (old_status, new_status) in enumerate(forbidden, start=101):
            _asset_id, version_id = await _insert_system_asset_version(
                engine,
                user_id=user_id,
                kind=kind,
                workflow_status=old_status,
                version_number=number,
            )
            with pytest.raises(IntegrityError, match="invalid shared asset version workflow transition"):
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            f"""UPDATE {version_table}
                            SET workflow_status=:new_status WHERE id=:id"""  # noqa: S608 - fixed test allowlist
                        ),
                        {"new_status": new_status, "id": version_id},
                    )

        _asset_id, stable_version_id = await _insert_system_asset_version(
            engine,
            user_id=user_id,
            kind=kind,
            workflow_status="published",
            version_number=999,
        )
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"""UPDATE {version_table}
                    SET workflow_status=workflow_status,review_note='metadata only'
                    WHERE id=:id"""  # noqa: S608 - fixed test allowlist
                ),
                {"id": stable_version_id},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_credential_version_status_state_machine_is_irreversible(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    user_id, _project_id = await _seed_user_and_project(engine)

    async def insert_version(status: str, number: int) -> uuid.UUID:
        credential_id, version_id = uuid.uuid4(), uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """INSERT INTO credentials
                    (id,scope,name,display_name,credential_type,created_by_user_id)
                    VALUES (:id,'system',:name,:name,'token',:user_id)"""
                ),
                {"id": credential_id, "name": f"credential-{number}", "user_id": user_id},
            )
            await conn.execute(
                text(
                    """INSERT INTO credential_versions
                    (id,credential_id,version_number,status,payload_schema,
                     created_by_user_id)
                    VALUES (:id,:credential_id,:number,:status,'{}'::jsonb,:user_id)"""
                ),
                {
                    "id": version_id,
                    "credential_id": credential_id,
                    "number": number,
                    "status": status,
                    "user_id": user_id,
                },
            )
        return version_id

    try:
        for number, (old_status, new_status) in enumerate(
            (("active", "retired"), ("active", "revoked"), ("retired", "revoked")),
            start=1,
        ):
            version_id = await insert_version(old_status, number)
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE credential_versions SET status=:status WHERE id=:id"),
                    {"status": new_status, "id": version_id},
                )

        for number, (old_status, new_status) in enumerate(
            (("retired", "active"), ("revoked", "active"), ("revoked", "retired")),
            start=101,
        ):
            version_id = await insert_version(old_status, number)
            with pytest.raises(IntegrityError, match="invalid credential version status transition"):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE credential_versions SET status=:status WHERE id=:id"),
                        {"status": new_status, "id": version_id},
                    )

        stable_version_id = await insert_version("revoked", 999)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """UPDATE credential_versions
                    SET status=status,revoked_at=now(),revoked_by_user_id=:user_id
                    WHERE id=:id"""
                ),
                {"user_id": user_id, "id": stable_version_id},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_kind", ["skill_file", "agent_skill_ref", "agent_mcp_ref", "mcp_slot"])
async def test_published_version_child_rows_reject_insert_and_delete(
    migrated_postgres_database_url: str,
    child_kind: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    user_id, _project_id = await _seed_user_and_project(engine)
    try:
        if child_kind == "skill_file":
            _asset_id, parent_version_id = await _insert_system_asset_version(
                engine,
                user_id=user_id,
                kind="skill",
                workflow_status="draft",
            )
            initial_params = {
                "version_id": parent_version_id,
                "path": "SKILL.md",
                "sha256": "a" * 64,
                "content": b"a",
            }
            added_params = {
                "version_id": parent_version_id,
                "path": "extra.txt",
                "sha256": "b" * 64,
                "content": b"b",
            }
            insert_sql = """INSERT INTO skill_version_files
                (skill_version_id,path,media_type,size_bytes,sha256,content)
                VALUES (:version_id,:path,'text/plain',1,:sha256,:content)"""
            delete_sql = """DELETE FROM skill_version_files
                WHERE skill_version_id=:version_id AND path=:path"""
            parent_table = "skill_versions"
        elif child_kind == "agent_skill_ref":
            _asset_id, parent_version_id = await _insert_system_asset_version(
                engine,
                user_id=user_id,
                kind="agent",
                workflow_status="draft",
            )
            _dependency_id, initial_dependency = await _insert_system_asset_version(
                engine,
                user_id=user_id,
                kind="skill",
                workflow_status="published",
            )
            _dependency_id, added_dependency = await _insert_system_asset_version(
                engine,
                user_id=user_id,
                kind="skill",
                workflow_status="published",
                version_number=2,
            )
            initial_params = {"version_id": parent_version_id, "dependency_id": initial_dependency}
            added_params = {"version_id": parent_version_id, "dependency_id": added_dependency}
            insert_sql = """INSERT INTO agent_version_skill_refs
                (agent_version_id,skill_version_id)
                VALUES (:version_id,:dependency_id)"""
            delete_sql = """DELETE FROM agent_version_skill_refs
                WHERE agent_version_id=:version_id AND skill_version_id=:dependency_id"""
            parent_table = "agent_versions"
        elif child_kind == "agent_mcp_ref":
            _asset_id, parent_version_id = await _insert_system_asset_version(
                engine,
                user_id=user_id,
                kind="agent",
                workflow_status="draft",
            )
            _dependency_id, initial_dependency = await _insert_system_asset_version(
                engine,
                user_id=user_id,
                kind="mcp",
                workflow_status="published",
            )
            _dependency_id, added_dependency = await _insert_system_asset_version(
                engine,
                user_id=user_id,
                kind="mcp",
                workflow_status="published",
                version_number=2,
            )
            initial_params = {"version_id": parent_version_id, "dependency_id": initial_dependency}
            added_params = {"version_id": parent_version_id, "dependency_id": added_dependency}
            insert_sql = """INSERT INTO agent_version_mcp_refs
                (agent_version_id,mcp_server_version_id)
                VALUES (:version_id,:dependency_id)"""
            delete_sql = """DELETE FROM agent_version_mcp_refs
                WHERE agent_version_id=:version_id AND mcp_server_version_id=:dependency_id"""
            parent_table = "agent_versions"
        else:
            _asset_id, parent_version_id = await _insert_system_asset_version(
                engine,
                user_id=user_id,
                kind="mcp",
                workflow_status="draft",
            )
            initial_params = {"id": uuid.uuid4(), "version_id": parent_version_id, "name": "token"}
            added_params = {"id": uuid.uuid4(), "version_id": parent_version_id, "name": "refresh"}
            insert_sql = """INSERT INTO mcp_version_credential_slots
                (id,mcp_server_version_id,name,purpose,payload_schema)
                VALUES (:id,:version_id,:name,'','{}'::jsonb)"""
            delete_sql = "DELETE FROM mcp_version_credential_slots WHERE id=:id"
            parent_table = "mcp_server_versions"

        async with engine.begin() as conn:
            await conn.execute(text(insert_sql), initial_params)
            assert (
                await conn.execute(
                    text(f"SELECT count(*) FROM {parent_table} WHERE id=:version_id AND workflow_status='draft'"),  # noqa: S608 - fixed test allowlist
                    {"version_id": parent_version_id},
                )
            ).scalar_one() == 1
            await conn.execute(
                text(f"UPDATE {parent_table} SET workflow_status='published' WHERE id=:version_id"),  # noqa: S608 - fixed test allowlist
                {"version_id": parent_version_id},
            )

        with pytest.raises(IntegrityError, match="invalid shared asset version workflow transition"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(f"UPDATE {parent_table} SET workflow_status='draft' WHERE id=:version_id"),  # noqa: S608 - fixed test allowlist
                    {"version_id": parent_version_id},
                )

        with pytest.raises(IntegrityError, match="published version child rows are immutable"):
            async with engine.begin() as conn:
                await conn.execute(text(insert_sql), added_params)
        with pytest.raises(IntegrityError, match="published version child rows are immutable"):
            async with engine.begin() as conn:
                await conn.execute(text(delete_sql), initial_params)
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
