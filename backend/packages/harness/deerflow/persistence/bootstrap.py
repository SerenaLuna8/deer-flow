"""Atomic full-schema initialization and read-only runtime validation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

import deerflow.persistence.models  # noqa: F401 -- populate final metadata
from deerflow.persistence.final_schema_contract import (
    FINAL_APP_TABLES,
    LANGGRAPH_TABLES,
    inventory_is_m7_allowed,
    inventory_user_schema_objects,
    verify_m7_catalog,
)

CURRENT_SCHEMA_REVISION = "full_schema_v1"
# Current-schema alias retained for the M7 readiness contract.
M7_FINAL_SCHEMA_REVISION = CURRENT_SCHEMA_REVISION

_FULL_SCHEMA_PATH = Path(__file__).resolve().parent / "full_schema.sql"
_PG_LOCK_KEY = 0x0DEE_12F1_0BEE_3682
_PG_LOCK_POLL_SECONDS = 0.1

_LANGGRAPH_TABLES = LANGGRAPH_TABLES
_FINAL_APP_TABLES = FINAL_APP_TABLES
_FINAL_ALLOWED_RELATIONS = _FINAL_APP_TABLES | _LANGGRAPH_TABLES | {"alembic_version"}


class M7RecreateRequired(RuntimeError):
    """The existing database is not the exact supported full-schema snapshot."""

    code = "M7_RECREATE_REQUIRED"

    def __init__(self) -> None:
        super().__init__("M7_RECREATE_REQUIRED: nonempty database is not the exact full_schema_v1 catalog and must be recreated")


class SchemaSetupRequired(RuntimeError):
    """An empty database must be initialized explicitly before runtime starts."""

    code = "DATABASE_SETUP_REQUIRED"

    def __init__(self) -> None:
        super().__init__("DATABASE_SETUP_REQUIRED: run `make setup-db` before starting ActWeave")


async def list_user_relations(connection: AsyncConnection) -> frozenset[str]:
    rows = await connection.execute(
        text(
            """SELECT c.relname
               FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = current_schema()
                 AND c.relkind IN ('r', 'p', 'v', 'm', 'f')"""
        )
    )
    return frozenset(str(value) for value in rows.scalars())


async def classify_database(
    connection: AsyncConnection,
) -> Literal["empty", "current"]:
    """Classify a database without mutation.

    Only an empty schema or the exact ``full_schema_v1`` catalog is accepted.
    Every other nonempty schema requires explicit recreation.
    """

    objects = await inventory_user_schema_objects(connection)
    if not objects:
        return "empty"
    if not inventory_is_m7_allowed(objects) or "relation:r:alembic_version" not in objects:
        raise M7RecreateRequired()

    markers = tuple(str(value) for value in (await connection.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num"))).scalars())
    if markers != (CURRENT_SCHEMA_REVISION,) or not await verify_m7_catalog(connection):
        raise M7RecreateRequired()
    return "current"


@asynccontextmanager
async def _postgres_lock(engine: AsyncEngine) -> AsyncIterator[None]:
    """Serialize classification and upgrade on a dedicated PostgreSQL session."""

    lock_engine = create_async_engine(
        engine.url,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with lock_engine.connect() as connection:
            await connection.execute(text("SET statement_timeout = 0"))
            await connection.execute(text("SET idle_in_transaction_session_timeout = 0"))
            idle_session_timeout = await connection.scalar(text("SELECT current_setting('idle_session_timeout', true)"))
            if idle_session_timeout is not None:
                await connection.execute(text("SET idle_session_timeout = 0"))
            while not await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": _PG_LOCK_KEY}):
                await asyncio.sleep(_PG_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                try:
                    await connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": _PG_LOCK_KEY})
                except Exception:
                    # Closing this dedicated session is the fail-safe unlock.
                    pass
    finally:
        await lock_engine.dispose()


def _read_full_schema_sql() -> str:
    payload = _FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    expected_marker = "INSERT INTO alembic_version (version_num) VALUES ('full_schema_v1');"
    if not payload.startswith("BEGIN;\n") or not payload.rstrip().endswith("COMMIT;") or payload.count(expected_marker) != 1 or "-- Running upgrade" in payload or "UPDATE alembic_version" in payload:
        raise RuntimeError("full schema SQL snapshot is invalid")
    return payload


async def _install_full_schema(engine: AsyncEngine) -> None:
    """Execute the complete snapshot as one PostgreSQL transaction."""

    payload = await asyncio.to_thread(_read_full_schema_sql)
    async with engine.connect() as connection:
        raw_connection = await connection.get_raw_connection()
        driver_connection = raw_connection.driver_connection
        try:
            # asyncpg's no-argument execute path uses PostgreSQL's simple-query
            # protocol, which accepts this complete BEGIN/COMMIT SQL batch.
            await driver_connection.execute(payload)
        except BaseException:
            try:
                await driver_connection.execute("ROLLBACK")
            except Exception:
                # Closing the owning SQLAlchemy connection is the final
                # rollback/cleanup boundary for a failed initialization.
                pass
            raise


async def bootstrap_schema(engine: AsyncEngine) -> None:
    """Install an empty schema or verify the exact full-schema marker."""

    if not isinstance(engine, AsyncEngine):
        raise TypeError("bootstrap_schema() requires an AsyncEngine")
    async with _postgres_lock(engine):
        async with engine.connect() as connection:
            state = await classify_database(connection)
        if state == "empty":
            await _install_full_schema(engine)
        async with engine.connect() as connection:
            if await classify_database(connection) != "current":
                raise M7RecreateRequired()


async def validate_schema(engine: AsyncEngine) -> None:
    """Read-only runtime gate; never invokes Alembic or executes DDL."""

    if not isinstance(engine, AsyncEngine):
        raise TypeError("validate_schema() requires an AsyncEngine")
    async with engine.connect() as connection:
        state = await classify_database(connection)
    if state == "current":
        return
    raise SchemaSetupRequired()


__all__ = [
    "CURRENT_SCHEMA_REVISION",
    "M7RecreateRequired",
    "M7_FINAL_SCHEMA_REVISION",
    "SchemaSetupRequired",
    "bootstrap_schema",
    "classify_database",
    "list_user_relations",
    "validate_schema",
]
