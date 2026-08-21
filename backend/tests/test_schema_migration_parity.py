"""正式数据库迁移链契约：单 head、fresh catalog、已知祖先升级闸门。

空库新装走 ``full_schema.sql`` 并直接 stamp 当前 head。正式
``initial_schema`` 数据库通过显式迁移加入审批输出交付义务表，并移除
系统模型目录中已废弃的说明与手工排序字段。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateIndex

import deerflow.persistence.bootstrap as bootstrap_module
from app.shared_assets.skill_credential_closure import (
    AdmittedSkillCredentialReference,
    SkillCredentialClosureInvalid,
    lock_admitted_skill_credential_materials,
)
from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import (
    CURRENT_SCHEMA_REVISION,
    KNOWN_CHAIN_REVISIONS,
    M7RecreateRequired,
    SchemaUpgradeRequired,
    bootstrap_schema,
    classify_database,
    validate_schema,
)
from deerflow.persistence.final_schema_contract import (
    FINAL_M7_CATALOG_SIGNATURE,
    read_m7_catalog_signature,
)
from scripts import upgrade_postgres as upgrade_module
from scripts.upgrade_postgres import PostgresUpgradeError, upgrade_postgres

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_PATH = BACKEND_ROOT / "migrations"
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"

pytestmark = pytest.mark.postgres


def _canonical_json_checksum(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
    ).hexdigest()


def _script_directory():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    return ScriptDirectory.from_config(config)


def test_known_chain_revisions_pin_the_actual_migration_scripts() -> None:
    script = _script_directory()
    walked = tuple(revision.revision for revision in script.walk_revisions("base", "heads"))
    root_to_head = tuple(reversed(walked))
    assert (
        root_to_head
        == KNOWN_CHAIN_REVISIONS
        == (
            "initial_schema",
            "approval_output_delivery",
            "model_catalog_simplify",
            "agent_design_resume_index",
            "agent_archived_slug_reuse",
            "skill_credential_source_field",
            "current_asset_version_lifecycle",
            "agent_design_activity",
            "agent_design_activity_retry",
            "agent_design_activity_terminal",
        )
    )
    assert script.get_heads() == [CURRENT_SCHEMA_REVISION]


def test_setup_and_upgrade_share_the_schema_mutation_advisory_lock() -> None:
    assert upgrade_module._UPGRADE_LOCK_KEY == bootstrap_module.SCHEMA_MUTATION_LOCK_KEY


def test_initial_chain_root_is_a_noop_and_fresh_schema_stamps_the_head() -> None:
    script = _script_directory()
    root = script.get_revision(KNOWN_CHAIN_REVISIONS[0])
    assert root.down_revision is None
    payload = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    assert payload.count(f"INSERT INTO alembic_version (version_num) VALUES ('{CURRENT_SCHEMA_REVISION}');") == 1
    assert CURRENT_SCHEMA_REVISION == "agent_design_activity_terminal"


def test_full_schema_is_the_only_install_snapshot() -> None:
    payload = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    assert payload.startswith("BEGIN;\n")
    assert payload.count(f"INSERT INTO alembic_version (version_num) VALUES ('{CURRENT_SCHEMA_REVISION}');") == 1
    assert not list((MIGRATIONS_PATH / "baseline").glob("*.sql"))
    assert sorted(path.name for path in (MIGRATIONS_PATH / "versions").glob("*.py")) == [
        "agent_archived_slug_reuse.py",
        "agent_design_activity.py",
        "agent_design_activity_retry.py",
        "agent_design_activity_terminal.py",
        "agent_design_resume_index.py",
        "approval_output_delivery.py",
        "current_asset_version_lifecycle.py",
        "initial_schema.py",
        "model_catalog_simplify.py",
        "skill_credential_source_field.py",
    ]


def test_agent_design_resume_index_matches_immutable_keyset_contract() -> None:
    index = next(candidate for candidate in Base.metadata.tables["agent_design_sessions"].indexes if candidate.name == "ix_agent_design_sessions_resume")
    expected = "CREATE INDEX ix_agent_design_sessions_resume ON agent_design_sessions (project_id, owner_user_id, created_at DESC, id DESC) WHERE status NOT IN ('completed', 'cancelled')"

    compiled = " ".join(
        str(CreateIndex(index).compile(dialect=postgresql.dialect())).split(),
    )
    full_schema = " ".join(FULL_SCHEMA_PATH.read_text(encoding="utf-8").split())

    assert compiled == expected
    assert expected in full_schema


def test_project_agent_slug_index_excludes_archived_rows() -> None:
    index = next(candidate for candidate in Base.metadata.tables["agents"].indexes if candidate.name == "uq_agents_project_slug")
    expected = "CREATE UNIQUE INDEX uq_agents_project_slug ON agents (project_id, lower(slug)) WHERE scope = 'project' AND status != 'archived'"

    compiled = " ".join(
        str(CreateIndex(index).compile(dialect=postgresql.dialect())).split(),
    )
    full_schema = " ".join(FULL_SCHEMA_PATH.read_text(encoding="utf-8").split())

    assert compiled == expected
    assert expected in full_schema


async def _catalog_signature(database_url: str):
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            state = await classify_database(connection)
            signature = await read_m7_catalog_signature(connection)
        return state, signature
    finally:
        await engine.dispose()


async def _restore_pre_agent_design_activity_schema(connection) -> None:
    await connection.execute(text("DROP TABLE agent_design_activities"))
    await connection.execute(
        text(
            """ALTER TABLE agent_design_sessions
               DROP CONSTRAINT ck_agent_design_sessions_generation_preference,
               DROP COLUMN generation_model_ref,
               DROP COLUMN generation_mode""",
        ),
    )
    await connection.execute(
        text(
            """ALTER TABLE agent_design_operations
               DROP CONSTRAINT ck_agent_design_operations_generation_profile,
               DROP CONSTRAINT uq_agent_design_operations_private_scope,
               DROP CONSTRAINT ck_agent_design_operations_status,
               DROP CONSTRAINT ck_agent_design_operations_completion,
               DROP COLUMN stop_requested_at,
               DROP COLUMN requested_generation_profile_json,
               DROP COLUMN effective_generation_profile_json,
               ADD CONSTRAINT ck_agent_design_operations_status
                   CHECK (status IN ('in_progress', 'completed', 'failed')),
               ADD CONSTRAINT ck_agent_design_operations_completion CHECK (
                   (status = 'in_progress' AND result_revision IS NULL AND public_error_code IS NULL)
                   OR (status = 'completed' AND result_revision IS NOT NULL AND public_error_code IS NULL)
                   OR (status = 'failed' AND result_revision IS NOT NULL AND public_error_code IS NOT NULL)
               )""",
        ),
    )


@pytest.mark.asyncio
async def test_agent_design_activity_migration_matches_fresh_schema(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await _restore_pre_agent_design_activity_schema(connection)
            await connection.execute(
                text(
                    "UPDATE alembic_version SET version_num = 'current_asset_version_lifecycle'",
                ),
            )
    finally:
        await engine.dispose()

    result = await upgrade_postgres(postgres_database_url)
    assert result.applied is True
    assert result.from_revision == "current_asset_version_lifecycle"
    assert result.to_revision == CURRENT_SCHEMA_REVISION
    state, signature = await _catalog_signature(postgres_database_url)
    assert state == "current"
    assert signature == FINAL_M7_CATALOG_SIGNATURE


async def _restore_pre_model_catalog_schema(connection) -> None:
    """Recreate the fields and indexes present at the preceding revision."""

    await connection.execute(
        text("DROP INDEX ix_system_model_configs_status_created"),
    )
    await connection.execute(
        text(
            """ALTER TABLE system_model_configs
               ADD COLUMN logical_name VARCHAR(128) NOT NULL,
               ADD COLUMN description TEXT DEFAULT '' NOT NULL,
               ADD COLUMN sort_order BIGINT DEFAULT 0 NOT NULL,
               ADD CONSTRAINT ck_system_model_configs_sort_order
                   CHECK (sort_order >= 0)""",
        ),
    )
    await connection.execute(
        text(
            """CREATE INDEX ix_system_model_configs_status_order
               ON system_model_configs (status, sort_order, id)""",
        ),
    )
    await connection.execute(
        text(
            """CREATE UNIQUE INDEX uq_system_model_configs_logical_name
               ON system_model_configs (lower(logical_name))""",
        ),
    )
    await connection.execute(
        text(
            """ALTER TABLE run_model_config_snapshots
               ADD COLUMN logical_name VARCHAR(128) NOT NULL""",
        ),
    )
    await connection.execute(
        text(
            """COMMENT ON TABLE system_model_configs IS
               '保存系统模型配置的逻辑身份和当前版本指针。'""",
        ),
    )


async def _restore_pre_current_asset_lifecycle_schema(connection) -> None:
    """Recreate the lifecycle catalog owned by the preceding released head."""

    statements = (
        "DROP TRIGGER trg_project_skill_credential_bindings_live_target ON project_skill_credential_bindings",
        "DROP TRIGGER trg_project_skill_credential_configs_live_binding ON project_skill_credential_configs",
        "DROP TRIGGER trg_skill_versions_live_credential_binding ON skill_versions",
        "DROP FUNCTION enforce_live_skill_credential_binding_target()",
        "DROP FUNCTION protect_live_skill_credential_binding_target()",
        "DROP INDEX uq_project_skill_credential_bindings_active_name",
        "ALTER TABLE project_skill_credential_bindings DROP CONSTRAINT fk_project_skill_credential_bindings_runtime_authority",
        "ALTER TABLE project_skill_credential_bindings DROP CONSTRAINT ck_project_skill_credential_bindings_runtime_authority",
        "ALTER TABLE project_skill_credential_bindings DROP CONSTRAINT fk_project_skill_credential_bindings_skill",
        "ALTER TABLE project_skill_credential_bindings DROP COLUMN runtime_authority_binding_id",
        "ALTER TABLE project_skill_credential_bindings DROP COLUMN admission_only",
        """ALTER TABLE project_skill_credential_bindings
           ADD CONSTRAINT fk_project_skill_credential_bindings_config
           FOREIGN KEY(project_id,skill_id,skill_version_id)
           REFERENCES project_skill_credential_configs(project_id,skill_id,skill_version_id)
           ON DELETE CASCADE""",
        "ALTER TABLE project_skill_credential_bindings ADD CONSTRAINT fk_project_skill_credential_bindings_skill_version FOREIGN KEY(skill_id,skill_version_id) REFERENCES skill_versions(skill_id,id) ON DELETE CASCADE",
        "CREATE UNIQUE INDEX uq_project_skill_credential_bindings_active_name ON project_skill_credential_bindings(project_id,skill_id,skill_version_id,secret_name) WHERE status='active'",
        "ALTER TABLE run_asset_versions DROP COLUMN snapshot_json",
        "DROP TABLE system_asset_upgrade_audit",
        "ALTER TABLE project_system_agent_bindings ADD COLUMN agent_version_id UUID NOT NULL",
        "ALTER TABLE project_system_agent_bindings ADD CONSTRAINT fk_project_system_agent_bindings_version FOREIGN KEY(system_agent_id,agent_version_id) REFERENCES agent_versions(agent_id,id) ON DELETE RESTRICT",
        "ALTER TABLE project_system_skill_bindings ADD COLUMN skill_version_id UUID NOT NULL",
        "ALTER TABLE project_system_skill_bindings ADD CONSTRAINT fk_project_system_skill_bindings_version FOREIGN KEY(system_skill_id,skill_version_id) REFERENCES skill_versions(skill_id,id) ON DELETE RESTRICT",
        "ALTER TABLE agent_version_skill_refs ADD COLUMN skill_version_id UUID NOT NULL",
        "ALTER TABLE agent_version_skill_refs DROP CONSTRAINT fk_agent_version_skill_refs_skill_asset",
        "ALTER TABLE agent_version_skill_refs DROP CONSTRAINT ck_agent_version_skill_refs_scope",
        "ALTER TABLE agent_version_skill_refs DROP CONSTRAINT agent_version_skill_refs_pkey",
        "ALTER TABLE agent_version_skill_refs DROP COLUMN skill_asset_scope",
        "ALTER TABLE agent_version_skill_refs DROP COLUMN skill_asset_id",
        "ALTER TABLE agent_version_skill_refs ADD CONSTRAINT agent_version_skill_refs_pkey PRIMARY KEY(agent_version_id,skill_version_id)",
        "ALTER TABLE agent_version_skill_refs ADD CONSTRAINT agent_version_skill_refs_skill_version_id_fkey FOREIGN KEY(skill_version_id) REFERENCES skill_versions(id) ON DELETE RESTRICT",
        "ALTER TABLE agents DROP CONSTRAINT fk_agents_current_version",
        "ALTER TABLE agents DROP CONSTRAINT ck_agents_revision",
        "ALTER TABLE agents RENAME COLUMN current_version_id TO current_published_version_id",
        "ALTER TABLE agents RENAME COLUMN revision TO version",
        "ALTER TABLE agents ADD CONSTRAINT ck_agents_version CHECK(version >= 1)",
        "ALTER TABLE agents ADD CONSTRAINT fk_agents_current_published_version FOREIGN KEY(id,current_published_version_id) REFERENCES agent_versions(agent_id,id)",
        "ALTER TABLE skills DROP CONSTRAINT fk_skills_current_version",
        "ALTER TABLE skills DROP CONSTRAINT ck_skills_revision",
        "ALTER TABLE skills RENAME COLUMN current_version_id TO current_published_version_id",
        "ALTER TABLE skills RENAME COLUMN revision TO version",
        "ALTER TABLE skills ADD CONSTRAINT ck_skills_version CHECK(version >= 1)",
        "ALTER TABLE skills ADD CONSTRAINT fk_skills_current_published_version FOREIGN KEY(id,current_published_version_id) REFERENCES skill_versions(skill_id,id)",
        "ALTER TABLE agent_versions ADD COLUMN workflow_status VARCHAR(24) DEFAULT 'draft' NOT NULL",
        "ALTER TABLE agent_versions ADD COLUMN submitted_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE agent_versions ADD COLUMN reviewed_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE agent_versions ADD COLUMN reviewed_by_user_id VARCHAR(36)",
        "ALTER TABLE agent_versions ADD COLUMN review_note TEXT",
        "ALTER TABLE agent_versions ADD CONSTRAINT agent_versions_reviewed_by_user_id_fkey FOREIGN KEY(reviewed_by_user_id) REFERENCES users(id)",
        "ALTER TABLE agent_versions ADD CONSTRAINT ck_agent_versions_workflow_status CHECK(workflow_status IN ('draft','pending_approval','published','rejected'))",
        "ALTER TABLE skill_versions ADD COLUMN workflow_status VARCHAR(24) DEFAULT 'draft' NOT NULL",
        "ALTER TABLE skill_versions ADD COLUMN submitted_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE skill_versions ADD COLUMN reviewed_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE skill_versions ADD COLUMN reviewed_by_user_id VARCHAR(36)",
        "ALTER TABLE skill_versions ADD COLUMN review_note TEXT",
        "ALTER TABLE skill_versions ADD CONSTRAINT skill_versions_reviewed_by_user_id_fkey FOREIGN KEY(reviewed_by_user_id) REFERENCES users(id)",
        "ALTER TABLE skill_versions ADD CONSTRAINT ck_skill_versions_workflow_status CHECK(workflow_status IN ('draft','pending_approval','published','rejected'))",
    )
    for statement in statements:
        await connection.execute(text(statement))


async def _restore_pre_skill_credential_source_schema(connection) -> None:
    """Recreate the catalog present before the two latest released heads."""

    await _restore_pre_current_asset_lifecycle_schema(connection)

    await connection.execute(
        text(
            "ALTER TABLE project_skill_credential_bindings DROP COLUMN source_env_field_name",
        ),
    )
    await connection.execute(
        text(
            "ALTER TABLE run_skill_credential_snapshots DROP COLUMN source_env_field_name",
        ),
    )


async def _seed_pre_model_catalog_references(
    connection,
    *,
    runtime_policy_model_ref: str | None = None,
) -> dict[str, object]:
    user_id = "10000000-0000-0000-0000-000000000001"
    project_id = uuid.UUID("10000000-0000-0000-0000-000000000002")
    membership_id = uuid.UUID("10000000-0000-0000-0000-000000000003")
    agent_id = uuid.UUID("10000000-0000-0000-0000-000000000004")
    agent_version_id = uuid.UUID("10000000-0000-0000-0000-000000000005")
    design_session_id = uuid.UUID("10000000-0000-0000-0000-000000000006")
    design_thread_id = uuid.UUID("10000000-0000-0000-0000-000000000007")
    model_config_id = uuid.UUID("10000000-0000-0000-0000-000000000008")
    model_version_id = uuid.UUID("10000000-0000-0000-0000-000000000009")
    runtime_policy_version_id = uuid.UUID(
        "10000000-0000-0000-0000-00000000000a",
    )
    logical_name = "migration-model"
    thread_id = "migration-thread"
    run_id = "migration-run"
    model_payload_checksum = "a" * 64

    agent_payload = {
        "agents_instructions": "agent instructions",
        "description": "migration agent",
        "identity": "identity",
        "mcp_version_ids": [],
        "model_ref": logical_name,
        "model_settings": {},
        "skill_version_ids": [],
        "soul": "soul",
        "tool_groups": [],
        "user_context": "user context",
    }
    agent_payload_checksum = _canonical_json_checksum(agent_payload)
    blueprint = {
        "agents_instructions": "agent instructions",
        "description": "migration blueprint",
        "identity": "identity",
        "mcp_version_ids": [],
        "model_ref": logical_name,
        "skill_version_ids": [],
        "soul": "soul",
        "tool_groups": [],
        "user_context": "user context",
    }
    blueprint_checksum = _canonical_json_checksum(blueprint)
    persisted_runtime_model_ref = runtime_policy_model_ref or logical_name
    runtime_policy = {
        "input_polish": {"model_name": logical_name},
        "memory": {"model_name": persisted_runtime_model_ref},
        "summarization": {"model_name": logical_name},
        "title": {"model_name": None},
        "vision_bridge": {"model_name": logical_name},
    }
    runtime_policy_checksum = _canonical_json_checksum(runtime_policy)
    run_kwargs = {
        "__run_execution_profile": {
            "effective": {
                "model_name": logical_name,
                "reasoning_effort": None,
                "supports_vision": True,
                "thinking_enabled": False,
            },
            "requested": {
                "model_name": logical_name,
                "reasoning_effort": None,
                "thinking_enabled": None,
            },
        },
    }

    await connection.execute(
        text(
            """INSERT INTO users
               (id,email,username,password_hash,system_role,created_at,
                oauth_provider,oauth_id,needs_setup,token_version)
               VALUES
               (:id,'migration@example.invalid','migration_user',NULL,
                'user',now(),NULL,NULL,false,0)""",
        ),
        {"id": user_id},
    )
    await connection.execute(
        text(
            """INSERT INTO projects
               (id,slug,display_name,created_by_user_id)
               VALUES (:id,'migration-project','Migration Project',:user_id)""",
        ),
        {"id": project_id, "user_id": user_id},
    )
    await connection.execute(
        text(
            """INSERT INTO project_memberships
               (id,project_id,user_id,role,status)
               VALUES (:id,:project_id,:user_id,'admin','active')""",
        ),
        {
            "id": membership_id,
            "project_id": project_id,
            "user_id": user_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO agents
               (id,scope,project_id,slug,display_name,status,version,
                created_by_user_id)
               VALUES
               (:id,'project',:project_id,'migration-agent',
                'Migration Agent','active',1,:user_id)""",
        ),
        {"id": agent_id, "project_id": project_id, "user_id": user_id},
    )
    await connection.execute(
        text(
            """INSERT INTO agent_versions
               (id,agent_id,version_number,workflow_status,description,soul,
                model_ref,model_settings,tool_groups,payload_checksum,
                created_by_user_id,agents_instructions,identity,user_context,
                payload_schema_version)
               VALUES
               (:id,:agent_id,1,'published',:description,:soul,:model_ref,
                CAST(:model_settings AS jsonb),CAST(:tool_groups AS jsonb),
                :payload_checksum,:user_id,:agents_instructions,:identity,
                :user_context,3)""",
        ),
        {
            "id": agent_version_id,
            "agent_id": agent_id,
            "description": agent_payload["description"],
            "soul": agent_payload["soul"],
            "model_ref": logical_name,
            "model_settings": json.dumps(agent_payload["model_settings"]),
            "tool_groups": json.dumps(agent_payload["tool_groups"]),
            "payload_checksum": agent_payload_checksum,
            "user_id": user_id,
            "agents_instructions": agent_payload["agents_instructions"],
            "identity": agent_payload["identity"],
            "user_context": agent_payload["user_context"],
        },
    )
    await connection.execute(
        text(
            "UPDATE agents SET current_published_version_id=:version_id WHERE id=:agent_id",
        ),
        {
            "version_id": agent_version_id,
            "agent_id": agent_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO threads_meta
               (thread_id,owner_user_id,status,metadata_json,created_at,
                updated_at,project_id,agent_asset_id,agent_scope)
               VALUES
               (:thread_id,:user_id,'idle','{}',now(),now(),:project_id,
                :agent_id,'project')""",
        ),
        {
            "thread_id": thread_id,
            "user_id": user_id,
            "project_id": project_id,
            "agent_id": agent_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO runs
               (run_id,thread_id,owner_user_id,status,model_name,
                multitask_strategy,metadata_json,kwargs_json,origin_trace_id,
                message_count,total_input_tokens,total_output_tokens,
                total_tokens,llm_call_count,lead_agent_tokens,
                subagent_tokens,middleware_tokens,created_at,updated_at,
                project_id)
               VALUES
               (:run_id,:thread_id,:user_id,'pending',:model_ref,'reject',
                '{}',CAST(:kwargs AS json),'migration-trace',0,0,0,0,0,0,0,0,
                now(),now(),:project_id)""",
        ),
        {
            "run_id": run_id,
            "thread_id": thread_id,
            "user_id": user_id,
            "model_ref": logical_name,
            "kwargs": json.dumps(run_kwargs),
            "project_id": project_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO system_model_configs
               (id,display_name,status,current_version_id,revision,
                created_by_user_id,updated_by_user_id,logical_name,
                description,sort_order)
               VALUES
               (:id,'Migration Model','active',NULL,1,:user_id,:user_id,
                :logical_name,'legacy description',7)""",
        ),
        {
            "id": model_config_id,
            "user_id": user_id,
            "logical_name": logical_name,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO system_model_config_versions
               (id,model_config_id,version_number,provider_adapter,
                provider_model,settings,supports_thinking,
                supports_reasoning_effort,supports_vision,payload_checksum,
                created_by_user_id)
               VALUES
               (:id,:model_config_id,1,'openai','migration-provider-model',
                '{}',false,false,true,:payload_checksum,:user_id)""",
        ),
        {
            "id": model_version_id,
            "model_config_id": model_config_id,
            "payload_checksum": model_payload_checksum,
            "user_id": user_id,
        },
    )
    await connection.execute(
        text(
            """UPDATE system_model_configs
                  SET current_version_id=:version_id
                WHERE id=:model_config_id""",
        ),
        {
            "version_id": model_version_id,
            "model_config_id": model_config_id,
        },
    )
    await connection.execute(
        text(
            """UPDATE system_model_catalog_state
                  SET default_model_config_id=:model_config_id,
                      updated_by_user_id=:user_id
                WHERE id=1""",
        ),
        {"model_config_id": model_config_id, "user_id": user_id},
    )
    await connection.execute(
        text(
            """INSERT INTO agent_design_sessions
               (id,project_id,owner_user_id,thread_id,slug,display_name,
                status,revision,messages_json,progress_json,blueprint_json,
                blueprint_checksum,create_idempotency_key_hash,
                create_request_checksum)
               VALUES
               (:id,:project_id,:user_id,:thread_id,'migration-design',
                'Migration Design','proposal_ready',1,'[]','[]',
                CAST(:blueprint AS jsonb),:blueprint_checksum,:key_hash,
                :request_checksum)""",
        ),
        {
            "id": design_session_id,
            "project_id": project_id,
            "user_id": user_id,
            "thread_id": design_thread_id,
            "blueprint": json.dumps(blueprint),
            "blueprint_checksum": blueprint_checksum,
            "key_hash": "b" * 64,
            "request_checksum": "c" * 64,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO system_runtime_policies
               (section,current_version_id,revision,updated_by_user_id)
               VALUES ('agent_runtime',:version_id,1,:user_id)""",
        ),
        {"version_id": runtime_policy_version_id, "user_id": user_id},
    )
    await connection.execute(
        text(
            """INSERT INTO system_runtime_policy_versions
               (id,section,version_number,schema_version,value,
                payload_checksum,created_by_user_id)
               VALUES
               (:id,'agent_runtime',1,3,CAST(:value AS jsonb),
                :payload_checksum,:user_id)""",
        ),
        {
            "id": runtime_policy_version_id,
            "value": json.dumps(runtime_policy),
            "payload_checksum": runtime_policy_checksum,
            "user_id": user_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO run_model_config_snapshots
               (project_id,owner_user_id,thread_id,run_id,purpose,
                model_config_id,model_config_version_id,payload_checksum,
                logical_name)
               VALUES
               (:project_id,:user_id,:thread_id,:run_id,'lead',
                :model_config_id,:model_version_id,:payload_checksum,
                :logical_name)""",
        ),
        {
            "project_id": project_id,
            "user_id": user_id,
            "thread_id": thread_id,
            "run_id": run_id,
            "model_config_id": model_config_id,
            "model_version_id": model_version_id,
            "payload_checksum": model_payload_checksum,
            "logical_name": logical_name,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO run_runtime_policy_snapshots
               (project_id,owner_user_id,thread_id,run_id,section,
                policy_version_id,schema_version,payload_checksum)
               VALUES
               (:project_id,:user_id,:thread_id,:run_id,'agent_runtime',
                :policy_version_id,3,:payload_checksum)""",
        ),
        {
            "project_id": project_id,
            "user_id": user_id,
            "thread_id": thread_id,
            "run_id": run_id,
            "policy_version_id": runtime_policy_version_id,
            "payload_checksum": runtime_policy_checksum,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO run_asset_versions
               (project_id,owner_user_id,thread_id,run_id,asset_kind,
                dependency_order,asset_scope,asset_id,version_id,
                payload_checksum,catalog_generation)
               VALUES
               (:project_id,:user_id,:thread_id,:run_id,'agent',0,'project',
                :agent_id,:agent_version_id,:payload_checksum,1)""",
        ),
        {
            "project_id": project_id,
            "user_id": user_id,
            "thread_id": thread_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "agent_version_id": agent_version_id,
            "payload_checksum": agent_payload_checksum,
        },
    )

    return {
        "agent_payload": agent_payload,
        "agent_version_id": agent_version_id,
        "blueprint": blueprint,
        "design_session_id": design_session_id,
        "logical_name": logical_name,
        "model_config_id": model_config_id,
        "project_id": project_id,
        "run_id": run_id,
        "runtime_policy": runtime_policy,
        "runtime_policy_version_id": runtime_policy_version_id,
        "user_id": user_id,
    }


@pytest.mark.asyncio
async def test_fresh_install_matches_frozen_catalog_and_detects_drift(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
    finally:
        await engine.dispose()
    state, signature = await _catalog_signature(postgres_database_url)
    assert state == "current"
    assert signature == FINAL_M7_CATALOG_SIGNATURE

    drift_engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        async with drift_engine.begin() as connection:
            await connection.execute(text("ALTER TABLE users ADD COLUMN migration_drift_drill text"))
        async with drift_engine.connect() as connection:
            with pytest.raises(M7RecreateRequired):
                await classify_database(connection)
    finally:
        await drift_engine.dispose()


def _pretend_head_is(monkeypatch: pytest.MonkeyPatch, fake_head: str) -> None:
    """Simulate a future released head one step past the initial baseline."""
    chain = (*KNOWN_CHAIN_REVISIONS, fake_head)
    monkeypatch.setattr(bootstrap_module, "KNOWN_CHAIN_REVISIONS", chain)
    monkeypatch.setattr(bootstrap_module, "CURRENT_SCHEMA_REVISION", fake_head)
    monkeypatch.setattr(upgrade_module, "CURRENT_SCHEMA_REVISION", fake_head)


@pytest.mark.asyncio
async def test_behind_database_is_recognized_and_gated_fail_closed(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        _pretend_head_is(monkeypatch, "future_schema")

        async with engine.connect() as connection:
            assert await classify_database(connection) == "behind"

        with pytest.raises(SchemaUpgradeRequired) as validate_error:
            await validate_schema(engine)
        assert "make upgrade-db" in str(validate_error.value)
        assert "future_schema" in str(validate_error.value)

        with pytest.raises(SchemaUpgradeRequired):
            await bootstrap_schema(engine)

        async with engine.begin() as connection:
            await connection.execute(text("UPDATE alembic_version SET version_num = 'mystery_marker'"))
        async with engine.connect() as connection:
            with pytest.raises(M7RecreateRequired):
                await classify_database(connection)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_runner_is_a_noop_on_a_current_database(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
    finally:
        await engine.dispose()

    result = await upgrade_postgres(postgres_database_url)
    assert result.applied is False
    assert result.from_revision == CURRENT_SCHEMA_REVISION
    assert result.to_revision == CURRENT_SCHEMA_REVISION


@pytest.mark.asyncio
@pytest.mark.parametrize("provisional_marker", ["full_schema", "execution_approvals"])
async def test_pre_release_markers_are_not_supported_upgrade_ancestors(
    postgres_database_url: str,
    provisional_marker: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :marker"),
                {"marker": provisional_marker},
            )
        async with engine.connect() as connection:
            with pytest.raises(M7RecreateRequired):
                await classify_database(connection)
    finally:
        await engine.dispose()

    with pytest.raises(PostgresUpgradeError) as error:
        await upgrade_postgres(postgres_database_url)
    assert "显式重建" in str(error.value)


@pytest.mark.asyncio
async def test_upgrade_runner_refuses_an_empty_database(
    postgres_database_url: str,
) -> None:
    with pytest.raises(PostgresUpgradeError) as error:
        await upgrade_postgres(postgres_database_url)
    assert "setup-db" in str(error.value)


def _stamp_marker_sync(database_url: str, marker: str) -> None:
    sync_url = database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    engine = sqlalchemy.create_engine(sync_url, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :marker"),
                {"marker": marker},
            )
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_runner_upgrades_a_behind_database_and_verifies_the_result(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
    finally:
        await engine.dispose()

    fake_head = "future_schema"
    _pretend_head_is(monkeypatch, fake_head)

    applied_urls: list[str] = []

    def _drill_upgrade(url: str) -> None:
        applied_urls.append(url)
        _stamp_marker_sync(url, fake_head)

    monkeypatch.setattr(upgrade_module, "_run_alembic_upgrade_sync", _drill_upgrade)

    result = await upgrade_postgres(postgres_database_url)
    assert applied_urls == [postgres_database_url]
    assert result.applied is True
    assert result.from_revision == CURRENT_SCHEMA_REVISION
    assert result.to_revision == fake_head

    _, signature = await _catalog_signature(postgres_database_url)
    assert signature == FINAL_M7_CATALOG_SIGNATURE


@pytest.mark.asyncio
async def test_output_delivery_migration_upgrades_initial_schema_catalog(
    postgres_database_url: str,
) -> None:
    """Exercise the real released ancestor rather than a marker-only drill."""

    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await _restore_pre_agent_design_activity_schema(connection)
            await _restore_pre_skill_credential_source_schema(connection)
            await _restore_pre_model_catalog_schema(connection)
            await connection.execute(text("DROP TABLE execution_approval_output_delivery_candidates"))
            await connection.execute(text("DROP TABLE execution_approval_output_delivery_obligations"))
            await connection.execute(
                text(
                    "ALTER TABLE execution_approval_requests DROP CONSTRAINT ck_execution_approval_requests_spawn_authorization, DROP COLUMN spawn_authorized_at",
                ),
            )
            await connection.execute(text("UPDATE alembic_version SET version_num = 'initial_schema'"))
    finally:
        await engine.dispose()

    result = await upgrade_postgres(postgres_database_url)
    assert result.applied is True
    assert result.from_revision == "initial_schema"
    assert result.to_revision == CURRENT_SCHEMA_REVISION
    state, signature = await _catalog_signature(postgres_database_url)
    assert state == "current"
    assert signature == FINAL_M7_CATALOG_SIGNATURE


@pytest.mark.asyncio
async def test_agent_design_resume_index_migration_supports_incomplete_keyset(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await _restore_pre_agent_design_activity_schema(connection)
            await _restore_pre_skill_credential_source_schema(connection)
            await connection.execute(
                text("DROP INDEX ix_agent_design_sessions_resume"),
            )
            await connection.execute(
                text(
                    """CREATE INDEX ix_agent_design_sessions_resume
                       ON agent_design_sessions
                          (project_id, owner_user_id, status,
                           updated_at DESC, id DESC)""",
                ),
            )
            await connection.execute(
                text(
                    "UPDATE alembic_version SET version_num = 'model_catalog_simplify'",
                ),
            )
    finally:
        await engine.dispose()

    result = await upgrade_postgres(postgres_database_url)

    assert result.applied is True
    assert result.from_revision == "model_catalog_simplify"
    assert result.to_revision == CURRENT_SCHEMA_REVISION
    state, signature = await _catalog_signature(postgres_database_url)
    assert state == "current"
    assert signature == FINAL_M7_CATALOG_SIGNATURE

    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL enable_seqscan = off"))
            plan_rows = await connection.execute(
                text(
                    """EXPLAIN (COSTS OFF)
                       SELECT id
                         FROM agent_design_sessions
                        WHERE project_id = :project_id
                          AND owner_user_id = :owner_user_id
                          AND status NOT IN (:completed, :cancelled)
                        ORDER BY created_at DESC, id DESC
                        LIMIT 20""",
                ),
                {
                    "project_id": uuid.uuid4(),
                    "owner_user_id": str(uuid.uuid4()),
                    "completed": "completed",
                    "cancelled": "cancelled",
                },
            )
            plan = "\n".join(str(row[0]) for row in plan_rows)
    finally:
        await engine.dispose()

    assert "ix_agent_design_sessions_resume" in plan
    assert "Sort" not in plan


@pytest.mark.asyncio
async def test_agent_archived_slug_reuse_migration_rebuilds_project_index(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await _restore_pre_agent_design_activity_schema(connection)
            await _restore_pre_skill_credential_source_schema(connection)
            await connection.execute(text("DROP INDEX uq_agents_project_slug"))
            await connection.execute(
                text(
                    """CREATE UNIQUE INDEX uq_agents_project_slug
                       ON agents (project_id, lower(slug))
                       WHERE scope = 'project'""",
                ),
            )
            await connection.execute(
                text(
                    "UPDATE alembic_version SET version_num = 'agent_design_resume_index'",
                ),
            )
    finally:
        await engine.dispose()

    result = await upgrade_postgres(postgres_database_url)

    assert result.applied is True
    assert result.from_revision == "agent_design_resume_index"
    assert result.to_revision == CURRENT_SCHEMA_REVISION
    state, signature = await _catalog_signature(postgres_database_url)
    assert state == "current"
    assert signature == FINAL_M7_CATALOG_SIGNATURE

    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            index_definition = await connection.scalar(
                text(
                    """SELECT indexdef
                         FROM pg_indexes
                        WHERE schemaname = current_schema()
                          AND tablename = 'agents'
                          AND indexname = 'uq_agents_project_slug'""",
                ),
            )
    finally:
        await engine.dispose()

    assert index_definition is not None
    normalized = " ".join(str(index_definition).split())
    assert "((scope)::text = 'project'::text)" in normalized
    assert "((status)::text <> 'archived'::text)" in normalized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credential_env_fields", "upgrade_succeeds"),
    [
        (["TARGET_API_KEY"], True),
        (["OTHER_KEY"], False),
    ],
)
async def test_skill_credential_source_field_migration_backfills_existing_rows(
    postgres_database_url: str,
    credential_env_fields: list[str],
    upgrade_succeeds: bool,
) -> None:
    user_id = "20000000-0000-0000-0000-000000000001"
    project_id = uuid.UUID("20000000-0000-0000-0000-000000000002")
    membership_id = uuid.UUID("20000000-0000-0000-0000-000000000003")
    agent_id = uuid.UUID("20000000-0000-0000-0000-000000000004")
    skill_id = uuid.UUID("20000000-0000-0000-0000-000000000005")
    skill_version_id = uuid.UUID("20000000-0000-0000-0000-000000000006")
    credential_id = uuid.UUID("20000000-0000-0000-0000-000000000007")
    credential_version_id = uuid.UUID(
        "20000000-0000-0000-0000-000000000008",
    )
    binding_id = uuid.UUID("20000000-0000-0000-0000-000000000009")
    agent_version_id = uuid.UUID("20000000-0000-0000-0000-00000000000a")
    thread_id = "skill-source-migration-thread"
    run_id = "skill-source-migration-run"
    skill_checksum = _canonical_json_checksum([])
    agent_checksum = _canonical_json_checksum(
        {
            "agents_instructions": "",
            "description": "skill source migration agent",
            "identity": "",
            "mcp_version_ids": [],
            "model_ref": "migration-model",
            "model_settings": {},
            "skill_version_ids": [str(skill_version_id)],
            "soul": "migration soul",
            "tool_groups": [],
            "user_context": "",
        },
    )

    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await _restore_pre_agent_design_activity_schema(connection)
            await _restore_pre_current_asset_lifecycle_schema(connection)
            await connection.execute(
                text(
                    """ALTER TABLE project_skill_credential_bindings
                       DROP COLUMN source_env_field_name""",
                ),
            )
            await connection.execute(
                text(
                    """ALTER TABLE run_skill_credential_snapshots
                       DROP COLUMN source_env_field_name""",
                ),
            )
            await connection.execute(
                text(
                    "UPDATE alembic_version SET version_num = 'agent_archived_slug_reuse'",
                ),
            )
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,username,password_hash,system_role,created_at,
                        oauth_provider,oauth_id,needs_setup,token_version)
                       VALUES
                       (:id,'skill-source@example.invalid','skill_source_user',
                        NULL,'user',now(),NULL,NULL,false,0)""",
                ),
                {"id": user_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                       (id,slug,display_name,created_by_user_id)
                       VALUES
                       (:id,'skill-source-migration','Skill Source Migration',
                        :user_id)""",
                ),
                {"id": project_id, "user_id": user_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                       (id,project_id,user_id,role,status)
                       VALUES (:id,:project_id,:user_id,'admin','active')""",
                ),
                {
                    "id": membership_id,
                    "project_id": project_id,
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO agents
                       (id,scope,project_id,slug,display_name,status,version,
                        created_by_user_id)
                       VALUES
                       (:id,'project',:project_id,'skill-source-agent',
                        'Skill Source Agent','active',1,:user_id)""",
                ),
                {
                    "id": agent_id,
                    "project_id": project_id,
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO threads_meta
                       (thread_id,owner_user_id,status,metadata_json,created_at,
                        updated_at,project_id,agent_asset_id,agent_scope)
                       VALUES
                       (:thread_id,:user_id,'idle','{}',now(),now(),
                        :project_id,:agent_id,'project')""",
                ),
                {
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "project_id": project_id,
                    "agent_id": agent_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO runs
                       (run_id,thread_id,owner_user_id,status,model_name,
                        multitask_strategy,metadata_json,kwargs_json,
                        origin_trace_id,message_count,total_input_tokens,
                        total_output_tokens,total_tokens,llm_call_count,
                        lead_agent_tokens,subagent_tokens,middleware_tokens,
                        created_at,updated_at,project_id)
                       VALUES
                       (:run_id,:thread_id,:user_id,'pending',NULL,'reject',
                        '{}','{}','skill-source-migration-trace',0,0,0,0,0,0,0,0,
                        now(),now(),:project_id)""",
                ),
                {
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "project_id": project_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO skills
                       (id,scope,project_id,slug,display_name,status,version,
                        created_by_user_id)
                       VALUES
                       (:id,'project',:project_id,'skill-source-migration',
                        'Skill Source Migration','active',1,:user_id)""",
                ),
                {
                    "id": skill_id,
                    "project_id": project_id,
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO skill_versions
                       (id,skill_id,version_number,description,
                        frontmatter,compatibility,secret_requirements,
                        scan_decision,scan_summary,payload_checksum,
                        created_by_user_id)
                       VALUES
                       (:id,:skill_id,1,'migration','{}',NULL,
                        CAST(:requirements AS jsonb),
                        'allow','{}',:checksum,:user_id)""",
                ),
                {
                    "id": skill_version_id,
                    "skill_id": skill_id,
                    "requirements": json.dumps(
                        [{"name": "TARGET_API_KEY", "optional": False}],
                    ),
                    "checksum": skill_checksum,
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    "UPDATE skill_versions SET workflow_status='published' WHERE id=:version_id",
                ),
                {"version_id": skill_version_id},
            )
            await connection.execute(
                text(
                    "UPDATE skills SET current_published_version_id=:version_id WHERE id=:skill_id",
                ),
                {"version_id": skill_version_id, "skill_id": skill_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO agent_versions
                       (id,agent_id,version_number,description,soul,model_ref,
                        model_settings,tool_groups,payload_checksum,
                        created_by_user_id,agents_instructions,identity,
                        user_context,payload_schema_version)
                       VALUES
                       (:id,:agent_id,1,'skill source migration agent',
                        'migration soul','migration-model','{}','[]',
                        :checksum,:user_id,'','','',3)""",
                ),
                {
                    "id": agent_version_id,
                    "agent_id": agent_id,
                    "checksum": agent_checksum,
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    "SELECT set_config('deerflow.asset_version_assembly', :version_id, true)",
                ),
                {"version_id": str(agent_version_id)},
            )
            await connection.execute(
                text(
                    """INSERT INTO agent_version_skill_refs
                       (agent_version_id,skill_version_id,sort_order)
                       VALUES (:agent_version_id,:skill_version_id,0)""",
                ),
                {
                    "agent_version_id": agent_version_id,
                    "skill_version_id": skill_version_id,
                },
            )
            await connection.execute(
                text(
                    "SELECT set_config('deerflow.asset_version_assembly', '', true)",
                ),
            )
            await connection.execute(
                text(
                    "UPDATE agent_versions SET workflow_status='published' WHERE id=:version_id",
                ),
                {"version_id": agent_version_id},
            )
            await connection.execute(
                text(
                    "UPDATE agents SET current_published_version_id=:version_id WHERE id=:agent_id",
                ),
                {"version_id": agent_version_id, "agent_id": agent_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO credentials
                       (id,scope,project_id,name,display_name,credential_type,
                        status,is_delete,version,created_by_user_id)
                       VALUES
                       (:id,'project',:project_id,'migration-credential',
                        'Migration Credential','opaque','active',false,1,
                        :user_id)""",
                ),
                {
                    "id": credential_id,
                    "project_id": project_id,
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO credential_versions
                       (id,credential_id,version_number,status,
                        payload_schema_version,payload_schema,
                        created_by_user_id)
                       VALUES
                       (:id,:credential_id,1,'active',1,
                        CAST(:payload_schema AS jsonb),:user_id)""",
                ),
                {
                    "id": credential_version_id,
                    "credential_id": credential_id,
                    "payload_schema": json.dumps(
                        {"env": credential_env_fields},
                    ),
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    "UPDATE credentials SET current_version_id=:version_id WHERE id=:credential_id",
                ),
                {
                    "version_id": credential_version_id,
                    "credential_id": credential_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO project_skill_credential_configs
                       (project_id,skill_id,skill_version_id,revision,
                        created_by_user_id,updated_by_user_id)
                       VALUES
                       (:project_id,:skill_id,:skill_version_id,1,
                        :user_id,:user_id)""",
                ),
                {
                    "project_id": project_id,
                    "skill_id": skill_id,
                    "skill_version_id": skill_version_id,
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO project_skill_credential_bindings
                       (id,project_id,skill_id,skill_version_id,secret_name,
                        credential_id,credential_version_id,config_revision,
                        status,created_by_user_id)
                       VALUES
                       (:id,:project_id,:skill_id,:skill_version_id,
                        'TARGET_API_KEY',:credential_id,:credential_version_id,
                        1,'active',:user_id)""",
                ),
                {
                    "id": binding_id,
                    "project_id": project_id,
                    "skill_id": skill_id,
                    "skill_version_id": skill_version_id,
                    "credential_id": credential_id,
                    "credential_version_id": credential_version_id,
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO run_skill_credential_snapshots
                       (project_id,owner_user_id,thread_id,run_id,skill_id,
                        skill_version_id,secret_name,
                        skill_credential_binding_id,binding_revision,
                        credential_id,credential_version_id)
                       VALUES
                       (:project_id,:user_id,:thread_id,:run_id,:skill_id,
                        :skill_version_id,'TARGET_API_KEY',:binding_id,1,
                        :credential_id,:credential_version_id)""",
                ),
                {
                    "project_id": project_id,
                    "user_id": user_id,
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "skill_id": skill_id,
                    "skill_version_id": skill_version_id,
                    "binding_id": binding_id,
                    "credential_id": credential_id,
                    "credential_version_id": credential_version_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO run_asset_versions
                       (project_id,owner_user_id,thread_id,run_id,asset_kind,
                        dependency_order,asset_scope,asset_id,version_id,
                        payload_checksum,catalog_generation)
                       VALUES
                       (:project_id,:user_id,:thread_id,:run_id,'agent',0,
                        'project',:agent_id,:agent_version_id,:agent_checksum,1),
                       (:project_id,:user_id,:thread_id,:run_id,'skill',1,
                        'project',:skill_id,:skill_version_id,:skill_checksum,1)""",
                ),
                {
                    "project_id": project_id,
                    "user_id": user_id,
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "agent_version_id": agent_version_id,
                    "agent_checksum": agent_checksum,
                    "skill_id": skill_id,
                    "skill_version_id": skill_version_id,
                    "skill_checksum": skill_checksum,
                },
            )
    finally:
        await engine.dispose()

    if not upgrade_succeeds:
        with pytest.raises(
            PostgresUpgradeError,
            match="ASSET_LIFECYCLE_PREFLIGHT_FAILED",
        ):
            await upgrade_postgres(postgres_database_url)
        return

    result = await upgrade_postgres(postgres_database_url)

    assert result.applied is True
    assert result.from_revision == "agent_archived_slug_reuse"
    assert result.to_revision == CURRENT_SCHEMA_REVISION
    state, signature = await _catalog_signature(postgres_database_url)
    assert state == "current"
    assert signature == FINAL_M7_CATALOG_SIGNATURE

    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            binding_source = await connection.scalar(
                text(
                    "SELECT source_env_field_name FROM project_skill_credential_bindings WHERE id=:id",
                ),
                {"id": binding_id},
            )
            snapshot_source = await connection.scalar(
                text(
                    "SELECT source_env_field_name FROM run_skill_credential_snapshots WHERE run_id=:run_id",
                ),
                {"run_id": run_id},
            )
    finally:
        await engine.dispose()

    assert binding_source == "TARGET_API_KEY"
    assert snapshot_source == "TARGET_API_KEY"


@pytest.mark.asyncio
async def test_current_lifecycle_migration_normalizes_system_skill_v1_and_preserves_binding_authority(
    postgres_database_url: str,
) -> None:
    user_id = "21000000-0000-0000-0000-000000000001"
    project_id = uuid.UUID("21000000-0000-0000-0000-000000000002")
    membership_id = uuid.UUID("21000000-0000-0000-0000-000000000003")
    skill_id = uuid.UUID("21000000-0000-0000-0000-000000000004")
    historical_version_id = uuid.UUID("21000000-0000-0000-0000-000000000005")
    current_version_id = uuid.UUID("21000000-0000-0000-0000-000000000006")
    credential_id = uuid.UUID("21000000-0000-0000-0000-000000000007")
    credential_version_id = uuid.UUID("21000000-0000-0000-0000-000000000008")
    historical_binding_id = uuid.UUID("21000000-0000-0000-0000-000000000009")
    current_binding_id = uuid.UUID("21000000-0000-0000-0000-00000000000a")
    envelope_id = uuid.UUID("21000000-0000-0000-0000-00000000000b")
    source_key = "builtin:skill:migration-fixture"
    canonical_version_id = uuid.uuid5(
        uuid.UUID("6f6622dd-a1f5-5799-a2f7-d9f793ea8d2e"),
        f"{source_key}:version:1",
    )
    skill_checksum = _canonical_json_checksum([])

    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await _restore_pre_agent_design_activity_schema(connection)
            await _restore_pre_current_asset_lifecycle_schema(connection)
            await connection.execute(
                text(
                    "UPDATE alembic_version SET version_num = 'skill_credential_source_field'",
                ),
            )
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,username,password_hash,system_role,created_at,
                        oauth_provider,oauth_id,needs_setup,token_version)
                       VALUES
                       (:id,'system-v1@example.invalid','system_v1_user',NULL,
                        'user',now(),NULL,NULL,false,0)""",
                ),
                {"id": user_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                       (id,slug,display_name,created_by_user_id)
                       VALUES (:id,'system-v1-fixture','System v1 Fixture',:user_id)""",
                ),
                {"id": project_id, "user_id": user_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                       (id,project_id,user_id,role,status)
                       VALUES (:id,:project_id,:user_id,'admin','active')""",
                ),
                {
                    "id": membership_id,
                    "project_id": project_id,
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO skills
                       (id,scope,project_id,slug,display_name,status,
                        current_published_version_id,version,source_key,
                        created_by_user_id)
                       VALUES
                       (:id,'system',NULL,'migration-fixture',
                        'Migration Fixture','active',NULL,1,:source_key,:user_id)""",
                ),
                {
                    "id": skill_id,
                    "source_key": source_key,
                    "user_id": user_id,
                },
            )
            for version_id, version_number in (
                (historical_version_id, 1),
                (current_version_id, 2),
            ):
                await connection.execute(
                    text(
                        """INSERT INTO skill_versions
                           (id,skill_id,version_number,workflow_status,
                            description,frontmatter,compatibility,
                            secret_requirements,scan_decision,scan_summary,
                            payload_checksum,created_by_user_id)
                           VALUES
                           (:id,:skill_id,:version_number,'published',
                            'migration fixture','{}',NULL,
                            CAST(:requirements AS jsonb),
                            'allow','{}',:checksum,:user_id)""",
                    ),
                    {
                        "id": version_id,
                        "skill_id": skill_id,
                        "version_number": version_number,
                        "requirements": json.dumps(
                            [{"name": "API_KEY", "optional": False}],
                        ),
                        "checksum": skill_checksum,
                        "user_id": user_id,
                    },
                )
            await connection.execute(
                text(
                    "UPDATE skills SET current_published_version_id=:version_id WHERE id=:skill_id",
                ),
                {"version_id": current_version_id, "skill_id": skill_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO credentials
                       (id,scope,project_id,name,display_name,credential_type,
                        status,is_delete,version,created_by_user_id)
                       VALUES
                       (:id,'project',:project_id,'system-skill-credential',
                        'System Skill Credential','opaque','active',false,1,
                        :user_id)""",
                ),
                {
                    "id": credential_id,
                    "project_id": project_id,
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO credential_versions
                       (id,credential_id,version_number,status,
                        payload_schema_version,payload_schema,
                        created_by_user_id)
                       VALUES
                       (:id,:credential_id,1,'active',1,
                        CAST(:payload_schema AS jsonb),:user_id)""",
                ),
                {
                    "id": credential_version_id,
                    "credential_id": credential_id,
                    "payload_schema": json.dumps({"env": ["API_KEY"]}),
                    "user_id": user_id,
                },
            )
            await connection.execute(
                text(
                    "UPDATE credentials SET current_version_id=:version_id WHERE id=:credential_id",
                ),
                {
                    "version_id": credential_version_id,
                    "credential_id": credential_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO credential_envelopes
                       (id,credential_version_id,envelope_generation,key_id,
                        nonce,ciphertext,is_active,created_by_user_id,activated_at)
                       VALUES
                       (:id,:credential_version_id,1,'migration-key',:nonce,
                        :ciphertext,true,:user_id,now())""",
                ),
                {
                    "id": envelope_id,
                    "credential_version_id": credential_version_id,
                    "nonce": b"n" * 12,
                    "ciphertext": b"c" * 16,
                    "user_id": user_id,
                },
            )
            for version_id, binding_id in (
                (historical_version_id, historical_binding_id),
                (current_version_id, current_binding_id),
            ):
                await connection.execute(
                    text(
                        """INSERT INTO project_skill_credential_configs
                           (project_id,skill_id,skill_version_id,revision,
                            created_by_user_id,updated_by_user_id)
                           VALUES
                           (:project_id,:skill_id,:version_id,1,:user_id,:user_id)""",
                    ),
                    {
                        "project_id": project_id,
                        "skill_id": skill_id,
                        "version_id": version_id,
                        "user_id": user_id,
                    },
                )
                await connection.execute(
                    text(
                        """INSERT INTO project_skill_credential_bindings
                           (id,project_id,skill_id,skill_version_id,secret_name,
                            credential_id,credential_version_id,config_revision,
                            status,created_by_user_id,source_env_field_name)
                           VALUES
                           (:id,:project_id,:skill_id,:version_id,'API_KEY',
                            :credential_id,:credential_version_id,1,'active',
                            :user_id,'API_KEY')""",
                    ),
                    {
                        "id": binding_id,
                        "project_id": project_id,
                        "skill_id": skill_id,
                        "version_id": version_id,
                        "credential_id": credential_id,
                        "credential_version_id": credential_version_id,
                        "user_id": user_id,
                    },
                )
    finally:
        await engine.dispose()

    result = await upgrade_postgres(postgres_database_url)

    assert result.applied is True
    assert result.from_revision == "skill_credential_source_field"
    assert result.to_revision == CURRENT_SCHEMA_REVISION

    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            current_id = await connection.scalar(
                text("SELECT current_version_id FROM skills WHERE id=:skill_id"),
                {"skill_id": skill_id},
            )
            versions = (
                await connection.execute(
                    text(
                        """SELECT id,version_number FROM skill_versions
                           WHERE skill_id=:skill_id ORDER BY version_number""",
                    ),
                    {"skill_id": skill_id},
                )
            ).all()
            bindings = (
                (
                    await connection.execute(
                        text(
                            """SELECT id,skill_version_id,admission_only,
                                      runtime_authority_binding_id,status
                               FROM project_skill_credential_bindings
                               WHERE skill_id=:skill_id ORDER BY admission_only,id""",
                        ),
                        {"skill_id": skill_id},
                    )
                )
                .mappings()
                .all()
            )
            configs = (
                (
                    await connection.execute(
                        text(
                            """SELECT skill_version_id
                           FROM project_skill_credential_configs
                           WHERE skill_id=:skill_id""",
                        ),
                        {"skill_id": skill_id},
                    )
                )
                .scalars()
                .all()
            )
            with pytest.raises(sqlalchemy.exc.IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """INSERT INTO project_skill_credential_bindings
                               (id,project_id,skill_id,skill_version_id,
                                secret_name,credential_id,credential_version_id,
                                config_revision,status,created_by_user_id,
                                source_env_field_name,admission_only)
                               VALUES
                               (:id,:project_id,:skill_id,:historical_version_id,
                                'API_KEY',:credential_id,:credential_version_id,
                                1,'active',:user_id,'API_KEY',false)""",
                        ),
                        {
                            "id": uuid.uuid4(),
                            "project_id": project_id,
                            "skill_id": skill_id,
                            "historical_version_id": historical_version_id,
                            "credential_id": credential_id,
                            "credential_version_id": credential_version_id,
                            "user_id": user_id,
                        },
                    )
        live = [row for row in bindings if not row["admission_only"]]
        tombstones = [row for row in bindings if row["admission_only"]]
        assert len(live) == 1
        admitted_reference = AdmittedSkillCredentialReference(
            skill_id=skill_id,
            skill_version_id=current_version_id,
            env_name="API_KEY",
            credential_field_name="API_KEY",
            binding_id=current_binding_id,
            binding_revision=1,
            credential_id=credential_id,
            credential_version_id=credential_version_id,
        )
        target = frozenset({(current_version_id, "API_KEY")})
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            materials = await lock_admitted_skill_credential_materials(
                session,
                project_id,
                (admitted_reference,),
                declared_targets=target,
                required_targets=target,
            )
        assert len(materials) == 1
        assert materials[0].binding_id == current_binding_id
        assert materials[0].envelope_id == envelope_id

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE project_skill_credential_bindings
                       SET status='revoked',revoked_at=now(),
                           revoked_by_user_id=:user_id
                       WHERE id=:binding_id""",
                ),
                {"user_id": user_id, "binding_id": live[0]["id"]},
            )
        async with factory() as session, session.begin():
            with pytest.raises(SkillCredentialClosureInvalid):
                await lock_admitted_skill_credential_materials(
                    session,
                    project_id,
                    (admitted_reference,),
                    declared_targets=target,
                    required_targets=target,
                )
    finally:
        await engine.dispose()

    assert current_id == canonical_version_id
    assert versions == [(canonical_version_id, 1)]
    assert live[0]["skill_version_id"] == canonical_version_id
    assert live[0]["status"] == "active"
    assert len(tombstones) == 2
    historical = next(row for row in tombstones if row["id"] == historical_binding_id)
    retired_current = next(row for row in tombstones if row["id"] == current_binding_id)
    assert historical["runtime_authority_binding_id"] is None
    assert retired_current["runtime_authority_binding_id"] == live[0]["id"]
    assert configs == [canonical_version_id]
    state, signature = await _catalog_signature(postgres_database_url)
    assert state == "current"
    assert signature == FINAL_M7_CATALOG_SIGNATURE


@pytest.mark.asyncio
async def test_model_catalog_migration_removes_obsolete_fields(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await _restore_pre_agent_design_activity_schema(connection)
            await _restore_pre_skill_credential_source_schema(connection)
            await _restore_pre_model_catalog_schema(connection)
            await connection.execute(
                text(
                    "UPDATE alembic_version SET version_num = 'approval_output_delivery'",
                ),
            )
    finally:
        await engine.dispose()

    result = await upgrade_postgres(postgres_database_url)

    assert result.applied is True
    assert result.from_revision == "approval_output_delivery"
    assert result.to_revision == CURRENT_SCHEMA_REVISION
    state, signature = await _catalog_signature(postgres_database_url)
    assert state == "current"
    assert signature == FINAL_M7_CATALOG_SIGNATURE

    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            columns = await connection.execute(
                text(
                    """SELECT column_name
                       FROM information_schema.columns
                       WHERE table_schema = current_schema()
                         AND table_name = 'system_model_configs'
                       ORDER BY ordinal_position""",
                ),
            )
            indexes = await connection.execute(
                text(
                    """SELECT indexname
                       FROM pg_indexes
                       WHERE schemaname = current_schema()
                         AND tablename = 'system_model_configs'""",
                ),
            )
        column_names = tuple(str(row[0]) for row in columns)
        index_names = {str(row[0]) for row in indexes}
    finally:
        await engine.dispose()

    assert "description" not in column_names
    assert "logical_name" not in column_names
    assert "sort_order" not in column_names
    assert "uq_system_model_configs_logical_name" not in index_names
    assert "ix_system_model_configs_status_order" not in index_names
    assert "ix_system_model_configs_status_created" in index_names


@pytest.mark.asyncio
async def test_model_catalog_migration_rewrites_durable_model_references(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await _restore_pre_agent_design_activity_schema(connection)
            await _restore_pre_skill_credential_source_schema(connection)
            await _restore_pre_model_catalog_schema(connection)
            seeded = await _seed_pre_model_catalog_references(connection)
            await connection.execute(
                text(
                    "UPDATE alembic_version SET version_num = 'approval_output_delivery'",
                ),
            )
    finally:
        await engine.dispose()

    result = await upgrade_postgres(postgres_database_url)
    assert result.applied is True
    assert result.from_revision == "approval_output_delivery"
    assert result.to_revision == CURRENT_SCHEMA_REVISION

    model_ref = str(seeded["model_config_id"])
    expected_agent_payload = json.loads(json.dumps(seeded["agent_payload"]))
    expected_agent_payload["model_ref"] = model_ref
    expected_agent_checksum = _canonical_json_checksum(
        expected_agent_payload,
    )
    expected_run_agent_payload = json.loads(json.dumps(expected_agent_payload))
    expected_run_agent_payload.pop("skill_version_ids")
    expected_run_agent_payload["skill_refs"] = []
    expected_run_agent_checksum = _canonical_json_checksum(expected_run_agent_payload)
    expected_blueprint = json.loads(json.dumps(seeded["blueprint"]))
    expected_blueprint["model_ref"] = model_ref
    expected_blueprint.pop("skill_version_ids")
    expected_blueprint["skill_refs"] = []
    expected_blueprint_checksum = _canonical_json_checksum(expected_blueprint)
    expected_runtime_policy = json.loads(json.dumps(seeded["runtime_policy"]))
    for section in ("input_polish", "memory", "summarization", "vision_bridge"):
        expected_runtime_policy[section]["model_name"] = model_ref
    expected_runtime_policy_checksum = _canonical_json_checksum(
        expected_runtime_policy,
    )

    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            agent = (
                (
                    await connection.execute(
                        text(
                            """SELECT model_ref,payload_checksum
                           FROM agent_versions WHERE id=:id""",
                        ),
                        {"id": seeded["agent_version_id"]},
                    )
                )
                .mappings()
                .one()
            )
            run_asset_checksum = await connection.scalar(
                text(
                    """SELECT payload_checksum
                       FROM run_asset_versions
                       WHERE asset_kind='agent' AND version_id=:id""",
                ),
                {"id": seeded["agent_version_id"]},
            )
            blueprint = (
                (
                    await connection.execute(
                        text(
                            """SELECT blueprint_json,blueprint_checksum
                           FROM agent_design_sessions WHERE id=:id""",
                        ),
                        {"id": seeded["design_session_id"]},
                    )
                )
                .mappings()
                .one()
            )
            policy = (
                (
                    await connection.execute(
                        text(
                            """SELECT value,payload_checksum
                           FROM system_runtime_policy_versions WHERE id=:id""",
                        ),
                        {"id": seeded["runtime_policy_version_id"]},
                    )
                )
                .mappings()
                .one()
            )
            run_policy_checksum = await connection.scalar(
                text(
                    """SELECT payload_checksum
                       FROM run_runtime_policy_snapshots
                       WHERE policy_version_id=:id""",
                ),
                {"id": seeded["runtime_policy_version_id"]},
            )
            run = (
                (
                    await connection.execute(
                        text(
                            """SELECT model_name,kwargs_json
                           FROM runs
                           WHERE project_id=:project_id
                             AND owner_user_id=:user_id
                             AND run_id=:run_id""",
                        ),
                        {
                            "project_id": seeded["project_id"],
                            "user_id": seeded["user_id"],
                            "run_id": seeded["run_id"],
                        },
                    )
                )
                .mappings()
                .one()
            )
            snapshot_columns = tuple(
                str(value)
                for value in (
                    await connection.execute(
                        text(
                            """SELECT column_name
                               FROM information_schema.columns
                               WHERE table_schema=current_schema()
                                 AND table_name='run_model_config_snapshots'
                               ORDER BY ordinal_position""",
                        ),
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()

    assert agent.model_ref == model_ref
    assert agent.payload_checksum == expected_agent_checksum
    assert run_asset_checksum == expected_run_agent_checksum
    assert blueprint.blueprint_json == expected_blueprint
    assert blueprint.blueprint_checksum == expected_blueprint_checksum
    assert policy.value == expected_runtime_policy
    assert policy.payload_checksum == expected_runtime_policy_checksum
    assert run_policy_checksum == expected_runtime_policy_checksum
    assert run.model_name == model_ref
    assert run.kwargs_json["__run_execution_profile"]["requested"]["model_name"] == model_ref
    assert run.kwargs_json["__run_execution_profile"]["effective"]["model_name"] == model_ref
    assert "logical_name" not in snapshot_columns

    state, signature = await _catalog_signature(postgres_database_url)
    assert state == "current"
    assert signature == FINAL_M7_CATALOG_SIGNATURE


@pytest.mark.asyncio
async def test_model_catalog_migration_rejects_unknown_durable_reference(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await _restore_pre_agent_design_activity_schema(connection)
            await _restore_pre_skill_credential_source_schema(connection)
            await _restore_pre_model_catalog_schema(connection)
            seeded = await _seed_pre_model_catalog_references(connection)
            invalid_blueprint = json.loads(json.dumps(seeded["blueprint"]))
            invalid_blueprint["model_ref"] = "unknown-migration-model"
            await connection.execute(
                text(
                    """UPDATE agent_design_sessions
                          SET blueprint_json=CAST(:blueprint AS jsonb),
                              blueprint_checksum=:checksum
                        WHERE id=:id""",
                ),
                {
                    "blueprint": json.dumps(invalid_blueprint),
                    "checksum": _canonical_json_checksum(invalid_blueprint),
                    "id": seeded["design_session_id"],
                },
            )
            await connection.execute(
                text(
                    "UPDATE alembic_version SET version_num = 'approval_output_delivery'",
                ),
            )
    finally:
        await engine.dispose()

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await upgrade_postgres(postgres_database_url)

    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            marker = await connection.scalar(
                text("SELECT version_num FROM alembic_version"),
            )
            logical_column_count = int(
                await connection.scalar(
                    text(
                        """SELECT count(*)
                           FROM information_schema.columns
                           WHERE table_schema=current_schema()
                             AND table_name='system_model_configs'
                             AND column_name='logical_name'""",
                    ),
                )
                or 0
            )
            agent_model_ref = await connection.scalar(
                text("SELECT model_ref FROM agent_versions WHERE id=:id"),
                {"id": seeded["agent_version_id"]},
            )
            immutable_trigger_enabled = await connection.scalar(
                text(
                    """SELECT t.tgenabled
                       FROM pg_trigger AS t
                       JOIN pg_class AS c ON c.oid=t.tgrelid
                       JOIN pg_namespace AS n ON n.oid=c.relnamespace
                       WHERE n.nspname=current_schema()
                         AND c.relname='agent_versions'
                         AND t.tgname='trg_agent_versions_immutable'""",
                ),
            )
    finally:
        await engine.dispose()

    assert marker == "approval_output_delivery"
    assert logical_column_count == 1
    assert agent_model_ref == seeded["logical_name"]
    assert immutable_trigger_enabled in {"O", b"O"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_model_ref",
    ["default", "unknown-migration-model"],
)
async def test_model_catalog_migration_rejects_invalid_runtime_policy_reference(
    postgres_database_url: str,
    invalid_model_ref: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await _restore_pre_agent_design_activity_schema(connection)
            await _restore_pre_skill_credential_source_schema(connection)
            await _restore_pre_model_catalog_schema(connection)
            seeded = await _seed_pre_model_catalog_references(
                connection,
                runtime_policy_model_ref=invalid_model_ref,
            )
            await connection.execute(
                text(
                    "UPDATE alembic_version SET version_num = 'approval_output_delivery'",
                ),
            )
    finally:
        await engine.dispose()

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await upgrade_postgres(postgres_database_url)

    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            marker = await connection.scalar(
                text("SELECT version_num FROM alembic_version"),
            )
            logical_column_count = int(
                await connection.scalar(
                    text(
                        """SELECT count(*)
                           FROM information_schema.columns
                           WHERE table_schema=current_schema()
                             AND table_name='system_model_configs'
                             AND column_name='logical_name'""",
                    ),
                )
                or 0
            )
            runtime_policy = await connection.scalar(
                text(
                    """SELECT value
                       FROM system_runtime_policy_versions
                       WHERE id=:id""",
                ),
                {"id": seeded["runtime_policy_version_id"]},
            )
    finally:
        await engine.dispose()

    assert marker == "approval_output_delivery"
    assert logical_column_count == 1
    assert runtime_policy["memory"]["model_name"] == invalid_model_ref


@pytest.mark.asyncio
async def test_upgrade_runner_fails_closed_when_the_migrated_catalog_does_not_verify(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
    finally:
        await engine.dispose()

    _pretend_head_is(monkeypatch, "future_schema")
    monkeypatch.setattr(upgrade_module, "_run_alembic_upgrade_sync", lambda url: None)

    with pytest.raises(PostgresUpgradeError) as error:
        await upgrade_postgres(postgres_database_url)
    assert "升级后校验失败" in str(error.value)
