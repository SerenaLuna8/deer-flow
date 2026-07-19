from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
from postgres_utils import temporary_postgres_database
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from support.m4_private_threads import seed_m4_thread_database

import deerflow.persistence.models  # noqa: F401
from app.final_schema import M7_FINAL_SCHEMA_REVISION
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.persistence import bootstrap as bootstrap_module
from deerflow.persistence.base import Base
from scripts.check_postgres import check_postgres
from scripts.setup_postgres import PostgresSetupError, _bootstrap_existing

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
    "trg_deletion_tombstones_append_only",
    "trg_project_usage_ledger_append_only",
    "trg_restore_proofs_append_only",
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
    "trg_deletion_tombstones_append_only": ("deletion_tombstones", "reject_m7_append_only_mutation", 27),
    "trg_project_usage_ledger_append_only": ("project_usage_ledger", "reject_m7_append_only_mutation", 27),
    "trg_restore_proofs_append_only": ("restore_proofs", "reject_m7_append_only_mutation", 27),
    "trg_run_events_stream_terminal": ("run_events", "enforce_stream_terminal_invariant", 7),
    "trg_scheduled_tasks_updated_at": ("scheduled_tasks", "set_m7_updated_at", 19),
}
EXPECTED_APP_SEQUENCE_OWNERS = {
    ("deletion_tombstones_journal_sequence_seq", "deletion_tombstones"),
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


def _versions_dir() -> Path:
    return Path(bootstrap_module.__file__).resolve().parent / "migrations" / "versions"


def _schema_digest_sql() -> str:
    return """
        SELECT md5(COALESCE(string_agg(item, E'\n' ORDER BY item), ''))
        FROM (
            SELECT 'r:' || c.relkind || ':' || n.nspname || ':' || c.relname AS item
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
            UNION ALL
            SELECT 'a:' || c.relname || ':' || a.attnum || ':' || a.attname || ':' ||
                   pg_catalog.format_type(a.atttypid, a.atttypmod) || ':' || a.attnotnull
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
            SELECT 't:' || t.typname || ':' || t.typtype || ':' ||
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


def test_m7_has_one_forward_only_revision() -> None:
    revision_files = sorted(path for path in _versions_dir().glob("*.py") if path.name != "__init__.py")
    assert [path.name for path in revision_files] == ["0001_project_saas_baseline.py"]

    spec = importlib.util.spec_from_file_location("m7_final_baseline", revision_files[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == M7_FINAL_SCHEMA_REVISION
    assert module.down_revision is None
    with pytest.raises(RuntimeError, match="M7 baseline downgrade is unsupported"):
        module.downgrade()


def test_final_metadata_and_contract_have_no_staged_relations() -> None:
    assert not (LEGACY_RELATIONS & set(Base.metadata.tables))
    assert not hasattr(importlib.import_module("app.final_schema"), "PRE_RESET_SCHEMA_REVISION")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_empty_database_installs_exact_m7_baseline(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == M7_FINAL_SCHEMA_REVISION
            relations = set((await connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"))).scalars())
            assert not (relations & LEGACY_RELATIONS)
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
            assert await bootstrap_module.classify_database(connection) == "m7"

        await _bootstrap_existing(postgres_database_url)
        async with engine.connect() as connection:
            full_sequences, full_indexes = await _sequence_index_owners(connection)
            assert full_sequences == EXPECTED_APP_SEQUENCE_OWNERS
            assert {identity for identity in full_indexes if identity[1] in langgraph_tables} == EXPECTED_LANGGRAPH_INDEX_OWNERS
            assert await bootstrap_module.classify_database(connection) == "m7"
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
async def test_baseline_matches_independent_metadata_database_catalog(
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
async def test_append_only_audit_ledger_tombstone_and_restore_proof_reject_mutation(
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
            await connection.execute(
                text(
                    """INSERT INTO deletion_tombstones
                    (journal_sequence,ciphertext_digest,record_digest,resource_kind,
                     resource_ref_key_id,resource_ref_hmac)
                    VALUES (1,:cipher,:record,'file','test',:ref)"""
                ),
                {"cipher": "3" * 64, "record": "4" * 64, "ref": "5" * 64},
            )
            await connection.execute(
                text(
                    """INSERT INTO restore_proofs
                    (id,archive_id,archive_digest,target_database_ref_key_id,
                     target_database_ref_hmac,schema_revision,archive_tombstone_sequence,
                     replayed_through_sequence,journal_id,final_journal_head_digest)
                    VALUES (:id,:archive,:digest,'test',:ref,:revision,0,0,:journal,:head)"""
                ),
                {
                    "id": uuid.uuid4(),
                    "archive": uuid.uuid4(),
                    "digest": "6" * 64,
                    "ref": "7" * 64,
                    "revision": M7_FINAL_SCHEMA_REVISION,
                    "journal": uuid.uuid4(),
                    "head": "8" * 64,
                },
            )

        mutations = {
            "project_usage_ledger": "delta=2",
            "audit_logs": "outcome='rejected'",
            "dead_jobs": "public_error_code='MUTATED'",
            "deletion_tombstones": "purge_status='purged'",
            "restore_proofs": "probes_complete=true",
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
