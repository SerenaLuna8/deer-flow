from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from deerflow.persistence.bootstrap import (
    CURRENT_SCHEMA_REVISION,
    M7RecreateRequired,
    bootstrap_schema,
    classify_database,
)
from deerflow.persistence.final_schema_contract import (
    COMMENTED_ROOT_TABLES,
    LANGGRAPH_TABLES,
)
from scripts import setup_postgres

BACKEND_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = BACKEND_ROOT / "migrations" / "baseline" / "full_schema_v5.sql"

pytestmark = pytest.mark.postgres


def _baseline_sql() -> str:
    lines = BASELINE_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(lines[next(i for i, line in enumerate(lines) if not line.startswith("--")) :])


async def _execute_sql_batch(database_url: str, payload: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            raw_connection = await connection.get_raw_connection()
            await raw_connection.driver_connection.execute(payload)
    finally:
        await engine.dispose()


def _upgrade_sync(database_url: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    config.attributes["sqlalchemy_url"] = database_url
    command.upgrade(config, revision)


async def _missing_comments(connection, tables: set[str] | frozenset[str]):
    table_rows = tuple(
        await connection.execute(
            text(
                """SELECT relation.relname
                     FROM pg_class relation
                     JOIN pg_namespace namespace
                       ON namespace.oid=relation.relnamespace
                    WHERE namespace.nspname=current_schema()
                      AND relation.relname=ANY(CAST(:tables AS text[]))
                      AND NULLIF(btrim(obj_description(relation.oid, 'pg_class')), '') IS NULL
                    ORDER BY relation.relname"""
            ),
            {"tables": sorted(tables)},
        )
    )
    column_rows = tuple(
        await connection.execute(
            text(
                """SELECT relation.relname, attribute.attname
                     FROM pg_class relation
                     JOIN pg_namespace namespace
                       ON namespace.oid=relation.relnamespace
                     JOIN pg_attribute attribute
                       ON attribute.attrelid=relation.oid
                    WHERE namespace.nspname=current_schema()
                      AND relation.relname=ANY(CAST(:tables AS text[]))
                      AND attribute.attnum > 0
                      AND NOT attribute.attisdropped
                      AND NULLIF(btrim(col_description(relation.oid, attribute.attnum)), '') IS NULL
                    ORDER BY relation.relname, attribute.attnum"""
            ),
            {"tables": sorted(tables)},
        )
    )
    return table_rows, column_rows


@pytest.mark.asyncio
async def test_fresh_schema_langgraph_and_future_partition_have_comments(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        await bootstrap_schema(engine)
        await setup_postgres._bootstrap_langgraph_schemas(postgres_database_url)
        async with engine.begin() as connection:
            assert await _missing_comments(connection, COMMENTED_ROOT_TABLES) == ((), ())
            assert await _missing_comments(connection, LANGGRAPH_TABLES) == ((), ())
            partition = await connection.scalar(
                text("SELECT ensure_run_events_month_partition(:target)"),
                {"target": datetime(2037, 4, 1, tzinfo=UTC)},
            )
            assert partition == "run_events_p203704"
            parent_table_comment = await connection.scalar(text("SELECT obj_description('run_events'::regclass, 'pg_class')"))
            child_table_comment = await connection.scalar(text("SELECT obj_description('run_events_p203704'::regclass, 'pg_class')"))
            assert child_table_comment == parent_table_comment
            mismatches = tuple(
                await connection.execute(
                    text(
                        """SELECT child.attname
                             FROM pg_attribute child
                             JOIN pg_attribute parent
                               ON parent.attrelid='run_events'::regclass
                              AND parent.attname=child.attname
                            WHERE child.attrelid='run_events_p203704'::regclass
                              AND child.attnum > 0
                              AND NOT child.attisdropped
                              AND col_description(child.attrelid, child.attnum)
                                  IS DISTINCT FROM
                                  col_description(parent.attrelid, parent.attnum)"""
                    )
                )
            )
            assert mismatches == ()
        async with engine.connect() as connection:
            assert await classify_database(connection) == "current"
            await connection.execute(text("COMMENT ON COLUMN checkpoints.metadata IS 'drifted'"))
            await connection.commit()
            with pytest.raises(M7RecreateRequired):
                await classify_database(connection)
        await setup_postgres._bootstrap_langgraph_schemas(postgres_database_url)
        async with engine.connect() as connection:
            assert await classify_database(connection) == "current"
            await connection.execute(text("COMMENT ON COLUMN users.email IS NULL"))
            await connection.commit()
            with pytest.raises(M7RecreateRequired):
                await classify_database(connection)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_v9_upgrade_preserves_rows_and_backfills_all_comments(
    postgres_database_url: str,
) -> None:
    await _execute_sql_batch(postgres_database_url, _baseline_sql())
    await asyncio.to_thread(_upgrade_sync, postgres_database_url, "full_schema_v9")
    connection_url = setup_postgres._asyncpg_url(postgres_database_url)
    async with setup_postgres.AsyncPostgresSaver.from_conn_string(connection_url) as saver:
        await saver.setup()
    async with setup_postgres.AsyncPostgresStore.from_conn_string(connection_url) as store:
        await store.setup()
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            assert await _missing_comments(connection, LANGGRAPH_TABLES) != ((), ())
            await connection.execute(
                text(
                    """INSERT INTO project_invitation_rate_limits
                       (key_hash, failure_count, window_started_at, expires_at, updated_at)
                       VALUES (:key_hash, 1, now(), now() + interval '1 hour', now())"""
                ),
                {"key_hash": "a" * 64},
            )
    finally:
        await engine.dispose()

    await asyncio.to_thread(_upgrade_sync, postgres_database_url, "head")
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == CURRENT_SCHEMA_REVISION == "full_schema_v17"
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM project_invitation_rate_limits WHERE key_hash=:key_hash"),
                    {"key_hash": "a" * 64},
                )
                == 1
            )
            assert await _missing_comments(connection, COMMENTED_ROOT_TABLES) == ((), ())
            assert await _missing_comments(connection, LANGGRAPH_TABLES) == ((), ())
            partition_tables = frozenset(
                str(name)
                for name in (
                    await connection.execute(
                        text(
                            """SELECT child.relname
                                 FROM pg_inherits inheritance
                                 JOIN pg_class parent ON parent.oid=inheritance.inhparent
                                 JOIN pg_class child ON child.oid=inheritance.inhrelid
                                WHERE parent.oid='run_events'::regclass"""
                        )
                    )
                ).scalars()
            )
            assert partition_tables
            assert await _missing_comments(connection, partition_tables) == ((), ())
            assert await classify_database(connection) == "current"
    finally:
        await engine.dispose()
