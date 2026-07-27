"""Explicit PostgreSQL migrations and read-only runtime schema validation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

import deerflow.persistence.models  # noqa: F401 -- populate final metadata
from deerflow.persistence.final_schema_contract import (
    FINAL_APP_TABLES,
    LANGGRAPH_TABLES,
    inventory_is_m7_allowed,
    inventory_user_schema_objects,
    verify_m7_baseline_catalog,
    verify_m7_catalog,
)

BASELINE_SCHEMA_REVISION = "0001_project_saas_baseline"
CURRENT_SCHEMA_REVISION = "0002_skill_design_builder"
# Current-schema alias retained for the M7 readiness contract.
M7_FINAL_SCHEMA_REVISION = CURRENT_SCHEMA_REVISION

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_HEAD_REVISION: str | None = None
_PG_LOCK_KEY = 0x0DEE_12F1_0BEE_3682
_PG_LOCK_POLL_SECONDS = 0.1

_LANGGRAPH_TABLES = LANGGRAPH_TABLES
_FINAL_APP_TABLES = FINAL_APP_TABLES
_FINAL_ALLOWED_RELATIONS = _FINAL_APP_TABLES | _LANGGRAPH_TABLES | {"alembic_version"}


class M7RecreateRequired(RuntimeError):
    """The existing database is not a recognized, safely migratable schema."""

    code = "M7_RECREATE_REQUIRED"

    def __init__(self) -> None:
        super().__init__("M7_RECREATE_REQUIRED: unknown revision or schema catalog drift requires manual operator review")


class SchemaMigrationRequired(RuntimeError):
    """A known older schema must be upgraded explicitly before runtime starts."""

    code = "DATABASE_MIGRATION_REQUIRED"

    def __init__(self) -> None:
        super().__init__("DATABASE_MIGRATION_REQUIRED: run `make migrate-db` before starting DeerFlow")


class SchemaSetupRequired(RuntimeError):
    """An empty database must be initialized explicitly before runtime starts."""

    code = "DATABASE_SETUP_REQUIRED"

    def __init__(self) -> None:
        super().__init__("DATABASE_SETUP_REQUIRED: run `make setup-db` before starting DeerFlow")


def _escape_url_for_alembic(url: str) -> str:
    return url.replace("%", "%%")


def _alembic_safe_url(engine: AsyncEngine) -> str:
    return _escape_url_for_alembic(engine.url.render_as_string(hide_password=False))


def _get_alembic_config(engine_or_url: AsyncEngine | str) -> AlembicConfig:
    config = AlembicConfig(str(_MIGRATIONS_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    url = _alembic_safe_url(engine_or_url) if hasattr(engine_or_url, "url") else _escape_url_for_alembic(str(engine_or_url))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _get_head_revision() -> str:
    global _HEAD_REVISION
    if _HEAD_REVISION is None:
        config = AlembicConfig()
        config.set_main_option("script_location", str(_MIGRATIONS_DIR))
        heads = ScriptDirectory.from_config(config).get_heads()
        if heads != [CURRENT_SCHEMA_REVISION]:
            raise RuntimeError("migration graph must contain exactly one declared current head")
        _HEAD_REVISION = heads[0]
    return _HEAD_REVISION


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
) -> Literal["empty", "upgradeable", "current"]:
    """Classify a database without mutation before any migration can run."""

    objects = await inventory_user_schema_objects(connection)
    if not objects:
        return "empty"
    if not inventory_is_m7_allowed(objects) or "relation:r:alembic_version" not in objects:
        raise M7RecreateRequired()

    revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    if revision == CURRENT_SCHEMA_REVISION:
        if await verify_m7_catalog(connection):
            return "current"
        raise M7RecreateRequired()
    if revision == BASELINE_SCHEMA_REVISION:
        if await verify_m7_baseline_catalog(connection):
            return "upgradeable"
        raise M7RecreateRequired()
    raise M7RecreateRequired()


def _upgrade(config: AlembicConfig, revision: str = "head") -> None:
    alembic_command.upgrade(config, revision)


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


async def migrate_schema(engine: AsyncEngine) -> None:
    """Explicitly migrate a future verified ancestor database to head."""

    if not isinstance(engine, AsyncEngine):
        raise TypeError("migrate_schema() requires an AsyncEngine")
    _get_head_revision()
    async with _postgres_lock(engine):
        async with engine.connect() as connection:
            state = await classify_database(connection)
        if state == "empty":
            raise SchemaSetupRequired()
        if state == "upgradeable":
            await _run_alembic_offload(_upgrade, _get_alembic_config(engine), "head")
        async with engine.connect() as connection:
            if await classify_database(connection) != "current":
                raise M7RecreateRequired()


async def bootstrap_schema(engine: AsyncEngine) -> None:
    """Install an empty schema or verify an already-current schema."""

    if not isinstance(engine, AsyncEngine):
        raise TypeError("bootstrap_schema() requires an AsyncEngine")
    _get_head_revision()
    async with _postgres_lock(engine):
        async with engine.connect() as connection:
            state = await classify_database(connection)
        if state == "upgradeable":
            raise SchemaMigrationRequired()
        if state == "empty":
            await _run_alembic_offload(
                _upgrade,
                _get_alembic_config(engine),
                "head",
            )
        async with engine.connect() as connection:
            if await classify_database(connection) != "current":
                raise M7RecreateRequired()


async def validate_schema(engine: AsyncEngine) -> None:
    """Read-only runtime gate; never invokes Alembic or executes DDL."""

    if not isinstance(engine, AsyncEngine):
        raise TypeError("validate_schema() requires an AsyncEngine")
    _get_head_revision()
    async with engine.connect() as connection:
        state = await classify_database(connection)
    if state == "current":
        return
    if state == "upgradeable":
        raise SchemaMigrationRequired()
    raise SchemaSetupRequired()


async def _run_alembic_offload(function, *args) -> None:
    """Keep the advisory lock until synchronous Alembic work settles on cancellation."""

    task = asyncio.create_task(asyncio.to_thread(function, *args))
    pending_cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if pending_cancellation is None:
                pending_cancellation = exc
        except Exception:
            if pending_cancellation is None:
                raise
            raise pending_cancellation
    if pending_cancellation is not None:
        raise pending_cancellation


__all__ = [
    "BASELINE_SCHEMA_REVISION",
    "CURRENT_SCHEMA_REVISION",
    "M7RecreateRequired",
    "M7_FINAL_SCHEMA_REVISION",
    "SchemaMigrationRequired",
    "SchemaSetupRequired",
    "bootstrap_schema",
    "classify_database",
    "list_user_relations",
    "migrate_schema",
    "validate_schema",
]
