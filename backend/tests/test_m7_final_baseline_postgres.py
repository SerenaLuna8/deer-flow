from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from postgres_utils import temporary_postgres_database
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, async_sessionmaker, create_async_engine
from support.m4_private_threads import seed_m4_thread_database

import deerflow.persistence.models  # noqa: F401
from app.final_schema import M7_FINAL_SCHEMA_REVISION
from app.gateway.auth.sessions import generate_session_id, hash_session_id
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_design_service import (
    AgentDesignService,
    CreateAgentDesignSession,
)
from app.shared_assets.bootstrap import bootstrap_system_assets
from app.shared_assets.errors import AssetConflict, AssetNotFound
from app.shared_assets.skill_design_generation import (
    CandidateResult,
    SkillDesignGeneratedFile,
    SkillDesignGenerationRequest,
)
from app.shared_assets.skill_design_service import (
    CancelSkillDesignSession,
    CreateSkillDesignSession,
    SkillDesignMessageTurn,
    SkillDesignService,
    SkillDesignStatus,
    SubmitSkillDesignTurn,
)
from app.shared_assets.skill_service import CreateSkill, SkillService
from deerflow.persistence import bootstrap as bootstrap_module
from deerflow.persistence.auth_sessions import AuthSessionRepository, AuthSessionRow
from deerflow.persistence.base import Base
from deerflow.persistence.shared_assets import SkillDesignDraftFileRow, SkillDesignOperationRow
from deerflow.persistence.user.model import UserRow
from scripts.check_postgres import check_postgres
from scripts.setup_postgres import PostgresSetupError, _bootstrap_existing

CURRENT_SCHEMA_MARKER = "full_schema_v1"

LEGACY_RELATIONS = {
    "automation_cutover_state",
    "automation_migration_ledger",
    "automation_migration_runs",
    "migration_ledger",
    "private_work_cutover_state",
    "private_work_migration_ledger",
    "private_work_migration_runs",
    "reliability_cutover_state",
    "reliability_migration_ledger",
    "reliability_migration_runs",
}
REQUIRED_FUNCTIONS = {
    "bump_asset_catalog_generation",
    "enforce_run_model_snapshot_credential_closure",
    "enforce_scheduled_task_agent_project",
    "enforce_shared_asset_version_state_transition",
    "enforce_stream_terminal_invariant",
    "ensure_system_binding_published_version",
    "prevent_bound_published_version_downgrade",
    "prevent_published_version_child_mutation",
    "prevent_shared_asset_version_payload_update",
    "reject_m7_append_only_mutation",
    "reject_direct_run_model_snapshot_mutation",
    "reject_direct_run_runtime_policy_snapshot_mutation",
    "set_m7_updated_at",
}
REQUIRED_TRIGGERS = {
    "trg_agent_design_operations_updated_at",
    "trg_agent_design_sessions_updated_at",
    "trg_audit_logs_append_only",
    "trg_dead_jobs_append_only",
    "trg_project_usage_ledger_append_only",
    "trg_run_model_config_snapshots_credential_closure",
    "trg_run_model_config_snapshots_immutable",
    "trg_run_runtime_policy_snapshots_immutable",
    "trg_system_runtime_policies_updated_at",
    "trg_system_runtime_policy_catalog_state_updated_at",
    "trg_system_runtime_policy_versions_immutable",
    "trg_run_events_stream_terminal",
    "trg_scheduled_tasks_updated_at",
    "trg_skill_design_draft_files_updated_at",
    "trg_skill_design_operations_updated_at",
    "trg_skill_design_sessions_updated_at",
}


@pytest.mark.asyncio
async def test_user_email_identity_is_case_insensitive_under_concurrency(
    migrated_postgres_database_url: str,
) -> None:
    """The full schema admits only one case variant, even in a write race."""

    engine = create_async_engine(migrated_postgres_database_url)

    async def insert_user(user_id: str, email: str) -> bool:
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO users
                           (id,email,system_role,created_at,needs_setup,token_version)
                           VALUES (:id,:email,'user',now(),false,0)"""
                    ),
                    {"id": user_id, "email": email},
                )
        except IntegrityError:
            return False
        return True

    try:
        async with engine.connect() as connection:
            index_definition = await connection.scalar(
                text(
                    """SELECT pg_get_indexdef(indexrelid)
                         FROM pg_index
                        WHERE indexrelid = 'ix_users_email'::regclass"""
                )
            )
        assert index_definition is not None
        assert "lower((email)::text)" in index_definition

        admitted = await asyncio.gather(
            insert_user(
                "11111111-1111-1111-1111-111111111111",
                "RaceCase@Example.com",
            ),
            insert_user(
                "22222222-2222-2222-2222-222222222222",
                "racecase@example.com",
            ),
        )
        assert sorted(admitted) == [False, True]

        async with engine.connect() as connection:
            count = await connection.scalar(
                text(
                    """SELECT count(*)
                         FROM users
                        WHERE lower(email) = 'racecase@example.com'"""
                )
            )
        assert count == 1
    finally:
        await engine.dispose()


EXPECTED_FUNCTION_FRAGMENTS = {
    "bump_asset_catalog_generation": "generation = asset_catalog_state.generation + 1",
    "enforce_run_model_snapshot_credential_closure": "run model snapshot credential closure mismatch",
    "enforce_scheduled_task_agent_project": "project Agent must belong to the scheduled task project",
    "enforce_shared_asset_version_state_transition": "invalid shared asset version workflow transition",
    "enforce_stream_terminal_invariant": "stream event cannot follow terminal event",
    "ensure_system_binding_published_version": "system binding requires published version",
    "prevent_bound_published_version_downgrade": "bound published version cannot change workflow status",
    "prevent_published_version_child_mutation": "deerflow.skill_hard_delete_asset_id",
    "prevent_shared_asset_version_payload_update": "shared asset version payload is immutable",
    "reject_m7_append_only_mutation": "M7 append-only rows cannot be updated or deleted",
    "reject_direct_run_model_snapshot_mutation": "run model snapshots cannot be updated or directly deleted",
    "reject_direct_run_runtime_policy_snapshot_mutation": "run runtime policy snapshots cannot be updated or directly deleted",
    "set_m7_updated_at": "NEW.updated_at := now()",
}
EXPECTED_TRIGGER_IDENTITIES = {
    "trg_agent_design_operations_updated_at": (
        "agent_design_operations",
        "set_m7_updated_at",
        19,
    ),
    "trg_agent_design_sessions_updated_at": (
        "agent_design_sessions",
        "set_m7_updated_at",
        19,
    ),
    "trg_audit_logs_append_only": ("audit_logs", "reject_m7_append_only_mutation", 27),
    "trg_dead_jobs_append_only": ("dead_jobs", "reject_m7_append_only_mutation", 27),
    "trg_project_usage_ledger_append_only": ("project_usage_ledger", "reject_m7_append_only_mutation", 27),
    "trg_run_model_config_snapshots_credential_closure": (
        "run_model_config_snapshots",
        "enforce_run_model_snapshot_credential_closure",
        7,
    ),
    "trg_run_model_config_snapshots_immutable": (
        "run_model_config_snapshots",
        "reject_direct_run_model_snapshot_mutation",
        27,
    ),
    "trg_run_runtime_policy_snapshots_immutable": (
        "run_runtime_policy_snapshots",
        "reject_direct_run_runtime_policy_snapshot_mutation",
        27,
    ),
    "trg_system_runtime_policies_updated_at": (
        "system_runtime_policies",
        "set_m7_updated_at",
        19,
    ),
    "trg_system_runtime_policy_catalog_state_updated_at": (
        "system_runtime_policy_catalog_state",
        "set_m7_updated_at",
        19,
    ),
    "trg_system_runtime_policy_versions_immutable": (
        "system_runtime_policy_versions",
        "reject_m7_append_only_mutation",
        27,
    ),
    "trg_run_events_stream_terminal": ("run_events", "enforce_stream_terminal_invariant", 7),
    "trg_scheduled_tasks_updated_at": ("scheduled_tasks", "set_m7_updated_at", 19),
    "trg_skill_design_draft_files_updated_at": (
        "skill_design_draft_files",
        "set_m7_updated_at",
        19,
    ),
    "trg_skill_design_operations_updated_at": (
        "skill_design_operations",
        "set_m7_updated_at",
        19,
    ),
    "trg_skill_design_sessions_updated_at": (
        "skill_design_sessions",
        "set_m7_updated_at",
        19,
    ),
}
EXPECTED_APP_SEQUENCE_OWNERS = {
    ("run_events_id_seq", "run_events"),
}
EXPECTED_LANGGRAPH_INDEX_OWNERS = {
    ("checkpoint_blobs_pkey", "checkpoint_blobs"),
    ("checkpoint_blobs_thread_id_idx", "checkpoint_blobs"),
    ("checkpoint_migrations_pkey", "checkpoint_migrations"),
    ("checkpoint_writes_pkey", "checkpoint_writes"),
    ("checkpoint_writes_thread_id_idx", "checkpoint_writes"),
    ("checkpoints_pkey", "checkpoints"),
    ("checkpoints_thread_id_idx", "checkpoints"),
    ("idx_store_expires_at", "store"),
    ("store_pkey", "store"),
    ("store_prefix_idx", "store"),
    ("store_migrations_pkey", "store_migrations"),
}


def _v1_skill_checksum(path: str, content: bytes) -> str:
    canonical = json.dumps(
        [
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _project_context(private_context) -> ProjectContext:
    return ProjectContext(
        user_id=private_context.user_id,
        project_id=private_context.project_id,
        membership_id=private_context.membership_id,
        role=private_context.role,
        capabilities=private_context.capabilities,
        membership_version=private_context.membership_version,
        request_id=private_context.request_id,
    )


class _PostgresSkillDesignGenerator:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        request: SkillDesignGenerationRequest,
        *,
        skill_creator_content: str,
    ) -> CandidateResult:
        assert "Skill Creator" in skill_creator_content
        self.calls += 1
        return CandidateResult(
            files=(
                SkillDesignGeneratedFile(
                    path="SKILL.md",
                    media_type="text/markdown",
                    content=(f"---\nname: {request.skill_slug}\ndescription: Create concise release notes from merged pull requests.\n---\n\n# Workflow\n\nGroup merged changes by label and summarize their user impact.\n"),
                ),
                SkillDesignGeneratedFile(
                    path="scripts/format_release_notes.py",
                    media_type="text/x-python",
                    content=('def format_release_notes(changes: list[str]) -> str:\n    return "\\n".join(changes)\n'),
                ),
            ),
            summary="候选 Skill 已生成。",
        )


def _versions_dir() -> Path:
    return Path(bootstrap_module.__file__).resolve().parent / "migrations" / "versions"


def _full_schema_path() -> Path:
    return Path(bootstrap_module.__file__).resolve().parent / "full_schema.sql"


def _schema_digest_sql() -> str:
    return """
        SELECT md5(COALESCE(string_agg(item, E'\n' ORDER BY item), ''))
        FROM (
            SELECT 'r:' || c.relkind::text || ':' || n.nspname || ':' || c.relname AS item
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
            UNION ALL
            SELECT 'a:' || c.relname || ':' || a.attnum::text || ':' || a.attname || ':' ||
                   pg_catalog.format_type(a.atttypid, a.atttypmod) || ':' || a.attnotnull::text
            FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND a.attnum > 0 AND NOT a.attisdropped
            UNION ALL
            SELECT 'x:' || c.relname || ':' || i.relname || ':' || pg_get_indexdef(i.oid)
            FROM pg_index x JOIN pg_class c ON c.oid = x.indrelid
            JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
            UNION ALL
            SELECT 'k:' || c.relname || ':' || con.conname || ':' || pg_get_constraintdef(con.oid, true)
            FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
            UNION ALL
            SELECT 'f:' || p.proname || ':' || pg_get_function_identity_arguments(p.oid) || ':' || pg_get_functiondef(p.oid)
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = current_schema()
            UNION ALL
            SELECT 't:' || t.typname || ':' || t.typtype::text || ':' ||
                   COALESCE(array_to_string(ARRAY(
                       SELECT e.enumlabel FROM pg_enum e
                       WHERE e.enumtypid=t.oid ORDER BY e.enumsortorder
                   ), ','), '')
            FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
            LEFT JOIN pg_class c ON c.oid = t.typrelid
            WHERE n.nspname = current_schema()
              AND t.typelem = 0
              AND (t.typrelid = 0 OR c.relkind = 'c')
        ) catalog
    """


async def _schema_digest(connection: AsyncConnection) -> str:
    return str(await connection.scalar(text(_schema_digest_sql())))


async def _table_row_counts(connection: AsyncConnection) -> tuple[tuple[str, int], ...]:
    tables = tuple(
        (
            await connection.execute(
                text(
                    """SELECT c.relname FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=current_schema() AND c.relkind IN ('r','p')
                    ORDER BY c.relname"""
                )
            )
        ).scalars()
    )
    counts = []
    for table_name in tables:
        assert str(table_name).replace("_", "").isalnum()
        counts.append((str(table_name), int(await connection.scalar(text(f'SELECT count(*) FROM "{table_name}"')) or 0)))
    return tuple(counts)


async def _entrypoint_refusal_result(
    engine,
    database_url: str,
) -> tuple[bool, bool, bool, bool, str, tuple[tuple[str, int], ...]]:
    """Exercise every final-schema entrypoint without hiding partial acceptance."""

    classify_rejected = False
    async with engine.connect() as connection:
        try:
            await bootstrap_module.classify_database(connection)
        except bootstrap_module.M7RecreateRequired:
            classify_rejected = True

    bootstrap_rejected = False
    try:
        await bootstrap_module.bootstrap_schema(engine)
    except bootstrap_module.M7RecreateRequired:
        bootstrap_rejected = True

    setup_rejected = False
    try:
        await _bootstrap_existing(database_url)
    except PostgresSetupError as exc:
        setup_rejected = "M7_RECREATE_REQUIRED" in str(exc)

    check = await check_postgres(database_url)
    async with engine.connect() as connection:
        catalog = await _schema_digest(connection)
        row_counts = await _table_row_counts(connection)
    return (
        classify_rejected,
        bootstrap_rejected,
        setup_rejected,
        "M7_RECREATE_REQUIRED" in check.error,
        catalog,
        row_counts,
    )


async def _sequence_index_owners(
    connection: AsyncConnection,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    sequence_rows = await connection.execute(
        text(
            """SELECT seq.relname,COALESCE(owner.relname,'')
            FROM pg_class seq JOIN pg_namespace n ON n.oid=seq.relnamespace
            LEFT JOIN pg_depend d ON d.classid='pg_class'::regclass
              AND d.objid=seq.oid AND d.refclassid='pg_class'::regclass
              AND d.deptype IN ('a','i')
            LEFT JOIN pg_class owner ON owner.oid=d.refobjid
            WHERE n.nspname=current_schema() AND seq.relkind='S'"""
        )
    )
    index_rows = await connection.execute(
        text(
            """SELECT idx.relname,owner.relname
            FROM pg_class idx JOIN pg_namespace n ON n.oid=idx.relnamespace
            JOIN pg_index x ON x.indexrelid=idx.oid
            JOIN pg_class owner ON owner.oid=x.indrelid
            WHERE n.nspname=current_schema() AND idx.relkind IN ('i','I')"""
        )
    )
    return (
        {(str(name), str(owner)) for name, owner in sequence_rows},
        {(str(name), str(owner)) for name, owner in index_rows},
    )


async def _native_relational_catalog(
    connection: AsyncConnection,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Independent pg_catalog snapshot; intentionally does not use the production verifier."""

    tables = sorted(Base.metadata.tables)
    queries = {
        "relations": """
            SELECT c.relname,c.relkind::text,c.relpersistence::text,
                   c.relrowsecurity,c.relforcerowsecurity,COALESCE(pg_get_partkeydef(c.oid),'')
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relname=ANY(CAST(:tables AS text[]))
            ORDER BY c.relname
        """,
        "columns": """
            SELECT c.relname,a.attnum,a.attname,format_type(a.atttypid,a.atttypmod),
                   a.attnotnull,a.attidentity::text,a.attgenerated::text,
                   COALESCE(coll.collname,''),
                   COALESCE(regexp_replace(pg_get_expr(ad.adbin,ad.adrelid,true),'\\s+',' ','g'),'')
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            LEFT JOIN pg_attrdef ad ON ad.adrelid=a.attrelid AND ad.adnum=a.attnum
            LEFT JOIN pg_collation coll ON coll.oid=a.attcollation
            WHERE n.nspname=current_schema() AND c.relname=ANY(CAST(:tables AS text[]))
              AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY c.relname,a.attnum
        """,
        "constraints": """
            SELECT c.relname,con.conname,con.contype::text,con.condeferrable,
                   con.condeferred,con.convalidated,
                   regexp_replace(pg_get_constraintdef(con.oid,true),'\\s+',' ','g')
            FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relname=ANY(CAST(:tables AS text[]))
            ORDER BY c.relname,con.conname
        """,
        "indexes": """
            SELECT c.relname,i.relname,x.indisunique,x.indisprimary,x.indisvalid,x.indisready,
                   regexp_replace(pg_get_indexdef(i.oid,0,true),'\\s+',' ','g')
            FROM pg_index x JOIN pg_class c ON c.oid=x.indrelid
            JOIN pg_class i ON i.oid=x.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relname=ANY(CAST(:tables AS text[]))
            ORDER BY c.relname,i.relname
        """,
    }
    snapshot = {}
    for category, query in queries.items():
        result = await connection.execute(text(query), {"tables": tables})
        snapshot[category] = tuple(tuple(row) for row in result)
    return snapshot


def test_schema_initialization_uses_one_complete_sql_contract() -> None:
    revision_files = sorted(path for path in _versions_dir().glob("*.py") if path.name != "__init__.py")
    assert revision_files == []
    schema_path = _full_schema_path()
    assert schema_path.is_file()
    assert CURRENT_SCHEMA_MARKER in schema_path.read_text(encoding="utf-8")


def test_final_metadata_and_contract_have_no_staged_relations() -> None:
    assert not (LEGACY_RELATIONS & set(Base.metadata.tables))
    assert not hasattr(importlib.import_module("app.final_schema"), "PRE_RESET_SCHEMA_REVISION")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_empty_database_installs_complete_schema_marker(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == CURRENT_SCHEMA_MARKER
            assert await connection.scalar(text("SELECT to_regclass('user_notifications')")) == "user_notifications"
            relations = set((await connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"))).scalars())
            assert {
                "agent_design_operations",
                "agent_design_sessions",
                "skill_design_draft_files",
                "skill_design_operations",
                "skill_design_sessions",
            } <= relations
            assert not (relations & LEGACY_RELATIONS)
            agent_version_columns = set(
                (
                    await connection.execute(
                        text(
                            """SELECT column_name
                               FROM information_schema.columns
                               WHERE table_schema=current_schema()
                                 AND table_name='agent_versions'"""
                        )
                    )
                ).scalars()
            )
            assert {
                "agents_instructions",
                "identity",
                "payload_schema_version",
                "user_context",
            } <= agent_version_columns
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_agent_builder_session_persists_optional_json_as_sql_null(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    now = datetime.now(UTC)
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'user',:now,false,0)"""
                ),
                {
                    "id": str(user_id),
                    "email": f"agent-builder-{user_id}@example.com",
                    "now": now,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                       (id,slug,display_name,created_by_user_id,created_at,updated_at)
                       VALUES (:id,:slug,:name,:user_id,:now,:now)"""
                ),
                {
                    "id": project_id,
                    "slug": "agent-builder-null",
                    "name": "Agent Builder Null",
                    "user_id": str(user_id),
                    "now": now,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                       (id,project_id,user_id,role,status,version,
                        activation_generation,created_at,updated_at)
                       VALUES (:id,:project_id,:user_id,'admin','active',1,1,:now,:now)"""
                ),
                {
                    "id": membership_id,
                    "project_id": project_id,
                    "user_id": str(user_id),
                    "now": now,
                },
            )

        role = ProjectRole.ADMIN
        context = ProjectContext(
            user_id=user_id,
            project_id=project_id,
            membership_id=membership_id,
            role=role,
            capabilities=capabilities_for(role),
            membership_version=1,
            request_id="req-agent-builder-null",
        )
        created = await AgentDesignService(factory).create(
            context,
            CreateAgentDesignSession(
                slug="null-safe-agent",
                display_name="Null Safe Agent",
                idempotency_key="create-null-safe-agent",
            ),
        )

        assert created.blueprint is None
        assert created.active_clarification is None
        async with engine.connect() as connection:
            stored = (
                await connection.execute(
                    text(
                        """SELECT blueprint_json IS NULL,
                                  active_clarification_json IS NULL
                           FROM agent_design_sessions
                           WHERE id=:session_id"""
                    ),
                    {"session_id": created.id},
                )
            ).one()
        assert stored == (True, True)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_skill_builder_is_owner_scoped_and_cancel_physically_clears_candidate_files(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    owner = _project_context(seed.owner_a)
    same_project_other_owner = _project_context(seed.owner_b)
    same_owner_other_project = _project_context(seed.project_b_owner_a)
    generator = _PostgresSkillDesignGenerator()
    service = SkillDesignService(seed.factory, generator=generator)
    try:
        await bootstrap_system_assets(seed.factory)
        created = await service.create(
            owner,
            CreateSkillDesignSession(
                slug="postgres-release-notes",
                display_name="PostgreSQL Release Notes",
                idempotency_key="create-postgres-release-notes",
            ),
        )
        ready = await service.submit_turn(
            owner,
            created.id,
            SubmitSkillDesignTurn(
                input=SkillDesignMessageTurn(
                    kind="message",
                    message="请按标签整理已合并的拉取请求并生成简洁的发布说明。",
                ),
                expected_revision=created.revision,
                idempotency_key="generate-postgres-release-notes",
            ),
        )

        assert ready.status is SkillDesignStatus.DRAFT_READY
        assert ready.draft_checksum is not None
        assert [file.path for file in ready.files] == [
            "SKILL.md",
            "scripts/format_release_notes.py",
        ]
        assert generator.calls == 1
        with pytest.raises(AssetNotFound):
            await service.get(same_project_other_owner, created.id)
        with pytest.raises(AssetNotFound):
            await service.get(same_owner_other_project, created.id)

        async with seed.factory() as session:
            stored_files = tuple(
                (
                    await session.execute(
                        select(SkillDesignDraftFileRow)
                        .where(
                            SkillDesignDraftFileRow.project_id == owner.project_id,
                            SkillDesignDraftFileRow.owner_user_id == str(owner.user_id),
                            SkillDesignDraftFileRow.session_id == created.id,
                        )
                        .order_by(SkillDesignDraftFileRow.path)
                    )
                ).scalars()
            )
            turn_operations = tuple(
                (
                    await session.execute(
                        select(SkillDesignOperationRow).where(
                            SkillDesignOperationRow.project_id == owner.project_id,
                            SkillDesignOperationRow.owner_user_id == str(owner.user_id),
                            SkillDesignOperationRow.session_id == created.id,
                        )
                    )
                ).scalars()
            )
        assert [row.path for row in stored_files] == [
            "SKILL.md",
            "scripts/format_release_notes.py",
        ]
        assert {row.operation_kind for row in turn_operations} == {"turn"}
        assert {row.status for row in turn_operations} == {"completed"}

        cancel_command = CancelSkillDesignSession(
            expected_revision=ready.revision,
            idempotency_key="cancel-postgres-release-notes",
        )
        cancelled = await service.cancel(owner, created.id, cancel_command)
        repeated = await service.cancel(owner, created.id, cancel_command)

        assert repeated == cancelled
        assert cancelled.status is SkillDesignStatus.CANCELLED
        assert cancelled.files == ()
        assert cancelled.draft_checksum is None
        assert cancelled.validation is None
        async with seed.factory() as session:
            assert (
                await session.scalar(
                    select(SkillDesignDraftFileRow).where(
                        SkillDesignDraftFileRow.session_id == created.id,
                    )
                )
                is None
            )
            stored_session = (
                await session.execute(
                    text(
                        """SELECT project_id,owner_user_id,status,draft_checksum,
                                  validation_json,active_clarification_json,
                                  error_code,error_message
                           FROM skill_design_sessions
                           WHERE id=:session_id"""
                    ),
                    {"session_id": created.id},
                )
            ).one()
            operations = tuple(
                (
                    await session.execute(
                        select(SkillDesignOperationRow)
                        .where(
                            SkillDesignOperationRow.project_id == owner.project_id,
                            SkillDesignOperationRow.owner_user_id == str(owner.user_id),
                            SkillDesignOperationRow.session_id == created.id,
                        )
                        .order_by(SkillDesignOperationRow.operation_kind)
                    )
                ).scalars()
            )
        assert stored_session == (
            owner.project_id,
            str(owner.user_id),
            "cancelled",
            None,
            None,
            None,
            None,
            None,
        )
        assert [(row.operation_kind, row.status) for row in operations] == [
            ("cancel", "completed"),
            ("turn", "completed"),
        ]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_skill_display_name_is_case_insensitively_unique_per_project_under_concurrency(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    user_id = uuid.uuid4()
    first_project_id = uuid.uuid4()
    second_project_id = uuid.uuid4()
    first_membership_id = uuid.uuid4()
    now = datetime.now(UTC)
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'user',:now,false,0)"""
                ),
                {
                    "id": str(user_id),
                    "email": f"skill-name-{user_id}@example.com",
                    "now": now,
                },
            )
            for project_id, slug in (
                (first_project_id, "skill-name-first"),
                (second_project_id, "skill-name-second"),
            ):
                await connection.execute(
                    text(
                        """INSERT INTO projects
                           (id,slug,display_name,created_by_user_id,created_at,updated_at)
                           VALUES (:id,:slug,:name,:user_id,:now,:now)"""
                    ),
                    {
                        "id": project_id,
                        "slug": slug,
                        "name": slug,
                        "user_id": str(user_id),
                        "now": now,
                    },
                )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                       (id,project_id,user_id,role,status,version)
                       VALUES (:id,:project_id,:user_id,'admin','active',1)"""
                ),
                {
                    "id": first_membership_id,
                    "project_id": first_project_id,
                    "user_id": str(user_id),
                },
            )

        async def insert_skill(
            *,
            project_id: uuid.UUID,
            slug: str,
            display_name: str,
        ) -> None:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO skills
                           (id,scope,project_id,slug,display_name,created_by_user_id)
                           VALUES (:id,'project',:project_id,:slug,:display_name,:user_id)"""
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_id,
                        "slug": slug,
                        "display_name": display_name,
                        "user_id": str(user_id),
                    },
                )

        same_project_results = await asyncio.gather(
            insert_skill(
                project_id=first_project_id,
                slug="concurrent-name-a",
                display_name="Shared Name",
            ),
            insert_skill(
                project_id=first_project_id,
                slug="concurrent-name-b",
                display_name="shared name",
            ),
            return_exceptions=True,
        )
        assert sum(result is None for result in same_project_results) == 1
        assert sum(isinstance(result, IntegrityError) for result in same_project_results) == 1

        await insert_skill(
            project_id=second_project_id,
            slug="same-name-other-project",
            display_name="SHARED NAME",
        )
        actor = ProjectContext(
            user_id=user_id,
            project_id=first_project_id,
            membership_id=first_membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="req-project-skill-name-conflict",
        )
        service = SkillService(async_sessionmaker(engine, expire_on_commit=False))
        with pytest.raises(AssetConflict) as conflict:
            await service.create_asset(
                actor,
                CreateSkill(
                    slug="service-name-conflict",
                    display_name="sHaReD nAmE",
                ),
            )
        assert conflict.value.request_id == actor.request_id

        async with engine.connect() as connection:
            index_definition = await connection.scalar(
                text(
                    """SELECT indexdef FROM pg_indexes
                       WHERE schemaname=current_schema()
                         AND indexname='uq_skills_project_display_name'"""
                )
            )
            assert index_definition is not None
            assert "UNIQUE INDEX" in index_definition
            assert "project_id" in index_definition
            assert "lower((display_name)::text)" in index_definition
            assert "WHERE ((scope)::text = 'project'::text)" in index_definition
            assert (
                await connection.scalar(
                    text(
                        """SELECT count(*) FROM skills
                           WHERE lower(display_name)=lower('Shared Name')"""
                    )
                )
                == 2
            )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_auth_session_authority_is_hashed_revocable_and_restart_safe(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = str(uuid.uuid4())
    raw_session_id = generate_session_id()
    session_hash = hash_session_id(raw_session_id)
    created_at = datetime.now(UTC)
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with factory() as session, session.begin():
            session.add(
                UserRow(
                    id=user_id,
                    email=f"auth-session-{uuid.uuid4().hex}@example.com",
                    password_hash=None,
                    system_role="user",
                    created_at=created_at,
                    needs_setup=False,
                    token_version=7,
                )
            )

        await AuthSessionRepository(factory).create(
            session_id_hash=session_hash,
            user_id=user_id,
            created_at=created_at,
            expires_at=created_at + timedelta(hours=1),
        )

        # A separately constructed repository sees the same durable authority.
        restarted_repository = AuthSessionRepository(
            async_sessionmaker(engine, expire_on_commit=False),
        )
        assert await restarted_repository.validate(
            session_id_hash=session_hash,
            user_id=user_id,
            token_version=7,
            now=created_at + timedelta(minutes=1),
        )
        assert not await restarted_repository.validate(
            session_id_hash=session_hash,
            user_id=user_id,
            token_version=8,
            now=created_at + timedelta(minutes=1),
        )
        assert not await restarted_repository.validate(
            session_id_hash=session_hash,
            user_id=str(uuid.uuid4()),
            token_version=7,
            now=created_at + timedelta(minutes=1),
        )
        assert not await restarted_repository.validate(
            session_id_hash=session_hash,
            user_id=user_id,
            token_version=7,
            now=created_at + timedelta(hours=2),
        )

        async with factory() as session:
            stored = await session.scalar(select(AuthSessionRow.session_id_hash))
        assert stored == session_hash
        assert raw_session_id != stored

        assert await restarted_repository.revoke(
            session_id_hash=session_hash,
            user_id=user_id,
            now=created_at + timedelta(minutes=2),
        )
        assert not await restarted_repository.validate(
            session_id_hash=session_hash,
            user_id=user_id,
            token_version=7,
            now=created_at + timedelta(minutes=3),
        )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_auth_session_create_prunes_only_one_bounded_stale_batch(
    postgres_database_url: str,
) -> None:
    from deerflow.persistence.auth_sessions.sql import (
        _SESSION_PRUNE_BATCH_SIZE,
        _SESSION_PRUNE_GRACE,
    )

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    old_created = now - _SESSION_PRUNE_GRACE - timedelta(days=20)
    old_expiry = now - _SESSION_PRUNE_GRACE - timedelta(days=1)
    future_expiry = now + timedelta(days=30)
    expired_hashes = tuple(f"{index + 1:064x}" for index in range(_SESSION_PRUNE_BATCH_SIZE + 2))
    old_revoked_hash = "a" * 64
    recent_expired_hash = "b" * 64
    recent_revoked_hash = "c" * 64
    active_hash = "d" * 64
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with factory() as session, session.begin():
            session.add(
                UserRow(
                    id=user_id,
                    email=f"auth-prune-{uuid.uuid4().hex}@example.com",
                    password_hash=None,
                    system_role="user",
                    created_at=old_created,
                    needs_setup=False,
                    token_version=0,
                )
            )
            await session.flush()
            session.add_all(
                [
                    AuthSessionRow(
                        session_id_hash=session_hash,
                        user_id=user_id,
                        created_at=old_created,
                        expires_at=old_expiry,
                        last_seen_at=old_created,
                    )
                    for session_hash in expired_hashes
                ]
            )
            session.add_all(
                [
                    AuthSessionRow(
                        session_id_hash=old_revoked_hash,
                        user_id=user_id,
                        created_at=old_created,
                        expires_at=future_expiry,
                        revoked_at=old_expiry,
                        last_seen_at=old_created,
                    ),
                    AuthSessionRow(
                        session_id_hash=recent_expired_hash,
                        user_id=user_id,
                        created_at=now - timedelta(days=2),
                        expires_at=now - timedelta(days=1),
                        last_seen_at=now - timedelta(days=2),
                    ),
                    AuthSessionRow(
                        session_id_hash=recent_revoked_hash,
                        user_id=user_id,
                        created_at=now - timedelta(days=2),
                        expires_at=future_expiry,
                        revoked_at=now - timedelta(days=1),
                        last_seen_at=now - timedelta(days=2),
                    ),
                    AuthSessionRow(
                        session_id_hash=active_hash,
                        user_id=user_id,
                        created_at=old_created,
                        expires_at=future_expiry,
                        last_seen_at=old_created,
                    ),
                ]
            )

        repository = AuthSessionRepository(factory)
        first_new_hash = "e" * 64
        await repository.create(
            session_id_hash=first_new_hash,
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
        cutoff = now - _SESSION_PRUNE_GRACE
        async with factory() as session:
            eligible_after_first = await session.scalar(select(text("count(*)")).select_from(AuthSessionRow).where((AuthSessionRow.expires_at <= cutoff) | (AuthSessionRow.revoked_at <= cutoff)))
            retained = set(
                (
                    await session.execute(
                        select(AuthSessionRow.session_id_hash).where(
                            AuthSessionRow.session_id_hash.in_(
                                (
                                    recent_expired_hash,
                                    recent_revoked_hash,
                                    active_hash,
                                    first_new_hash,
                                )
                            )
                        )
                    )
                ).scalars()
            )
        assert eligible_after_first == 3
        assert retained == {
            recent_expired_hash,
            recent_revoked_hash,
            active_hash,
            first_new_hash,
        }

        second_new_hash = "f" * 64
        await repository.create(
            session_id_hash=second_new_hash,
            user_id=user_id,
            created_at=now + timedelta(seconds=1),
            expires_at=now + timedelta(hours=1),
        )
        async with factory() as session:
            eligible_after_second = await session.scalar(select(text("count(*)")).select_from(AuthSessionRow).where((AuthSessionRow.expires_at <= cutoff) | (AuthSessionRow.revoked_at <= cutoff)))
        assert eligible_after_second == 0
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_marker_is_rejected_before_any_ddl(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE alembic_version (version_num varchar(64) PRIMARY KEY)"))
            await connection.execute(text("INSERT INTO alembic_version VALUES ('0015_project_reliability_finalize')"))
        async with engine.connect() as connection:
            before = await _schema_digest(connection)

        error_type = getattr(bootstrap_module, "M7RecreateRequired", RuntimeError)
        with pytest.raises(error_type) as captured:
            await bootstrap_module.bootstrap_schema(engine)
        assert getattr(captured.value, "code", None) == "M7_RECREATE_REQUIRED"

        async with engine.connect() as connection:
            assert await _schema_digest(connection) == before
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_unknown_nonempty_schema_is_rejected_before_any_ddl(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE unknown_business_data (id bigint PRIMARY KEY, payload text)"))
            await connection.execute(text("INSERT INTO unknown_business_data VALUES (1, 'keep-me')"))
        async with engine.connect() as connection:
            before = await _schema_digest(connection)

        error_type = getattr(bootstrap_module, "M7RecreateRequired", RuntimeError)
        with pytest.raises(error_type) as captured:
            await bootstrap_module.bootstrap_schema(engine)
        assert getattr(captured.value, "code", None) == "M7_RECREATE_REQUIRED"

        async with engine.connect() as connection:
            assert await _schema_digest(connection) == before
            assert await connection.scalar(text("SELECT payload FROM unknown_business_data WHERE id=1")) == "keep-me"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("object_kind", "create_sql"),
    [
        ("sequence", "CREATE SEQUENCE reviewer_only_sequence"),
        (
            "function",
            "CREATE FUNCTION reviewer_only_function() RETURNS integer LANGUAGE sql IMMUTABLE AS 'SELECT 7'",
        ),
        ("type", "CREATE TYPE reviewer_only_type AS ENUM ('alpha', 'beta')"),
    ],
)
async def test_user_schema_object_only_database_is_rejected_without_mutation(
    postgres_database_url: str,
    object_kind: str,
    create_sql: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(create_sql))
        async with engine.connect() as connection:
            before_catalog = await _schema_digest(connection)
            before_rows = await _table_row_counts(connection)

        with pytest.raises(bootstrap_module.M7RecreateRequired) as captured:
            await bootstrap_module.bootstrap_schema(engine)
        assert captured.value.code == "M7_RECREATE_REQUIRED"

        async with engine.connect() as connection:
            assert await _schema_digest(connection) == before_catalog, object_kind
            assert await _table_row_counts(connection) == before_rows, object_kind
            assert await connection.scalar(text("SELECT to_regclass('alembic_version')")) is None
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_extension_owned_schema_objects_are_allowed_during_empty_bootstrap(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE EXTENSION hstore WITH SCHEMA public"))

        await bootstrap_module.bootstrap_schema(engine)

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == M7_FINAL_SCHEMA_REVISION
            assert await connection.scalar(text("SELECT extname FROM pg_extension WHERE extname='hstore'")) == "hstore"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_unexpected_app_owned_sequence_is_rejected_by_every_entrypoint_without_mutation(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(text("CREATE SEQUENCE unexpected_owned_sequence OWNED BY projects.membership_version"))
        async with engine.connect() as connection:
            before_catalog = await _schema_digest(connection)
            before_rows = await _table_row_counts(connection)

        result = await _entrypoint_refusal_result(engine, postgres_database_url)

        assert result == (True, True, True, True, before_catalog, before_rows)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('unexpected_owned_sequence')")) == "unexpected_owned_sequence"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_unexpected_langgraph_index_is_rejected_by_every_entrypoint_without_mutation(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await _bootstrap_existing(postgres_database_url)
        async with engine.begin() as connection:
            await connection.execute(text("CREATE INDEX unexpected_lg_index ON checkpoints(thread_id)"))
        async with engine.connect() as connection:
            before_catalog = await _schema_digest(connection)
            before_rows = await _table_row_counts(connection)

        result = await _entrypoint_refusal_result(engine, postgres_database_url)

        assert result == (True, True, True, True, before_catalog, before_rows)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('unexpected_lg_index')")) == "unexpected_lg_index"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_app_only_and_full_langgraph_stages_have_exact_sequence_index_inventory(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    langgraph_tables = set(bootstrap_module._LANGGRAPH_TABLES)
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with engine.connect() as connection:
            app_sequences, app_indexes = await _sequence_index_owners(connection)
            assert app_sequences == EXPECTED_APP_SEQUENCE_OWNERS
            assert not {identity for identity in app_indexes if identity[1] in langgraph_tables}
            assert await bootstrap_module.classify_database(connection) == "current"

        await _bootstrap_existing(postgres_database_url)
        async with engine.connect() as connection:
            full_sequences, full_indexes = await _sequence_index_owners(connection)
            assert full_sequences == EXPECTED_APP_SEQUENCE_OWNERS
            assert {identity for identity in full_indexes if identity[1] in langgraph_tables} == EXPECTED_LANGGRAPH_INDEX_OWNERS
            assert await bootstrap_module.classify_database(connection) == "current"
        check = await check_postgres(postgres_database_url)
        assert check.healthy is True
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_partial_langgraph_inventory_is_rejected_without_mutation(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE checkpoints (thread_id text PRIMARY KEY)"))
        async with engine.connect() as connection:
            before_catalog = await _schema_digest(connection)
            before_rows = await _table_row_counts(connection)

        result = await _entrypoint_refusal_result(engine, postgres_database_url)

        assert result == (True, True, True, True, before_catalog, before_rows)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('checkpoints')")) == "checkpoints"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_missing_langgraph_index_is_rejected_without_repair(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await _bootstrap_existing(postgres_database_url)
        async with engine.begin() as connection:
            await connection.execute(text("DROP INDEX checkpoints_thread_id_idx"))
        async with engine.connect() as connection:
            before_catalog = await _schema_digest(connection)
            before_rows = await _table_row_counts(connection)

        result = await _entrypoint_refusal_result(engine, postgres_database_url)

        assert result == (True, True, True, True, before_catalog, before_rows)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('checkpoints_thread_id_idx')")) is None
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drift_kind", "mutation_sql"),
    [
        (
            "trigger-missing",
            ("DROP TRIGGER trg_run_events_stream_terminal ON run_events",),
        ),
        (
            "trigger-body",
            (
                "DROP TRIGGER trg_run_events_stream_terminal ON run_events",
                "CREATE TRIGGER trg_run_events_stream_terminal BEFORE INSERT ON run_events FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
            ),
        ),
        ("column-nullability", ("ALTER TABLE jobs ALTER COLUMN max_attempts DROP NOT NULL",)),
        ("column-default", ("ALTER TABLE jobs ALTER COLUMN attempt_count SET DEFAULT 7",)),
        (
            "check-definition",
            (
                "ALTER TABLE jobs DROP CONSTRAINT ck_jobs_attempts",
                "ALTER TABLE jobs ADD CONSTRAINT ck_jobs_attempts CHECK (attempt_count >= -1 AND max_attempts >= 1)",
            ),
        ),
        (
            "index-predicate",
            (
                "DROP INDEX ix_jobs_active_lease",
                "CREATE INDEX ix_jobs_active_lease ON jobs (lease_expires_at, id) WHERE status = 'running'",
            ),
        ),
    ],
)
async def test_final_schema_drift_fails_closed_across_all_entrypoints(
    postgres_database_url: str,
    drift_kind: str,
    mutation_sql: tuple[str, ...],
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with engine.begin() as connection:
            for statement in mutation_sql:
                await connection.execute(text(statement))
        async with engine.connect() as connection:
            before_catalog = await _schema_digest(connection)
            before_rows = await _table_row_counts(connection)
            with pytest.raises(bootstrap_module.M7RecreateRequired):
                await bootstrap_module.classify_database(connection)

        with pytest.raises(bootstrap_module.M7RecreateRequired):
            await bootstrap_module.bootstrap_schema(engine)
        with pytest.raises(PostgresSetupError, match="M7_RECREATE_REQUIRED"):
            await _bootstrap_existing(postgres_database_url)
        check = await check_postgres(postgres_database_url)
        assert check.healthy is False

        async with engine.connect() as connection:
            assert await _schema_digest(connection) == before_catalog, drift_kind
            assert await _table_row_counts(connection) == before_rows, drift_kind
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_empty_setup_converges(postgres_database_url: str) -> None:
    engines = [create_async_engine(postgres_database_url) for _ in range(2)]
    try:
        await asyncio.gather(*(bootstrap_module.bootstrap_schema(engine) for engine in engines))
        async with engines[0].connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == M7_FINAL_SCHEMA_REVISION
    finally:
        await asyncio.gather(*(engine.dispose() for engine in engines))


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_full_schema_matches_independent_metadata_database_catalog(
    postgres_database_url: str,
    postgres_admin_url: str,
) -> None:
    baseline_engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_module.bootstrap_schema(baseline_engine)
        async with baseline_engine.connect() as connection:
            baseline_catalog = await _native_relational_catalog(connection)

        async with temporary_postgres_database(postgres_admin_url) as metadata_url:
            metadata_engine = create_async_engine(metadata_url)
            try:
                async with metadata_engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                async with metadata_engine.connect() as connection:
                    metadata_catalog = await _native_relational_catalog(connection)
            finally:
                await metadata_engine.dispose()

        assert baseline_catalog == metadata_catalog
    finally:
        await baseline_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_baseline_installs_required_functions_and_triggers(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with engine.connect() as connection:
            function_rows = (
                await connection.execute(
                    text(
                        """SELECT p.proname,pg_get_functiondef(p.oid)
                        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                        WHERE n.nspname=current_schema()
                          AND p.proname=ANY(CAST(:names AS text[]))"""
                    ),
                    {"names": sorted(REQUIRED_FUNCTIONS)},
                )
            ).all()
            trigger_rows = (
                await connection.execute(
                    text(
                        """SELECT t.tgname,c.relname,p.proname,t.tgtype,
                                  pg_get_triggerdef(t.oid,true)
                        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                        JOIN pg_namespace n ON n.oid=c.relnamespace
                        JOIN pg_proc p ON p.oid=t.tgfoid
                        WHERE n.nspname=current_schema() AND NOT t.tgisinternal
                          AND t.tgname=ANY(CAST(:names AS text[]))"""
                    ),
                    {"names": sorted(REQUIRED_TRIGGERS)},
                )
            ).all()
        functions = {name: definition for name, definition in function_rows}
        assert set(functions) == REQUIRED_FUNCTIONS
        for function_name, fragment in EXPECTED_FUNCTION_FRAGMENTS.items():
            assert fragment in functions[function_name]
            assert f"FUNCTION public.{function_name}()" in functions[function_name]
        assert "deerflow.agent_hard_delete_asset_id" in functions["prevent_published_version_child_mutation"]

        triggers = {name: (table, function, event_bits, definition) for name, table, function, event_bits, definition in trigger_rows}
        assert set(triggers) == REQUIRED_TRIGGERS
        for trigger_name, identity in EXPECTED_TRIGGER_IDENTITIES.items():
            table, function, event_bits, definition = triggers[trigger_name]
            assert (table, function, event_bits) == identity
            assert f"TRIGGER {trigger_name}" in definition
            assert f"ON {table}" in definition
            assert f"EXECUTE FUNCTION {function}()" in definition
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stream_terminal_trigger_rejects_late_and_duplicate_terminal_events(
    postgres_database_url: str,
) -> None:
    bootstrap_engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_module.bootstrap_schema(bootstrap_engine)
    finally:
        await bootstrap_engine.dispose()
    seed = await seed_m4_thread_database(postgres_database_url)
    thread_id = f"m7-terminal-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await PrivateRunRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                request=PrivateRunCreate(run_id=run_id),
            )
            await session.execute(
                text(
                    """INSERT INTO thread_event_sequences
                    (project_id,owner_user_id,thread_id,high_watermark)
                    VALUES (:project,:owner,:thread,0)"""
                ),
                {
                    "project": seed.owner_a.project_id,
                    "owner": str(seed.owner_a.user_id),
                    "thread": thread_id,
                },
            )

        async def insert_event(seq: int, event_type: str) -> None:
            async with seed.engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO run_events
                        (thread_id,run_id,owner_user_id,event_type,category,content,
                         event_metadata,seq,created_at,project_id)
                        VALUES (:thread,:run,:owner,:event,'stream','',
                                '{}'::json,:seq,now(),:project)"""
                    ),
                    {
                        "thread": thread_id,
                        "run": run_id,
                        "owner": str(seed.owner_a.user_id),
                        "event": event_type,
                        "seq": seq,
                        "project": seed.owner_a.project_id,
                    },
                )

        await insert_event(1, "stream.frame")
        await insert_event(2, "stream.end")
        with pytest.raises(DBAPIError):
            await insert_event(3, "stream.frame")
        with pytest.raises(DBAPIError):
            await insert_event(4, "stream.end")
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_append_only_audit_usage_and_dead_job_ledgers_reject_mutation(
    postgres_database_url: str,
) -> None:
    bootstrap_engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_module.bootstrap_schema(bootstrap_engine)
    finally:
        await bootstrap_engine.dispose()
    seed = await seed_m4_thread_database(postgres_database_url)
    try:
        dead_job_id = uuid.uuid4()
        async with seed.engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO project_usage_ledger
                    (id,project_id,dimension,delta,bucket,source_kind,source_ref_key_id,
                     source_ref_hmac,idempotency_key,occurred_at)
                    VALUES (:id,:project,'storage_bytes',1,'lifetime','file','test',
                            :digest,:digest,now())"""
                ),
                {"id": uuid.uuid4(), "project": seed.owner_a.project_id, "digest": "1" * 64},
            )
            await connection.execute(
                text(
                    """INSERT INTO audit_logs
                    (id,actor_process,action,target_kind,target_ref_key_id,target_ref_hmac,
                     outcome,metadata_json)
                    VALUES (:id,'worker','test.action','test','test',:digest,'success','{}'::json)"""
                ),
                {"id": uuid.uuid4(), "digest": "2" * 64},
            )
            await connection.execute(
                text(
                    """INSERT INTO jobs
                    (id,job_type,project_id,idempotency_key,max_attempts)
                    VALUES (:id,'retention_purge',:project,:key,3)"""
                ),
                {
                    "id": dead_job_id,
                    "project": seed.owner_a.project_id,
                    "key": "a" * 64,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO dead_jobs
                    (job_id,project_id,job_type,attempt_count,retry_safety,public_error_code)
                    VALUES (:id,:project,'retention_purge',1,'safe','TEST_DEAD')"""
                ),
                {"id": dead_job_id, "project": seed.owner_a.project_id},
            )
        mutations = {
            "project_usage_ledger": "delta=2",
            "audit_logs": "outcome='rejected'",
            "dead_jobs": "public_error_code='MUTATED'",
        }
        for table_name, assignment in mutations.items():
            with pytest.raises(DBAPIError):
                async with seed.engine.begin() as connection:
                    await connection.execute(text(f"UPDATE {table_name} SET {assignment}"))
            with pytest.raises(DBAPIError):
                async with seed.engine.begin() as connection:
                    await connection.execute(text(f"DELETE FROM {table_name}"))

    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_updated_at_and_shared_asset_version_invariants_are_enforced(
    postgres_database_url: str,
) -> None:
    bootstrap_engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_module.bootstrap_schema(bootstrap_engine)
    finally:
        await bootstrap_engine.dispose()
    seed = await seed_m4_thread_database(postgres_database_url)
    try:
        async with seed.engine.connect() as connection:
            before = await connection.scalar(
                text("SELECT updated_at FROM projects WHERE id=:id"),
                {"id": seed.owner_a.project_id},
            )
        await asyncio.sleep(0.01)
        async with seed.engine.begin() as connection:
            await connection.execute(
                text("UPDATE projects SET display_name='Updated' WHERE id=:id"),
                {"id": seed.owner_a.project_id},
            )
        async with seed.engine.connect() as connection:
            after = await connection.scalar(
                text("SELECT updated_at FROM projects WHERE id=:id"),
                {"id": seed.owner_a.project_id},
            )
        assert before is not None and after is not None and after > before

        with pytest.raises(DBAPIError):
            async with seed.engine.begin() as connection:
                await connection.execute(
                    text(
                        """UPDATE agent_versions SET description='mutated'
                        WHERE id=(SELECT current_published_version_id FROM agents WHERE id=:agent)"""
                    ),
                    {"agent": seed.system_agent_id},
                )
        with pytest.raises(DBAPIError):
            async with seed.engine.begin() as connection:
                await connection.execute(
                    text(
                        """UPDATE agent_versions SET workflow_status='draft'
                        WHERE id=(SELECT current_published_version_id FROM agents WHERE id=:agent)"""
                    ),
                    {"agent": seed.system_agent_id},
                )

        draft_version = uuid.uuid4()
        async with seed.engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO agent_versions
                    (id,agent_id,version_number,workflow_status,description,soul,model_ref,
                     tool_groups,payload_checksum,created_by_user_id)
                    VALUES (:id,:agent,2,'draft','','draft','test-model','[]'::jsonb,
                            :checksum,:owner)"""
                ),
                {
                    "id": draft_version,
                    "agent": seed.system_agent_id,
                    "checksum": "9" * 64,
                    "owner": str(seed.owner_a.user_id),
                },
            )
        with pytest.raises(DBAPIError):
            async with seed.engine.begin() as connection:
                await connection.execute(
                    text(
                        """UPDATE project_system_agent_bindings
                        SET agent_version_id=:draft
                        WHERE project_id=:project AND system_agent_id=:agent"""
                    ),
                    {
                        "draft": draft_version,
                        "project": seed.owner_a.project_id,
                        "agent": seed.system_agent_id,
                    },
                )
    finally:
        await seed.engine.dispose()
