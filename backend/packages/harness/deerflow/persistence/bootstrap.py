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

# Ordered incremental migration chain, root -> head. Every released revision
# id lives here; ``backend/tests/test_schema_migration_parity.py`` pins this
# tuple to the actual scripts under ``backend/migrations/versions``. The head
# is the only marker the runtime accepts; any known ancestor classifies as
# "behind" (explicit ``make upgrade-db`` required), and anything else stays
# fail-closed.
KNOWN_CHAIN_REVISIONS: tuple[str, ...] = (
    "full_schema_v5",
    "full_schema_v6",
    "full_schema_v7",
    "full_schema_v8",
    "full_schema_v9",
    "full_schema_v10",
    "full_schema_v11",
    "full_schema_v12",
    "full_schema_v13",
    "full_schema_v14",
    "full_schema_v15",
    "full_schema_v16",
    "full_schema_v17",
)

# The migration-chain head revision id. ``full_schema.sql`` stamps exactly
# this marker, so a fresh install is always already at head.
CURRENT_SCHEMA_REVISION = KNOWN_CHAIN_REVISIONS[-1]
# Current-schema alias retained for the M7 readiness contract.
M7_FINAL_SCHEMA_REVISION = CURRENT_SCHEMA_REVISION

_FULL_SCHEMA_PATH = Path(__file__).resolve().parent / "full_schema.sql"
SCHEMA_MUTATION_LOCK_KEY = 0x0DEE_12F1_0BEE_3682
_PG_LOCK_POLL_SECONDS = 0.1

_LANGGRAPH_TABLES = LANGGRAPH_TABLES
_FINAL_APP_TABLES = FINAL_APP_TABLES
_FINAL_ALLOWED_RELATIONS = _FINAL_APP_TABLES | _LANGGRAPH_TABLES | {"alembic_version"}


class M7RecreateRequired(RuntimeError):
    """The existing database is not the exact supported full-schema snapshot."""

    code = "M7_RECREATE_REQUIRED"

    def __init__(self) -> None:
        super().__init__(f"M7_RECREATE_REQUIRED: nonempty database is not the exact {CURRENT_SCHEMA_REVISION} catalog and must be recreated")


class SchemaSetupRequired(RuntimeError):
    """An empty database must be initialized explicitly before runtime starts."""

    code = "DATABASE_SETUP_REQUIRED"

    def __init__(self) -> None:
        super().__init__("DATABASE_SETUP_REQUIRED: run `make setup-db` before starting ActWeave")


class SchemaUpgradeRequired(RuntimeError):
    """A behind database needs an explicit, operator-driven migration run."""

    code = "DATABASE_UPGRADE_REQUIRED"

    def __init__(self, current_marker: str) -> None:
        super().__init__(f"DATABASE_UPGRADE_REQUIRED: database is at {current_marker} but the current chain head is {CURRENT_SCHEMA_REVISION}; back up the database and run `make upgrade-db`")


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
) -> Literal["empty", "current", "behind"]:
    """Classify a database without mutation.

    Three accepted states: empty (installable), the exact chain-head catalog
    (current), or a database stamped with a known ancestor revision (behind —
    upgradable only through explicit ``make upgrade-db``). Every other
    nonempty schema stays fail-closed and requires explicit recreation. A
    behind database deliberately skips the head catalog contract: its older
    catalog is verified by the migration run itself, whose post-upgrade check
    re-enters the "current" branch here.
    """

    objects = await inventory_user_schema_objects(connection)
    if not objects:
        return "empty"
    if "relation:r:alembic_version" not in objects:
        raise M7RecreateRequired()

    markers = tuple(str(value) for value in (await connection.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num"))).scalars())
    if len(markers) != 1 or markers[0] not in KNOWN_CHAIN_REVISIONS:
        raise M7RecreateRequired()
    if markers[0] != CURRENT_SCHEMA_REVISION:
        return "behind"
    if not inventory_is_m7_allowed(objects) or not await verify_m7_catalog(connection):
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
            while not await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": SCHEMA_MUTATION_LOCK_KEY},
            ):
                await asyncio.sleep(_PG_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                try:
                    await connection.scalar(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": SCHEMA_MUTATION_LOCK_KEY},
                    )
                except Exception:
                    # Closing this dedicated session is the fail-safe unlock.
                    pass
    finally:
        await lock_engine.dispose()


def _read_full_schema_sql() -> str:
    payload = _FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    expected_marker = f"INSERT INTO alembic_version (version_num) VALUES ('{CURRENT_SCHEMA_REVISION}');"
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
        if state == "behind":
            # Setup never migrates: `make upgrade-db` is the only upgrade path.
            raise SchemaUpgradeRequired(await _single_marker(engine))
        if state == "empty":
            await _install_full_schema(engine)
        async with engine.connect() as connection:
            if await classify_database(connection) != "current":
                raise M7RecreateRequired()


async def _single_marker(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        marker = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    return str(marker)


async def validate_schema(engine: AsyncEngine) -> None:
    """Read-only runtime gate; never invokes Alembic or executes DDL."""

    if not isinstance(engine, AsyncEngine):
        raise TypeError("validate_schema() requires an AsyncEngine")
    async with engine.connect() as connection:
        state = await classify_database(connection)
    if state == "current":
        return
    if state == "behind":
        raise SchemaUpgradeRequired(await _single_marker(engine))
    raise SchemaSetupRequired()


__all__ = [
    "CURRENT_SCHEMA_REVISION",
    "KNOWN_CHAIN_REVISIONS",
    "M7RecreateRequired",
    "M7_FINAL_SCHEMA_REVISION",
    "SCHEMA_MUTATION_LOCK_KEY",
    "SchemaSetupRequired",
    "SchemaUpgradeRequired",
    "bootstrap_schema",
    "classify_database",
    "list_user_relations",
    "validate_schema",
]
