from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from deerflow.persistence.bootstrap import (
    SchemaRecreateRequired,
    bootstrap_schema,
    classify_database,
)
from deerflow.persistence.final_schema_contract import (
    COMMENTED_ROOT_TABLES,
    LANGGRAPH_TABLES,
)
from scripts import setup_postgres

pytestmark = pytest.mark.postgres


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
            with pytest.raises(SchemaRecreateRequired):
                await classify_database(connection)
        await setup_postgres._bootstrap_langgraph_schemas(postgres_database_url)
        async with engine.connect() as connection:
            assert await classify_database(connection) == "current"
            await connection.execute(text("COMMENT ON COLUMN users.email IS NULL"))
            await connection.commit()
            with pytest.raises(SchemaRecreateRequired):
                await classify_database(connection)
    finally:
        await engine.dispose()
