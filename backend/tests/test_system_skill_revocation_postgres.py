from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@dataclass(frozen=True)
class _RevocationSeed:
    admin_user_id: str
    project_ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
    system_skill_id: uuid.UUID
    system_version_id: uuid.UUID
    project_version_id: uuid.UUID


async def _seed_revocation_graph(engine: AsyncEngine) -> _RevocationSeed:
    admin_user_id = str(uuid.uuid4())
    project_ids = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    system_skill_id = uuid.uuid4()
    project_skill_id = uuid.uuid4()
    system_version_id = uuid.uuid4()
    project_version_id = uuid.uuid4()

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users
                (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'system_admin',now(),false,0)"""
            ),
            {
                "id": admin_user_id,
                "email": f"skill-revocation-{admin_user_id}@example.com",
            },
        )
        await connection.execute(
            text(
                """INSERT INTO projects
                (id,slug,display_name,created_by_user_id)
                VALUES (:id,:slug,:display_name,:owner)"""
            ),
            [
                {
                    "id": project_id,
                    "slug": f"skill-revoke-{index}-{project_id.hex[:8]}",
                    "display_name": f"Skill revocation project {index}",
                    "owner": admin_user_id,
                }
                for index, project_id in enumerate(project_ids, start=1)
            ],
        )
        await connection.execute(
            text(
                """INSERT INTO skills
                (id,scope,project_id,slug,display_name,status,revision,
                 created_by_user_id)
                VALUES (:id,:scope,:project_id,:slug,:display_name,'active',
                        :version,:owner)"""
            ),
            [
                {
                    "id": system_skill_id,
                    "scope": "system",
                    "project_id": None,
                    "slug": f"system-revoke-{system_skill_id.hex[:8]}",
                    "display_name": "System revocation skill",
                    "version": 7,
                    "owner": admin_user_id,
                },
                {
                    "id": project_skill_id,
                    "scope": "project",
                    "project_id": project_ids[0],
                    "slug": f"project-revoke-{project_skill_id.hex[:8]}",
                    "display_name": "Project revocation skill",
                    "version": 1,
                    "owner": admin_user_id,
                },
            ],
        )
        await connection.execute(
            text(
                """INSERT INTO skill_versions
                (id,skill_id,version_number,description,
                 frontmatter,compatibility,secret_requirements,scan_decision,
                scan_summary,payload_checksum,created_by_user_id)
                VALUES (:id,:skill_id,:version_number,
                        :description,jsonb_build_object('name',CAST(:name AS text)),
                        '>=1.0','[]'::jsonb,'allow',
                        jsonb_build_object('decision','allow'),:checksum,:owner)"""
            ),
            [
                {
                    "id": system_version_id,
                    "skill_id": system_skill_id,
                    "version_number": 1,
                    "description": "System Skill Current v1",
                    "name": "system-v1",
                    "checksum": "1" * 64,
                    "owner": admin_user_id,
                },
                {
                    "id": project_version_id,
                    "skill_id": project_skill_id,
                    "version_number": 1,
                    "description": "Project Skill Current v1",
                    "name": "project-v1",
                    "checksum": "6" * 64,
                    "owner": admin_user_id,
                },
            ],
        )
        await connection.execute(
            text(
                """UPDATE skills
                SET current_version_id=:version_id
                WHERE id=:skill_id"""
            ),
            {
                "skill_id": system_skill_id,
                "version_id": system_version_id,
            },
        )
        await connection.execute(
            text(
                "UPDATE skills SET current_version_id=:version_id WHERE id=:skill_id",
            ),
            {
                "version_id": project_version_id,
                "skill_id": project_skill_id,
            },
        )
        await connection.execute(
            text(
                """INSERT INTO project_system_skill_bindings
                (project_id,system_skill_id,enabled,version,
                 created_by_user_id,updated_by_user_id)
                VALUES (:project_id,:skill_id,true,1,:owner,:owner)"""
            ),
            [
                {
                    "project_id": project_ids[0],
                    "skill_id": system_skill_id,
                    "owner": admin_user_id,
                },
                {
                    "project_id": project_ids[1],
                    "skill_id": system_skill_id,
                    "owner": admin_user_id,
                },
            ],
        )
        await connection.execute(
            text(
                """INSERT INTO asset_catalog_state (id,generation)
                VALUES (1,1) ON CONFLICT (id) DO NOTHING"""
            )
        )

    return _RevocationSeed(
        admin_user_id=admin_user_id,
        project_ids=project_ids,
        system_skill_id=system_skill_id,
        system_version_id=system_version_id,
        project_version_id=project_version_id,
    )


async def _expect_database_error(
    engine: AsyncEngine,
    statement: str,
    parameters: dict[str, Any],
    expected_message: str,
) -> None:
    with pytest.raises(DBAPIError) as error:
        async with engine.begin() as connection:
            await connection.execute(text(statement), parameters)
    assert expected_message in str(error.value.orig)


async def _version_payload(engine: AsyncEngine, version_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as connection:
        payload = await connection.scalar(
            text(
                """SELECT to_jsonb(version_row) - ARRAY[
                    'revoked_at','revoked_by_user_id','revocation_reason_code'
                ]::text[]
                FROM skill_versions AS version_row
                WHERE id=:version_id"""
            ),
            {"version_id": version_id},
        )
    assert isinstance(payload, dict)
    return payload


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_system_skill_revocation_and_binding_targets_are_fail_closed(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        seed = await _seed_revocation_graph(engine)
        revoked_version = seed.system_version_id

        before_payload = await _version_payload(engine, revoked_version)
        async with engine.connect() as connection:
            before_generation = await connection.scalar(text("SELECT generation FROM asset_catalog_state WHERE id=1"))
            before_asset = (
                await connection.execute(
                    text(
                        """SELECT current_version_id,revision
                        FROM skills WHERE id=:skill_id"""
                    ),
                    {"skill_id": seed.system_skill_id},
                )
            ).one()

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE skill_versions
                    SET revoked_at=clock_timestamp(),
                        revoked_by_user_id=:actor,
                        revocation_reason_code='security'
                    WHERE id=:version_id"""
                ),
                {
                    "actor": seed.admin_user_id,
                    "version_id": revoked_version,
                },
            )

        async with engine.connect() as connection:
            after_generation = await connection.scalar(text("SELECT generation FROM asset_catalog_state WHERE id=1"))
            after_asset = (
                await connection.execute(
                    text(
                        """SELECT current_version_id,revision
                        FROM skills WHERE id=:skill_id"""
                    ),
                    {"skill_id": seed.system_skill_id},
                )
            ).one()
            revocation = (
                await connection.execute(
                    text(
                        """SELECT revoked_at,revoked_by_user_id,
                                  revocation_reason_code
                        FROM skill_versions WHERE id=:version_id"""
                    ),
                    {"version_id": revoked_version},
                )
            ).one()

        assert before_generation is not None
        assert after_generation == before_generation + 1
        assert revocation.revoked_at is not None
        assert revocation.revoked_by_user_id == seed.admin_user_id
        assert revocation.revocation_reason_code == "security"
        assert await _version_payload(engine, revoked_version) == before_payload
        assert after_asset == before_asset == (revoked_version, 7)

        irreversible_message = "system skill version revocation is irreversible"
        await _expect_database_error(
            engine,
            """UPDATE skill_versions
            SET revoked_at=clock_timestamp(),
                revoked_by_user_id=:actor,
                revocation_reason_code='security'
            WHERE id=:version_id""",
            {"actor": seed.admin_user_id, "version_id": revoked_version},
            irreversible_message,
        )
        await _expect_database_error(
            engine,
            """UPDATE skill_versions
            SET revoked_at=NULL,revoked_by_user_id=NULL,
                revocation_reason_code=NULL
            WHERE id=:version_id""",
            {"version_id": revoked_version},
            irreversible_message,
        )
        await _expect_database_error(
            engine,
            """UPDATE skill_versions SET revocation_reason_code='policy'
            WHERE id=:version_id""",
            {"version_id": revoked_version},
            irreversible_message,
        )
        await _expect_database_error(
            engine,
            """UPDATE skill_versions
            SET revoked_at=clock_timestamp(),
                revoked_by_user_id=:actor,
                revocation_reason_code='security'
            WHERE id=:version_id""",
            {"actor": seed.admin_user_id, "version_id": seed.project_version_id},
            "only a System Skill Current v1 can be revoked",
        )
        await _expect_database_error(
            engine,
            """INSERT INTO skill_versions
            (id,skill_id,version_number,scan_decision,
             payload_checksum,created_by_user_id,revoked_at,
             revoked_by_user_id,revocation_reason_code)
            VALUES (:id,:skill_id,2,'allow',:checksum,:actor,
                    clock_timestamp(),:actor,'integrity')""",
            {
                "id": uuid.uuid4(),
                "skill_id": seed.system_skill_id,
                "checksum": "7" * 64,
                "actor": seed.admin_user_id,
            },
            "skill version must be created unrevoked",
        )

        # An asset-only binding that was valid before revocation remains
        # removable, but it cannot be re-enabled while Current v1 is revoked.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE project_system_skill_bindings
                    SET enabled=false,version=version+1,
                        updated_by_user_id=:actor
                    WHERE project_id=:project_id
                      AND system_skill_id=:skill_id"""
                ),
                {
                    "actor": seed.admin_user_id,
                    "project_id": seed.project_ids[0],
                    "skill_id": seed.system_skill_id,
                },
            )

        binding_error = "system binding requires an eligible Current Version"
        await _expect_database_error(
            engine,
            """UPDATE project_system_skill_bindings
            SET enabled=true,version=version+1,updated_by_user_id=:actor
            WHERE project_id=:project_id AND system_skill_id=:skill_id""",
            {
                "actor": seed.admin_user_id,
                "project_id": seed.project_ids[0],
                "skill_id": seed.system_skill_id,
            },
            binding_error,
        )
        await _expect_database_error(
            engine,
            """INSERT INTO project_system_skill_bindings
            (project_id,system_skill_id,enabled,version,
             created_by_user_id,updated_by_user_id)
            VALUES (:project_id,:skill_id,true,1,:actor,:actor)""",
            {
                "project_id": seed.project_ids[2],
                "skill_id": seed.system_skill_id,
                "actor": seed.admin_user_id,
            },
            binding_error,
        )

        async with engine.connect() as connection:
            first_binding = (
                await connection.execute(
                    text(
                        """SELECT enabled,version
                        FROM project_system_skill_bindings
                        WHERE project_id=:project_id
                          AND system_skill_id=:skill_id"""
                    ),
                    {
                        "project_id": seed.project_ids[0],
                        "skill_id": seed.system_skill_id,
                    },
                )
            ).one()
            second_binding = (
                await connection.execute(
                    text(
                        """SELECT enabled,version
                        FROM project_system_skill_bindings
                        WHERE project_id=:project_id
                          AND system_skill_id=:skill_id"""
                    ),
                    {
                        "project_id": seed.project_ids[1],
                        "skill_id": seed.system_skill_id,
                    },
                )
            ).one()
            third_binding_count = await connection.scalar(
                text(
                    """SELECT count(*) FROM project_system_skill_bindings
                    WHERE project_id=:project_id
                      AND system_skill_id=:skill_id"""
                ),
                {
                    "project_id": seed.project_ids[2],
                    "skill_id": seed.system_skill_id,
                },
            )

        assert first_binding == (False, 2)
        assert second_binding == (True, 1)
        assert third_binding_count == 0
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_system_skill_revocations_serialize_to_one_winner(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        seed = await _seed_revocation_graph(engine)
        second_transaction_started = asyncio.Event()

        async def competing_revocation() -> DBAPIError | None:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                second_transaction_started.set()
                try:
                    await connection.execute(
                        text(
                            """UPDATE skill_versions
                            SET revoked_at=clock_timestamp(),
                                revoked_by_user_id=:actor,
                                revocation_reason_code='policy'
                            WHERE id=:version_id"""
                        ),
                        {
                            "actor": seed.admin_user_id,
                            "version_id": seed.system_version_id,
                        },
                    )
                except DBAPIError as error:
                    await transaction.rollback()
                    return error
                await transaction.commit()
                return None

        async with engine.connect() as first_connection:
            first_transaction = await first_connection.begin()
            await first_connection.execute(
                text(
                    """UPDATE skill_versions
                    SET revoked_at=clock_timestamp(),
                        revoked_by_user_id=:actor,
                        revocation_reason_code='security'
                    WHERE id=:version_id"""
                ),
                {
                    "actor": seed.admin_user_id,
                    "version_id": seed.system_version_id,
                },
            )
            competing_task = asyncio.create_task(competing_revocation())
            await second_transaction_started.wait()
            await first_transaction.commit()

        competing_error = await competing_task
        assert competing_error is not None
        assert "system skill version revocation is irreversible" in str(competing_error.orig)

        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """SELECT revoked_at,revoked_by_user_id,
                                  revocation_reason_code
                        FROM skill_versions WHERE id=:version_id"""
                    ),
                    {"version_id": seed.system_version_id},
                )
            ).one()
        assert row.revoked_at is not None
        assert row.revoked_by_user_id == seed.admin_user_id
        assert row.revocation_reason_code == "security"
    finally:
        await engine.dispose()
