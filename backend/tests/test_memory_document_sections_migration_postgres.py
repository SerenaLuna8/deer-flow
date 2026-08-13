from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from postgres_utils import temporary_postgres_database
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.system_runtime_settings.models import (
    RuntimePolicySection,
    default_policy_value,
)
from app.system_runtime_settings.validation import canonical_policy_payload
from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION
from scripts.upgrade_postgres import upgrade_postgres

BACKEND_ROOT = Path(__file__).resolve().parents[1]
BASELINE_SNAPSHOT_PATH = BACKEND_ROOT / "migrations" / "baseline" / "full_schema_v5.sql"
MIGRATIONS_PATH = BACKEND_ROOT / "migrations"
POLICY_VERSION_NAMESPACE = uuid.UUID("e80287de-83d9-5d3a-a4c8-df0eeaa2a955")
LEGACY_SECTIONS = [
    "用户偏好与协作方式",
    "项目背景",
    "长期约束与架构决策",
    "当前仍有效的目标",
]

pytestmark = pytest.mark.postgres


def _baseline_sql() -> str:
    lines = BASELINE_SNAPSHOT_PATH.read_text(encoding="utf-8").splitlines(
        keepends=True,
    )
    for index, line in enumerate(lines):
        if not line.startswith("--"):
            return "".join(lines[index:])
    raise AssertionError("baseline snapshot contains no SQL body")


async def _execute_sql_batch(database_url: str, payload: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            raw_connection = await connection.get_raw_connection()
            await raw_connection.driver_connection.execute(payload)
    finally:
        await engine.dispose()


def _upgrade_sync(database_url: str, revision: str) -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    config.attributes["sqlalchemy_url"] = database_url
    command.upgrade(config, revision)


@pytest.mark.asyncio
async def test_v8_to_v9_backfills_frozen_sections_and_policy_provenance(
    postgres_admin_url: str,
) -> None:
    actor_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    thread_id = f"memory-sections-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    content = "\n\n".join(f"# {title}" for title in LEGACY_SECTIONS) + "\n\nlegacy body"
    content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

    async with temporary_postgres_database(postgres_admin_url) as database_url:
        await _execute_sql_batch(database_url, _baseline_sql())
        await asyncio.to_thread(_upgrade_sync, database_url, "full_schema_v8")
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO users
                        (id,email,system_role,created_at,needs_setup,token_version)
                        VALUES (:id,:email,'system_admin',now(),false,0)"""
                    ),
                    {"id": actor_id, "email": f"{actor_id}@example.com"},
                )
                for section in (
                    RuntimePolicySection.AGENT_RUNTIME,
                    RuntimePolicySection.AUTH,
                    RuntimePolicySection.QUOTAS,
                ):
                    version_id = uuid.uuid5(
                        POLICY_VERSION_NAMESPACE,
                        f"{section.value}:version:1",
                    )
                    canonical = canonical_policy_payload(
                        section,
                        default_policy_value(section),
                    )
                    await connection.execute(
                        text(
                            """INSERT INTO system_runtime_policies
                            (section,current_version_id,revision,updated_by_user_id)
                            VALUES (:section,:version_id,1,:actor)"""
                        ),
                        {
                            "section": section.value,
                            "version_id": version_id,
                            "actor": actor_id,
                        },
                    )
                    await connection.execute(
                        text(
                            """INSERT INTO system_runtime_policy_versions
                            (id,section,version_number,schema_version,value,
                             payload_checksum,created_by_user_id)
                            VALUES (:id,:section,1,:schema_version,
                                    CAST(:value AS jsonb),:checksum,:actor)"""
                        ),
                        {
                            "id": version_id,
                            "section": section.value,
                            "schema_version": canonical.schema_version,
                            "value": json.dumps(
                                canonical.value,
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ),
                            "checksum": canonical.checksum,
                            "actor": actor_id,
                        },
                    )

                await connection.execute(
                    text(
                        """INSERT INTO projects
                        (id,slug,display_name,created_by_user_id)
                        VALUES (:id,:slug,'Memory Sections',:owner)"""
                    ),
                    {
                        "id": project_id,
                        "slug": f"memory-sections-{project_id.hex[:12]}",
                        "owner": actor_id,
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
                        "user": actor_id,
                    },
                )
                await connection.execute(
                    text(
                        """INSERT INTO agents
                        (id,scope,project_id,slug,display_name,status,version,
                         created_by_user_id)
                        VALUES (:id,'project',:project,'memory-sections-agent',
                                'Memory Sections Agent','active',1,:user)"""
                    ),
                    {"id": agent_id, "project": project_id, "user": actor_id},
                )
                await connection.execute(
                    text(
                        """INSERT INTO threads_meta
                        (thread_id,owner_user_id,status,metadata_json,created_at,
                         updated_at,project_id,agent_asset_id,agent_scope)
                        VALUES (:thread,:user,'idle','{}',now(),now(),:project,
                                :agent,'project')"""
                    ),
                    {
                        "thread": thread_id,
                        "user": actor_id,
                        "project": project_id,
                        "agent": agent_id,
                    },
                )
                await connection.execute(
                    text(
                        """INSERT INTO runs
                        (run_id,thread_id,owner_user_id,status,multitask_strategy,
                         metadata_json,kwargs_json,origin_trace_id,message_count,
                         total_input_tokens,total_output_tokens,total_tokens,
                         llm_call_count,lead_agent_tokens,subagent_tokens,
                         middleware_tokens,created_at,updated_at,project_id)
                        VALUES (:run,:thread,:user,'pending','reject','{}','{}',
                                :trace,0,0,0,0,0,0,0,0,now(),now(),:project)"""
                    ),
                    {
                        "run": run_id,
                        "thread": thread_id,
                        "user": actor_id,
                        "trace": str(uuid.uuid4()),
                        "project": project_id,
                    },
                )
                await connection.execute(
                    text(
                        """INSERT INTO memory_documents
                        (project_id,owner_user_id,namespace,content,content_digest,
                         version,dream_cursor)
                        VALUES (:project,:owner,'project-owner',:content,:digest,7,3)"""
                    ),
                    {
                        "project": project_id,
                        "owner": actor_id,
                        "content": content,
                        "digest": content_digest,
                    },
                )
                await connection.execute(
                    text(
                        """INSERT INTO run_memory_context_snapshots
                        (project_id,owner_user_id,run_id,namespace,
                         document_version,content,content_digest)
                        VALUES (:project,:owner,:run,'project-owner',7,:content,:digest)"""
                    ),
                    {
                        "project": project_id,
                        "owner": actor_id,
                        "run": run_id,
                        "content": content,
                        "digest": content_digest,
                    },
                )
        finally:
            await engine.dispose()

        result = await upgrade_postgres(database_url, assume_yes=True)
        assert result.from_revision == "full_schema_v8"
        assert result.to_revision == CURRENT_SCHEMA_REVISION == "full_schema_v17"

        upgraded = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with upgraded.connect() as connection:
                document = (
                    await connection.execute(
                        text(
                            """SELECT content,content_digest,version,dream_cursor,
                                      sections,sections_policy_section,
                                      sections_policy_version_id
                                 FROM memory_documents
                                WHERE project_id=:project
                                  AND owner_user_id=:owner
                                  AND namespace='project-owner'"""
                        ),
                        {"project": project_id, "owner": actor_id},
                    )
                ).one()
                assert document.content == content
                assert document.content_digest == content_digest
                assert document.version == 7
                assert document.dream_cursor == 3
                assert document.sections == LEGACY_SECTIONS
                assert document.sections_policy_section == "memory_document"

                policy = (
                    await connection.execute(
                        text(
                            """SELECT policy.section,policy.revision,version.value,
                                      version.version_number,version.schema_version,
                                      version.payload_checksum
                                 FROM system_runtime_policies AS policy
                                 JOIN system_runtime_policy_versions AS version
                                   ON version.section=policy.section
                                  AND version.id=policy.current_version_id
                                WHERE policy.section='memory_document'
                                  AND version.id=:version_id"""
                        ),
                        {"version_id": document.sections_policy_version_id},
                    )
                ).one()
                assert policy.section == "memory_document"
                assert policy.revision == policy.version_number == 1
                assert policy.value == {"sections": LEGACY_SECTIONS}
                expected_policy = canonical_policy_payload(
                    RuntimePolicySection.MEMORY_DOCUMENT,
                    default_policy_value(RuntimePolicySection.MEMORY_DOCUMENT),
                )
                assert policy.schema_version == expected_policy.schema_version
                assert policy.payload_checksum == expected_policy.checksum
                assert await connection.scalar(text("SELECT count(*) FROM system_runtime_policies")) == 5
                automations = (
                    await connection.execute(
                        text(
                            """SELECT policy.revision, version.value, version.payload_checksum
                                 FROM system_runtime_policies AS policy
                                 JOIN system_runtime_policy_versions AS version
                                   ON version.section=policy.section
                                  AND version.id=policy.current_version_id
                                WHERE policy.section='automations'"""
                        ),
                    )
                ).one()
                expected_automations = canonical_policy_payload(
                    RuntimePolicySection.AUTOMATIONS,
                    default_policy_value(RuntimePolicySection.AUTOMATIONS),
                )
                assert automations.revision == 1
                assert automations.value == expected_automations.value
                assert automations.payload_checksum == expected_automations.checksum
                assert (
                    await connection.scalar(
                        text(
                            """SELECT sections
                                 FROM run_memory_context_snapshots
                                WHERE project_id=:project
                                  AND owner_user_id=:owner
                                  AND run_id=:run"""
                        ),
                        {
                            "project": project_id,
                            "owner": actor_id,
                            "run": run_id,
                        },
                    )
                    == LEGACY_SECTIONS
                )

            with pytest.raises(DBAPIError) as invalid_element:
                async with upgraded.begin() as connection:
                    await connection.execute(
                        text(
                            """INSERT INTO memory_documents
                            (project_id,owner_user_id,namespace,content,
                             content_digest,sections,sections_policy_version_id)
                            VALUES (:project,:owner,'invalid-sections',:content,
                                    :digest,'[1,2]'::jsonb,:policy_version)"""
                        ),
                        {
                            "project": project_id,
                            "owner": actor_id,
                            "content": content,
                            "digest": content_digest,
                            "policy_version": document.sections_policy_version_id,
                        },
                    )
            assert invalid_element.value.orig.sqlstate == "23514"

            with pytest.raises(DBAPIError) as document_mutation:
                async with upgraded.begin() as connection:
                    await connection.execute(
                        text(
                            """UPDATE memory_documents
                                  SET sections='["Changed","Second"]'::jsonb
                                WHERE project_id=:project
                                  AND owner_user_id=:owner
                                  AND namespace='project-owner'"""
                        ),
                        {"project": project_id, "owner": actor_id},
                    )
            assert document_mutation.value.orig.sqlstate == "55000"

            with pytest.raises(DBAPIError) as snapshot_mutation:
                async with upgraded.begin() as connection:
                    await connection.execute(
                        text(
                            """UPDATE run_memory_context_snapshots
                                  SET sections='["Changed","Second"]'::jsonb
                                WHERE project_id=:project
                                  AND owner_user_id=:owner
                                  AND run_id=:run"""
                        ),
                        {
                            "project": project_id,
                            "owner": actor_id,
                            "run": run_id,
                        },
                    )
            assert snapshot_mutation.value.orig.sqlstate == "55000"
        finally:
            await upgraded.dispose()
