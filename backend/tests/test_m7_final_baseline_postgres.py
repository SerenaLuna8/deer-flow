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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, async_sessionmaker, create_async_engine
from support.m4_private_threads import seed_m4_thread_database

import deerflow.persistence.models  # noqa: F401
from app.final_schema import M7_FINAL_SCHEMA_REVISION
from app.gateway.auth.sessions import generate_session_id, hash_session_id
from app.private_work.asset_runtime import PrivateAssetRuntime
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.context import ProjectContext
from app.shared_assets.agent_service import AgentService
from app.shared_assets.models import AgentPayload, SkillArchiveFile
from app.shared_assets.skill_service import SkillService
from deerflow.persistence import bootstrap as bootstrap_module
from deerflow.persistence.auth_sessions import AuthSessionRepository, AuthSessionRow
from deerflow.persistence.base import Base
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.persistence.user.model import UserRow
from scripts.check_postgres import check_postgres
from scripts.setup_postgres import PostgresSetupError, _bootstrap_existing

BASELINE_REVISION = "0001_project_saas_baseline"
CURRENT_REVISION = "0002_project_skill_hard_delete"
FROZEN_BASELINE_SHA256 = "a2239e89966891c13d75a307d54deec2e45f03eb19b10ac8f3bf06d2ffb3eb71"

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
    "enforce_scheduled_task_agent_project",
    "enforce_shared_asset_version_state_transition",
    "enforce_stream_terminal_invariant",
    "ensure_system_binding_published_version",
    "prevent_bound_published_version_downgrade",
    "prevent_published_version_child_mutation",
    "prevent_shared_asset_version_payload_update",
    "reject_m7_append_only_mutation",
    "set_m7_updated_at",
}
REQUIRED_TRIGGERS = {
    "trg_audit_logs_append_only",
    "trg_dead_jobs_append_only",
    "trg_project_usage_ledger_append_only",
    "trg_run_events_stream_terminal",
    "trg_scheduled_tasks_updated_at",
}
EXPECTED_FUNCTION_FRAGMENTS = {
    "bump_asset_catalog_generation": "generation = asset_catalog_state.generation + 1",
    "enforce_scheduled_task_agent_project": "project Agent must belong to the scheduled task project",
    "enforce_shared_asset_version_state_transition": "invalid shared asset version workflow transition",
    "enforce_stream_terminal_invariant": "stream event cannot follow terminal event",
    "ensure_system_binding_published_version": "system binding requires published version",
    "prevent_bound_published_version_downgrade": "bound published version cannot change workflow status",
    "prevent_published_version_child_mutation": "published version child rows are immutable",
    "prevent_shared_asset_version_payload_update": "shared asset version payload is immutable",
    "reject_m7_append_only_mutation": "M7 append-only rows cannot be updated or deleted",
    "set_m7_updated_at": "NEW.updated_at := now()",
}
EXPECTED_TRIGGER_IDENTITIES = {
    "trg_audit_logs_append_only": ("audit_logs", "reject_m7_append_only_mutation", 27),
    "trg_dead_jobs_append_only": ("dead_jobs", "reject_m7_append_only_mutation", 27),
    "trg_project_usage_ledger_append_only": ("project_usage_ledger", "reject_m7_append_only_mutation", 27),
    "trg_run_events_stream_terminal": ("run_events", "enforce_stream_terminal_invariant", 7),
    "trg_scheduled_tasks_updated_at": ("scheduled_tasks", "set_m7_updated_at", 19),
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


def _versions_dir() -> Path:
    return Path(bootstrap_module.__file__).resolve().parent / "migrations" / "versions"


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


def test_migration_history_preserves_frozen_0001_and_has_one_0002_head() -> None:
    revision_files = sorted(path for path in _versions_dir().glob("*.py") if path.name != "__init__.py")
    assert [path.name for path in revision_files] == [
        "0001_project_saas_baseline.py",
        "0002_project_skill_hard_delete.py",
    ]
    assert hashlib.sha256(revision_files[0].read_bytes()).hexdigest() == FROZEN_BASELINE_SHA256

    baseline_spec = importlib.util.spec_from_file_location("m7_final_baseline", revision_files[0])
    assert baseline_spec is not None and baseline_spec.loader is not None
    baseline_module = importlib.util.module_from_spec(baseline_spec)
    baseline_spec.loader.exec_module(baseline_module)
    assert baseline_module.revision == BASELINE_REVISION
    assert baseline_module.down_revision is None

    head_spec = importlib.util.spec_from_file_location("project_skill_hard_delete", revision_files[1])
    assert head_spec is not None and head_spec.loader is not None
    head_module = importlib.util.module_from_spec(head_spec)
    head_spec.loader.exec_module(head_module)
    assert head_module.revision == CURRENT_REVISION
    assert head_module.down_revision == BASELINE_REVISION
    assert bootstrap_module._get_head_revision() == CURRENT_REVISION
    with pytest.raises(RuntimeError, match="M7 baseline downgrade is unsupported"):
        baseline_module.downgrade()
    with pytest.raises(RuntimeError, match="forward-only schema downgrade is unsupported"):
        head_module.downgrade()


def test_final_metadata_and_contract_have_no_staged_relations() -> None:
    assert not (LEGACY_RELATIONS & set(Base.metadata.tables))
    assert not hasattr(importlib.import_module("app.final_schema"), "PRE_RESET_SCHEMA_REVISION")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_empty_database_installs_current_forward_head(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == CURRENT_REVISION
            assert await connection.scalar(text("SELECT to_regclass('user_notifications')")) == "user_notifications"
            relations = set((await connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"))).scalars())
            assert not (relations & LEGACY_RELATIONS)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_exact_0001_ancestor_requires_and_accepts_explicit_migration(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await asyncio.to_thread(
            bootstrap_module._upgrade,
            bootstrap_module._get_alembic_config(engine),
            BASELINE_REVISION,
        )
        async with engine.connect() as connection:
            assert await bootstrap_module.classify_database(connection) == "upgradeable"
        with pytest.raises(bootstrap_module.SchemaMigrationRequired):
            await bootstrap_module.validate_schema(engine)
        with pytest.raises(bootstrap_module.SchemaMigrationRequired):
            await bootstrap_module.bootstrap_schema(engine)

        await bootstrap_module.migrate_schema(engine)

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == CURRENT_REVISION
            assert await bootstrap_module.classify_database(connection) == "current"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_0001_skill_and_run_snapshot_remain_readable_and_materializable_after_0002(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    seed = None
    runtime = None
    try:
        await asyncio.to_thread(
            bootstrap_module._upgrade,
            bootstrap_module._get_alembic_config(engine),
            BASELINE_REVISION,
        )
        seed = await seed_m4_thread_database(postgres_database_url)
        factory = seed.factory
        skill_id = uuid.uuid4()
        skill_version_id = uuid.uuid4()
        agent_id = uuid.uuid4()
        agent_version_id = uuid.uuid4()
        skill_content = b"---\nname: v1-migration-skill\ndescription: Existing revision 0001 Skill\n---\n\nKeep this package executable after migration.\n"
        skill_checksum = _v1_skill_checksum("SKILL.md", skill_content)
        agent_payload = AgentPayload(
            description="Existing revision 0001 Agent",
            soul="Use the exact persisted Skill.",
            model_ref="test-model",
            tool_groups=(),
            skill_version_ids=(skill_version_id,),
            mcp_version_ids=(),
        )
        agent_checksum = AgentService._payload_checksum(agent_payload)

        async with factory() as session, session.begin():
            skill = SkillRow(
                id=skill_id,
                scope="project",
                project_id=seed.owner_a.project_id,
                slug="v1-migration-skill",
                display_name="V1 Migration Skill",
                created_by_user_id=str(seed.owner_a.user_id),
            )
            session.add(skill)
            await session.flush()
            skill_version = SkillVersionRow(
                id=skill_version_id,
                skill_id=skill_id,
                version_number=1,
                workflow_status="draft",
                description="Existing revision 0001 Skill",
                frontmatter={
                    "name": "v1-migration-skill",
                    "description": "Existing revision 0001 Skill",
                },
                compatibility=None,
                secret_requirements=[],
                scan_decision="allow",
                scan_summary={},
                payload_checksum=skill_checksum,
                created_by_user_id=str(seed.owner_a.user_id),
            )
            session.add(skill_version)
            await session.flush()
            session.add(
                SkillVersionFileRow(
                    skill_version_id=skill_version_id,
                    path="SKILL.md",
                    media_type="text/markdown",
                    size_bytes=len(skill_content),
                    sha256=hashlib.sha256(skill_content).hexdigest(),
                    content=skill_content,
                )
            )
            await session.flush()
            skill_version.workflow_status = "published"
            skill.current_published_version_id = skill_version_id

            agent = AgentRow(
                id=agent_id,
                scope="project",
                project_id=seed.owner_a.project_id,
                slug="v1-migration-agent",
                display_name="V1 Migration Agent",
                created_by_user_id=str(seed.owner_a.user_id),
            )
            session.add(agent)
            await session.flush()
            agent_version = AgentVersionRow(
                id=agent_version_id,
                agent_id=agent_id,
                version_number=1,
                workflow_status="draft",
                description=agent_payload.description,
                soul=agent_payload.soul,
                model_ref=agent_payload.model_ref,
                tool_groups=list(agent_payload.tool_groups),
                payload_checksum=agent_checksum,
                created_by_user_id=str(seed.owner_a.user_id),
            )
            session.add(agent_version)
            await session.flush()
            session.add(
                AgentVersionSkillRefRow(
                    agent_version_id=agent_version_id,
                    skill_version_id=skill_version_id,
                    sort_order=0,
                )
            )
            await session.flush()
            agent_version.workflow_status = "published"
            agent.current_published_version_id = agent_version_id
            await session.flush()

        thread_id = f"v1-migration-{uuid.uuid4().hex}"
        async with factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(agent_id, "project"),
            )
        admitted = await PrivateRunAdmissionService(factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(),
        )
        assert [item.payload_checksum for item in admitted.snapshot.assets] == [
            agent_checksum,
            skill_checksum,
        ]

        await bootstrap_module.migrate_schema(engine)

        actor = ProjectContext(
            user_id=seed.owner_a.user_id,
            project_id=seed.owner_a.project_id,
            membership_id=seed.owner_a.membership_id,
            role=seed.owner_a.role,
            capabilities=seed.owner_a.capabilities,
            membership_version=seed.owner_a.membership_version,
            request_id="req-v1-skill-after-0002",
        )
        assert await SkillService(factory).load_version_files(
            actor,
            skill_id,
            skill_version_id,
        ) == (
            SkillArchiveFile(
                path="SKILL.md",
                content=skill_content,
                media_type="text/markdown",
            ),
        )

        runtime = await PrivateAssetRuntime(factory).materialize(
            seed.owner_a,
            admitted,
        )
        assert (runtime.skill_root / "custom" / skill_id.hex / "SKILL.md").read_bytes() == skill_content
    finally:
        if runtime is not None:
            await runtime.aclose()
        if seed is not None:
            await seed.engine.dispose()
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
async def test_old_revision_is_rejected_before_any_ddl(postgres_database_url: str) -> None:
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
async def test_migration_head_matches_independent_metadata_database_catalog(
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
