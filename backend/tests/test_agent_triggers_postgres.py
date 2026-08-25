from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.models import AgentModelSettings, AgentPayload


@dataclass(frozen=True, slots=True)
class _Seed:
    project_id: uuid.UUID
    agent_id: uuid.UUID
    definition_id: uuid.UUID
    skill_id: uuid.UUID


async def _seed(engine: AsyncEngine) -> _Seed:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    definition_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    payload = AgentPayload(
        description="Initial Definition",
        agents_instructions="Review carefully.",
        soul="Be precise.",
        identity="Reviewer",
        user_context="Use Chinese.",
        model_ref="default",
        model_settings=AgentModelSettings(),
        tool_groups=(),
        skill_refs=(),
        mcp_version_ids=(),
        payload_schema_version=4,
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users
                (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'user',now(),false,0)"""
            ),
            {"id": str(user_id), "email": f"agent-definition-{user_id}@example.test"},
        )
        await connection.execute(
            text(
                """INSERT INTO projects
                (id,slug,display_name,created_by_user_id)
                VALUES (:id,:slug,'Agent Definition trigger',:owner)"""
            ),
            {
                "id": project_id,
                "slug": f"agent-definition-{project_id.hex[:12]}",
                "owner": str(user_id),
            },
        )
        await connection.execute(
            text(
                """INSERT INTO agents
                (id,scope,project_id,slug,display_name,status,definition_id,
                 description,agents_instructions,soul,identity,user_context,
                 model_ref,model_settings,tool_groups,payload_schema_version,
                 payload_checksum,revision,created_by_user_id,updated_by_user_id)
                VALUES
                (:id,'project',:project,'definition-agent','Definition Agent',
                 'active',:definition_id,:description,:instructions,:soul,
                 :identity,:user_context,'default','{}'::jsonb,'[]'::jsonb,4,
                 :checksum,1,:owner,:owner)"""
            ),
            {
                "id": agent_id,
                "project": project_id,
                "definition_id": definition_id,
                "description": payload.description,
                "instructions": payload.agents_instructions,
                "soul": payload.soul,
                "identity": payload.identity,
                "user_context": payload.user_context,
                "checksum": agent_payload_checksum(payload),
                "owner": str(user_id),
            },
        )
        await connection.execute(
            text(
                """INSERT INTO skills
                (id,scope,project_id,slug,display_name,status,created_by_user_id)
                VALUES
                (:id,'project',:project,'definition-skill','Definition Skill',
                 'active',:owner)"""
            ),
            {"id": skill_id, "project": project_id, "owner": str(user_id)},
        )
    return _Seed(project_id, agent_id, definition_id, skill_id)


def _assert_database_message(error: DBAPIError, expected: str) -> None:
    assert expected in str(error.orig)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_agent_definition_requires_fence_rotation_and_one_revision(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        seed = await _seed(engine)
        replacement_id = uuid.uuid4()

        with pytest.raises(DBAPIError) as missing_fence:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """UPDATE agents
                        SET definition_id=:definition_id,description='Changed',
                            revision=revision+1,payload_checksum=:checksum
                        WHERE id=:agent_id"""
                    ),
                    {
                        "agent_id": seed.agent_id,
                        "definition_id": replacement_id,
                        "checksum": "b" * 64,
                    },
                )
        _assert_database_message(missing_fence.value, "requires its transaction fence")

        with pytest.raises(DBAPIError) as unchanged_identity:
            async with engine.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('deerflow.agent_definition_mutation_id',:agent_id,true)"),
                    {"agent_id": str(seed.agent_id)},
                )
                await connection.execute(
                    text(
                        """UPDATE agents
                        SET description='Changed',revision=revision+1,
                            payload_checksum=:checksum
                        WHERE id=:agent_id"""
                    ),
                    {"agent_id": seed.agent_id, "checksum": "b" * 64},
                )
        _assert_database_message(unchanged_identity.value, "requires its transaction fence")

        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('deerflow.agent_definition_mutation_id',:agent_id,true)"),
                {"agent_id": str(seed.agent_id)},
            )
            await connection.execute(
                text(
                    """UPDATE agents
                    SET definition_id=:definition_id,description='Changed',
                        revision=revision+1,payload_checksum=:checksum
                    WHERE id=:agent_id"""
                ),
                {
                    "agent_id": seed.agent_id,
                    "definition_id": replacement_id,
                    "checksum": "b" * 64,
                },
            )
            row = (
                await connection.execute(
                    text("SELECT definition_id,revision,status FROM agents WHERE id=:agent_id"),
                    {"agent_id": seed.agent_id},
                )
            ).one()

        assert tuple(row) == (replacement_id, 2, "active")
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_agent_direct_references_require_exact_definition_fence(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        seed = await _seed(engine)

        async with engine.begin() as connection:
            precondition = (
                await connection.execute(
                    text(
                        """SELECT project.status,project.deletion_effective_at,
                                  current_setting(
                                      'deerflow.agent_definition_mutation_id',true
                                  ),
                                  current_setting(
                                      'deerflow.agent_hard_delete_asset_id',true
                                  )
                             FROM projects project
                            WHERE project.id=:project_id"""
                    ),
                    {"project_id": seed.project_id},
                )
            ).one()
        assert tuple(precondition) == ("active", None, None, None)

        with pytest.raises(DBAPIError) as missing_fence:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO agent_skill_refs
                        (agent_id,skill_asset_scope,skill_asset_id,sort_order)
                        VALUES (:agent_id,'project',:skill_id,0)"""
                    ),
                    {"agent_id": seed.agent_id, "skill_id": seed.skill_id},
                )
        _assert_database_message(missing_fence.value, "requires its transaction fence")

        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('deerflow.agent_definition_mutation_id',:agent_id,true)"),
                {"agent_id": str(seed.agent_id)},
            )
            await connection.execute(
                text(
                    """INSERT INTO agent_skill_refs
                    (agent_id,skill_asset_scope,skill_asset_id,sort_order)
                    VALUES (:agent_id,'project',:skill_id,0)"""
                ),
                {"agent_id": seed.agent_id, "skill_id": seed.skill_id},
            )

        with pytest.raises(DBAPIError) as delete_without_fence:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM agent_skill_refs WHERE agent_id=:agent_id"),
                    {"agent_id": seed.agent_id},
                )
        _assert_database_message(delete_without_fence.value, "requires its transaction fence")
    finally:
        await engine.dispose()
