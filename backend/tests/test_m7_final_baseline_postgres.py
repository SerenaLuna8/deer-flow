from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import UniqueConstraint, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

import deerflow.persistence.models  # noqa: F401
from app.final_schema import M7_FINAL_SCHEMA_REVISION
from deerflow.persistence import bootstrap as bootstrap_module
from deerflow.persistence.base import Base

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
        ) catalog
    """


async def _schema_digest(connection: AsyncConnection) -> str:
    return str(await connection.scalar(text(_schema_digest_sql())))


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
async def test_baseline_matches_final_metadata_catalog(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with engine.connect() as connection:

            def inspect_catalog(sync_connection):
                inspector = sa_inspect(sync_connection)
                tables = set(inspector.get_table_names()) - {"alembic_version"}
                columns = {table: {column["name"] for column in inspector.get_columns(table)} for table in tables}
                indexes = {table: {(index["name"], bool(index["unique"]), tuple(index.get("column_names") or ())) for index in inspector.get_indexes(table) if not index.get("duplicates_constraint")} for table in tables}
                unique_constraints = {table: {(item["name"], tuple(item.get("column_names") or ())) for item in inspector.get_unique_constraints(table)} for table in tables}
                checks = {table: {item["name"] for item in inspector.get_check_constraints(table)} for table in tables}
                foreign_keys = {
                    table: {
                        (
                            tuple(item["constrained_columns"]),
                            item["referred_table"],
                            tuple(item["referred_columns"]),
                            (item.get("options") or {}).get("ondelete"),
                        )
                        for item in inspector.get_foreign_keys(table)
                    }
                    for table in tables
                }
                return tables, columns, indexes, unique_constraints, checks, foreign_keys

            tables, columns, indexes, unique_constraints, checks, foreign_keys = await connection.run_sync(inspect_catalog)

        expected_tables = set(Base.metadata.tables)
        assert tables == expected_tables
        assert columns == {name: set(table.c.keys()) for name, table in Base.metadata.tables.items()}
        for name, table in Base.metadata.tables.items():
            actual_indexes = {index_name: (unique, column_names) for index_name, unique, column_names in indexes[name]}
            expected_indexes = {index.name: index for index in table.indexes}
            assert set(actual_indexes) == set(expected_indexes)
            for index_name, expected_index in expected_indexes.items():
                actual_unique, actual_columns = actual_indexes[index_name]
                assert actual_unique is bool(expected_index.unique)
                assert len(actual_columns) == len(expected_index.expressions)
                for actual_column, expected_expression in zip(actual_columns, expected_index.expressions, strict=True):
                    if expected_expression.__class__.__name__ == "Column":
                        assert actual_column == expected_expression.name
                    else:
                        underlying_column = getattr(expected_expression, "element", None)
                        if underlying_column is not None and underlying_column.__class__.__name__ == "Column":
                            assert actual_column == underlying_column.name
                        else:
                            assert actual_column is None
            expected_unique_constraints = {
                (
                    constraint.name or f"{name}_{'_'.join(column.name for column in constraint.columns)}_key",
                    tuple(column.name for column in constraint.columns),
                )
                for constraint in table.constraints
                if isinstance(constraint, UniqueConstraint)
            }
            assert unique_constraints[name] == expected_unique_constraints
            expected_checks = {constraint.name for constraint in table.constraints if constraint.__class__.__name__ == "CheckConstraint"}
            assert checks[name] == expected_checks
            expected_foreign_keys = {
                (
                    tuple(element.parent.name for element in constraint.elements),
                    constraint.elements[0].column.table.name,
                    tuple(element.column.name for element in constraint.elements),
                    constraint.ondelete,
                )
                for constraint in table.foreign_key_constraints
            }
            assert foreign_keys[name] == expected_foreign_keys
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_baseline_installs_required_functions_and_triggers(postgres_database_url: str) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_module.bootstrap_schema(engine)
        async with engine.connect() as connection:
            functions = set(
                (
                    await connection.execute(
                        text("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=current_schema() AND p.proname = ANY(:names)"),
                        {"names": sorted(REQUIRED_FUNCTIONS)},
                    )
                ).scalars()
            )
            triggers = set(
                (
                    await connection.execute(
                        text("SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema() AND NOT t.tgisinternal AND tgname = ANY(:names)"),
                        {"names": sorted(REQUIRED_TRIGGERS)},
                    )
                ).scalars()
            )
        assert functions == REQUIRED_FUNCTIONS
        assert triggers == REQUIRED_TRIGGERS
    finally:
        await engine.dispose()
