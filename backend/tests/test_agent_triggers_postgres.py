from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.projects.context import ProjectContext, resolve_project_context
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.default_agent_service import ProjectDefaultAgentService
from app.shared_assets.errors import AssetConflict, AssetForbidden
from app.shared_assets.models import AgentPayload


@dataclass(frozen=True)
class _TriggerSeed:
    user_id: uuid.UUID
    project_id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    skill_version_ids: tuple[uuid.UUID, uuid.UUID]
    mcp_version_ids: tuple[uuid.UUID, uuid.UUID]


@dataclass(frozen=True)
class _DefaultAgentSeed:
    admin: ProjectContext
    editor: ProjectContext
    outsider_admin: ProjectContext
    agent_id: uuid.UUID
    outsider_agent_id: uuid.UUID


def _assert_database_message(error: DBAPIError, expected: str) -> None:
    assert expected in str(error.orig)


async def _seed_trigger_graph(engine: AsyncEngine) -> _TriggerSeed:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()
    skill_ids = (uuid.uuid4(), uuid.uuid4())
    skill_version_ids = (uuid.uuid4(), uuid.uuid4())
    mcp_ids = (uuid.uuid4(), uuid.uuid4())
    mcp_version_ids = (uuid.uuid4(), uuid.uuid4())

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users
                (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'user',now(),false,0)"""
            ),
            {
                "id": str(user_id),
                "email": f"agent-triggers-{user_id}@example.com",
            },
        )
        await connection.execute(
            text(
                """INSERT INTO projects
                (id,slug,display_name,created_by_user_id)
                VALUES (:id,:slug,'Agent trigger tests',:owner)"""
            ),
            {
                "id": project_id,
                "slug": f"agent-triggers-{project_id.hex[:8]}",
                "owner": str(user_id),
            },
        )
        await connection.execute(
            text(
                """INSERT INTO agents
                (id,scope,project_id,slug,display_name,status,
                 created_by_user_id)
                VALUES
                (:id,'project',:project,'trigger-agent','Trigger Agent',
                 'active',:owner)"""
            ),
            {
                "id": agent_id,
                "project": project_id,
                "owner": str(user_id),
            },
        )
        await connection.execute(
            text(
                """INSERT INTO agent_versions
                (id,agent_id,version_number,workflow_status,description,
                 agents_instructions,soul,identity,user_context,model_ref,
                 model_settings,tool_groups,payload_schema_version,
                 payload_checksum,created_by_user_id)
                VALUES
                (:id,:agent,1,'draft','Initial description','# Agent',
                 '# Soul','# Identity','# User','default','{}'::jsonb,
                 '["research"]'::jsonb,3,:checksum,:owner)"""
            ),
            {
                "id": agent_version_id,
                "agent": agent_id,
                "checksum": "a" * 64,
                "owner": str(user_id),
            },
        )
        await connection.execute(
            text(
                """INSERT INTO skills
                (id,scope,project_id,slug,display_name,status,
                 created_by_user_id)
                VALUES
                (:id,'project',:project,:slug,:display,'active',:owner)"""
            ),
            [
                {
                    "id": skill_id,
                    "project": project_id,
                    "slug": f"trigger-skill-{index}",
                    "display": f"Trigger Skill {index}",
                    "owner": str(user_id),
                }
                for index, skill_id in enumerate(skill_ids, start=1)
            ],
        )
        await connection.execute(
            text(
                """INSERT INTO skill_versions
                (id,skill_id,version_number,workflow_status,scan_decision,
                 payload_checksum,created_by_user_id)
                VALUES
                (:id,:asset,1,'published','allow',:checksum,:owner)"""
            ),
            [
                {
                    "id": version_id,
                    "asset": asset_id,
                    "checksum": str(index) * 64,
                    "owner": str(user_id),
                }
                for index, (asset_id, version_id) in enumerate(
                    zip(skill_ids, skill_version_ids, strict=True),
                    start=1,
                )
            ],
        )
        await connection.execute(
            text(
                """UPDATE skills
                SET current_published_version_id=:version
                WHERE id=:asset"""
            ),
            [
                {"asset": asset_id, "version": version_id}
                for asset_id, version_id in zip(
                    skill_ids,
                    skill_version_ids,
                    strict=True,
                )
            ],
        )
        await connection.execute(
            text(
                """INSERT INTO mcp_servers
                (id,scope,project_id,slug,display_name,status,
                 created_by_user_id)
                VALUES
                (:id,'project',:project,:slug,:display,'active',:owner)"""
            ),
            [
                {
                    "id": mcp_id,
                    "project": project_id,
                    "slug": f"trigger-mcp-{index}",
                    "display": f"Trigger MCP {index}",
                    "owner": str(user_id),
                }
                for index, mcp_id in enumerate(mcp_ids, start=1)
            ],
        )
        await connection.execute(
            text(
                """INSERT INTO mcp_server_versions
                (id,mcp_server_id,version_number,workflow_status,transport,
                 payload_checksum,created_by_user_id)
                VALUES
                (:id,:asset,1,'published','stdio',:checksum,:owner)"""
            ),
            [
                {
                    "id": version_id,
                    "asset": asset_id,
                    "checksum": str(index + 2) * 64,
                    "owner": str(user_id),
                }
                for index, (asset_id, version_id) in enumerate(
                    zip(mcp_ids, mcp_version_ids, strict=True),
                    start=1,
                )
            ],
        )
        await connection.execute(
            text(
                """UPDATE mcp_servers
                SET current_published_version_id=:version
                WHERE id=:asset"""
            ),
            [
                {"asset": asset_id, "version": version_id}
                for asset_id, version_id in zip(
                    mcp_ids,
                    mcp_version_ids,
                    strict=True,
                )
            ],
        )
        await connection.execute(
            text(
                """INSERT INTO agent_version_skill_refs
                (agent_version_id,skill_version_id,sort_order)
                VALUES (:agent_version,:dependency,0)"""
            ),
            {
                "agent_version": agent_version_id,
                "dependency": skill_version_ids[0],
            },
        )
        await connection.execute(
            text(
                """INSERT INTO agent_version_mcp_refs
                (agent_version_id,mcp_server_version_id,sort_order)
                VALUES (:agent_version,:dependency,0)"""
            ),
            {
                "agent_version": agent_version_id,
                "dependency": mcp_version_ids[0],
            },
        )

    return _TriggerSeed(
        user_id=user_id,
        project_id=project_id,
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        skill_version_ids=skill_version_ids,
        mcp_version_ids=mcp_version_ids,
    )


async def _resolve_context(
    factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    request_id: str,
) -> ProjectContext:
    async with factory() as session:
        return await resolve_project_context(
            session,
            user_id,
            project_id,
            request_id,
        )


async def _seed_default_agent_matrix(
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
) -> _DefaultAgentSeed:
    admin_id = uuid.uuid4()
    editor_id = uuid.uuid4()
    outsider_id = uuid.uuid4()
    project_id = uuid.uuid4()
    outsider_project_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    outsider_agent_id = uuid.uuid4()
    version_id = uuid.uuid4()
    outsider_version_id = uuid.uuid4()
    default_agent_checksum = agent_payload_checksum(
        AgentPayload(
            description="Default Agent",
            soul="Reliable",
            model_ref="default",
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
            payload_schema_version=3,
        )
    )

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users
                (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'user',now(),false,0)"""
            ),
            [
                {
                    "id": str(user_id),
                    "email": f"default-agent-{label}-{user_id}@example.com",
                }
                for label, user_id in (
                    ("admin", admin_id),
                    ("editor", editor_id),
                    ("outsider", outsider_id),
                )
            ],
        )
        await connection.execute(
            text(
                """INSERT INTO projects
                (id,slug,display_name,created_by_user_id)
                VALUES (:id,:slug,:display,:owner)"""
            ),
            [
                {
                    "id": project_id,
                    "slug": f"default-agent-{project_id.hex[:8]}",
                    "display": "Default Agent Project",
                    "owner": str(admin_id),
                },
                {
                    "id": outsider_project_id,
                    "slug": f"default-agent-{outsider_project_id.hex[:8]}",
                    "display": "Outsider Agent Project",
                    "owner": str(outsider_id),
                },
            ],
        )
        await connection.execute(
            text(
                """INSERT INTO project_memberships
                (id,project_id,user_id,role,status,version)
                VALUES (:id,:project,:user,:role,'active',1)"""
            ),
            [
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "user": str(admin_id),
                    "role": "admin",
                },
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "user": str(editor_id),
                    "role": "editor",
                },
                {
                    "id": uuid.uuid4(),
                    "project": outsider_project_id,
                    "user": str(outsider_id),
                    "role": "admin",
                },
            ],
        )
        await connection.execute(
            text(
                """INSERT INTO agents
                (id,scope,project_id,slug,display_name,status,
                 created_by_user_id)
                VALUES
                (:id,'project',:project,:slug,:display,'active',:owner)"""
            ),
            [
                {
                    "id": agent_id,
                    "project": project_id,
                    "slug": "default-agent-target",
                    "display": "Default Agent Target",
                    "owner": str(admin_id),
                },
                {
                    "id": outsider_agent_id,
                    "project": outsider_project_id,
                    "slug": "outsider-agent-target",
                    "display": "Outsider Agent Target",
                    "owner": str(outsider_id),
                },
            ],
        )
        await connection.execute(
            text(
                """INSERT INTO agent_versions
                (id,agent_id,version_number,workflow_status,description,
                 soul,model_ref,model_settings,tool_groups,
                 payload_schema_version,payload_checksum,created_by_user_id)
                VALUES
                (:id,:agent,1,'published','Default Agent','Reliable',
                 'default','{}'::jsonb,'[]'::jsonb,3,:checksum,:owner)"""
            ),
            [
                {
                    "id": version_id,
                    "agent": agent_id,
                    "checksum": default_agent_checksum,
                    "owner": str(admin_id),
                },
                {
                    "id": outsider_version_id,
                    "agent": outsider_agent_id,
                    "checksum": default_agent_checksum,
                    "owner": str(outsider_id),
                },
            ],
        )
        await connection.execute(
            text(
                """UPDATE agents
                SET current_published_version_id=:version
                WHERE id=:asset"""
            ),
            [
                {"asset": agent_id, "version": version_id},
                {
                    "asset": outsider_agent_id,
                    "version": outsider_version_id,
                },
            ],
        )

    admin = await _resolve_context(
        factory,
        admin_id,
        project_id,
        "req-default-admin",
    )
    editor = await _resolve_context(
        factory,
        editor_id,
        project_id,
        "req-default-editor",
    )
    outsider_admin = await _resolve_context(
        factory,
        outsider_id,
        outsider_project_id,
        "req-default-outsider",
    )
    return _DefaultAgentSeed(
        admin=admin,
        editor=editor,
        outsider_admin=outsider_admin,
        agent_id=agent_id,
        outsider_agent_id=outsider_agent_id,
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_agent_version_payload_is_immutable_and_published_is_terminal(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        seed = await _seed_trigger_graph(engine)
        payload_mutations = (
            "SET version_number=2",
            "SET description='Changed description'",
            "SET agents_instructions='# Changed Agent'",
            "SET soul='# Changed Soul'",
            "SET identity='# Changed Identity'",
            "SET user_context='# Changed User'",
            "SET model_ref='changed-model'",
            "SET model_settings=jsonb_build_object('temperature',0.5)",
            "SET tool_groups='[\"changed\"]'::jsonb",
            "SET supersedes_version_id=id",
            f"SET payload_checksum='{'b' * 64}'",
            "SET payload_schema_version=2",
        )
        for mutation in payload_mutations:
            with pytest.raises(DBAPIError) as error:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            f"""UPDATE agent_versions {mutation}
                            WHERE id=:version"""
                        ),
                        {"version": seed.agent_version_id},
                    )
            _assert_database_message(
                error.value,
                "shared asset version payload is immutable",
            )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE agent_versions SET workflow_status='published'
                    WHERE id=:version"""
                ),
                {"version": seed.agent_version_id},
            )
            await connection.execute(
                text(
                    """UPDATE agents SET current_published_version_id=:version
                    WHERE id=:agent"""
                ),
                {
                    "agent": seed.agent_id,
                    "version": seed.agent_version_id,
                },
            )

        for target_status in ("draft", "pending_approval", "rejected"):
            with pytest.raises(DBAPIError) as error:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            """UPDATE agent_versions
                            SET workflow_status=:target_status
                            WHERE id=:version"""
                        ),
                        {
                            "target_status": target_status,
                            "version": seed.agent_version_id,
                        },
                    )
            _assert_database_message(
                error.value,
                "invalid shared asset version workflow transition",
            )

        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """SELECT workflow_status,description,
                                  agents_instructions,soul,identity,user_context,
                                  model_ref,model_settings,tool_groups,
                                  payload_schema_version,payload_checksum
                        FROM agent_versions WHERE id=:version"""
                    ),
                    {"version": seed.agent_version_id},
                )
            ).one()
        assert row == (
            "published",
            "Initial description",
            "# Agent",
            "# Soul",
            "# Identity",
            "# User",
            "default",
            {},
            ["research"],
            3,
            "a" * 64,
        )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_published_agent_refs_are_frozen_and_delete_escape_is_exact(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        seed = await _seed_trigger_graph(engine)

        # Draft child rows are replaceable. Once the parent is published, both
        # dependency tables must reject new outbound refs.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO agent_version_skill_refs
                    (agent_version_id,skill_version_id,sort_order)
                    VALUES (:version,:dependency,1)"""
                ),
                {
                    "version": seed.agent_version_id,
                    "dependency": seed.skill_version_ids[1],
                },
            )
            await connection.execute(
                text(
                    """DELETE FROM agent_version_skill_refs
                    WHERE agent_version_id=:version
                      AND skill_version_id=:dependency"""
                ),
                {
                    "version": seed.agent_version_id,
                    "dependency": seed.skill_version_ids[1],
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO agent_version_mcp_refs
                    (agent_version_id,mcp_server_version_id,sort_order)
                    VALUES (:version,:dependency,1)"""
                ),
                {
                    "version": seed.agent_version_id,
                    "dependency": seed.mcp_version_ids[1],
                },
            )
            await connection.execute(
                text(
                    """DELETE FROM agent_version_mcp_refs
                    WHERE agent_version_id=:version
                      AND mcp_server_version_id=:dependency"""
                ),
                {
                    "version": seed.agent_version_id,
                    "dependency": seed.mcp_version_ids[1],
                },
            )
            await connection.execute(
                text(
                    """UPDATE agent_versions SET workflow_status='published'
                    WHERE id=:version"""
                ),
                {"version": seed.agent_version_id},
            )
            await connection.execute(
                text(
                    """UPDATE agents SET current_published_version_id=:version
                    WHERE id=:agent"""
                ),
                {
                    "agent": seed.agent_id,
                    "version": seed.agent_version_id,
                },
            )

        frozen_mutations = (
            (
                """INSERT INTO agent_version_skill_refs
                (agent_version_id,skill_version_id,sort_order)
                VALUES (:version,:dependency,1)""",
                seed.skill_version_ids[1],
                "published version child rows are immutable",
            ),
            (
                """INSERT INTO agent_version_mcp_refs
                (agent_version_id,mcp_server_version_id,sort_order)
                VALUES (:version,:dependency,1)""",
                seed.mcp_version_ids[1],
                "published version child rows are immutable",
            ),
            (
                """UPDATE agent_version_skill_refs SET sort_order=1
                WHERE agent_version_id=:version
                  AND skill_version_id=:dependency""",
                seed.skill_version_ids[0],
                "shared asset version payload is immutable",
            ),
            (
                """UPDATE agent_version_mcp_refs SET sort_order=1
                WHERE agent_version_id=:version
                  AND mcp_server_version_id=:dependency""",
                seed.mcp_version_ids[0],
                "shared asset version payload is immutable",
            ),
            (
                """DELETE FROM agent_version_skill_refs
                WHERE agent_version_id=:version
                  AND skill_version_id=:dependency""",
                seed.skill_version_ids[0],
                "published version child rows are immutable",
            ),
            (
                """DELETE FROM agent_version_mcp_refs
                WHERE agent_version_id=:version
                  AND mcp_server_version_id=:dependency""",
                seed.mcp_version_ids[0],
                "published version child rows are immutable",
            ),
        )
        for statement, dependency_id, message in frozen_mutations:
            with pytest.raises(DBAPIError) as error:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(statement),
                        {
                            "version": seed.agent_version_id,
                            "dependency": dependency_id,
                        },
                    )
            _assert_database_message(error.value, message)

        # An exact GUC alone is insufficient while the Agent remains active
        # and still has its published pointer.
        with pytest.raises(DBAPIError) as error:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """SELECT set_config(
                            'deerflow.agent_hard_delete_asset_id',
                            :asset_id,
                            true
                        )"""
                    ),
                    {"asset_id": str(seed.agent_id)},
                )
                await connection.execute(
                    text(
                        """DELETE FROM agent_version_skill_refs
                        WHERE agent_version_id=:version"""
                    ),
                    {"version": seed.agent_version_id},
                )
        _assert_database_message(
            error.value,
            "published version child rows are immutable",
        )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE agents
                    SET status='archived',current_published_version_id=NULL
                    WHERE id=:agent"""
                ),
                {"agent": seed.agent_id},
            )

        for hard_delete_id in (None, str(uuid.uuid4())):
            with pytest.raises(DBAPIError) as error:
                async with engine.begin() as connection:
                    if hard_delete_id is not None:
                        await connection.execute(
                            text(
                                """SELECT set_config(
                                    'deerflow.agent_hard_delete_asset_id',
                                    :asset_id,
                                    true
                                )"""
                            ),
                            {"asset_id": hard_delete_id},
                        )
                    await connection.execute(
                        text(
                            """DELETE FROM agent_version_skill_refs
                            WHERE agent_version_id=:version"""
                        ),
                        {"version": seed.agent_version_id},
                    )
            _assert_database_message(
                error.value,
                "published version child rows are immutable",
            )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """SELECT set_config(
                        'deerflow.agent_hard_delete_asset_id',
                        :asset_id,
                        true
                    )"""
                ),
                {"asset_id": str(seed.agent_id)},
            )
            await connection.execute(
                text(
                    """DELETE FROM agent_version_skill_refs
                    WHERE agent_version_id=:version"""
                ),
                {"version": seed.agent_version_id},
            )
            await connection.execute(
                text(
                    """DELETE FROM agent_version_mcp_refs
                    WHERE agent_version_id=:version"""
                ),
                {"version": seed.agent_version_id},
            )

        async with engine.connect() as connection:
            skill_ref_count = await connection.scalar(
                text(
                    """SELECT count(*) FROM agent_version_skill_refs
                    WHERE agent_version_id=:version"""
                ),
                {"version": seed.agent_version_id},
            )
            mcp_ref_count = await connection.scalar(
                text(
                    """SELECT count(*) FROM agent_version_mcp_refs
                    WHERE agent_version_id=:version"""
                ),
                {"version": seed.agent_version_id},
            )
        assert skill_ref_count == 0
        assert mcp_ref_count == 0
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_default_agent_first_write_cas_permission_and_composite_fk(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        seed = await _seed_default_agent_matrix(engine, factory)
        service = ProjectDefaultAgentService(factory)

        assert (await service.get(seed.admin)).revision == 0
        with pytest.raises(AssetForbidden):
            await service.replace(
                seed.editor,
                seed.agent_id,
                expected_revision=0,
            )

        results = await asyncio.gather(
            service.replace(
                seed.admin,
                seed.agent_id,
                expected_revision=0,
            ),
            service.replace(
                seed.admin,
                seed.agent_id,
                expected_revision=0,
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, AssetConflict) for result in results) == 1
        selected = await service.get(seed.admin)
        assert selected.agent_asset_id == seed.agent_id
        assert selected.revision == 1

        with pytest.raises(IntegrityError) as cross_project_fk:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """UPDATE project_default_agents
                        SET agent_asset_id=:outsider_agent
                        WHERE project_id=:project"""
                    ),
                    {
                        "outsider_agent": seed.outsider_agent_id,
                        "project": seed.admin.project_id,
                    },
                )
        assert "fk_project_default_agents_project_agent" in str(cross_project_fk.value.orig)

        cleared = await service.replace(
            seed.admin,
            None,
            expected_revision=1,
        )
        assert cleared.agent_asset_id is None
        assert cleared.revision == 2
        assert (await service.get(seed.outsider_admin)).revision == 0
    finally:
        await engine.dispose()
