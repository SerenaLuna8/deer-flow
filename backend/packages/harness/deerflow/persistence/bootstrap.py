"""Atomic full-schema initialization and read-only runtime validation."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

import deerflow.persistence.models  # noqa: F401 -- populate final metadata
from deerflow.persistence.final_schema_contract import (
    COMMENTED_ROOT_TABLES,
    FINAL_APP_TABLES,
    LANGGRAPH_TABLES,
    inventory_is_schema_v1_allowed,
    inventory_user_schema_objects,
    verify_schema_v1_catalog,
)

# Schema V1 is the current baseline. Future heads must add an explicit packaged
# migration; pre-V1 databases remain unsupported and require a new empty target.
SCHEMA_V1_REVISION = "schema_v1"
CURRENT_SCHEMA_REVISION = SCHEMA_V1_REVISION

_FULL_SCHEMA_PATH = Path(__file__).resolve().parent / "full_schema.sql"
_SCHEMA_COMMENTS_PATH = _FULL_SCHEMA_PATH.with_name("schema_comments.sql")
_SCHEMA_COMMENTS_PLACEHOLDER = "-- INCLUDE GENERATED SCHEMA COMMENTS FROM schema_comments.sql"
_SCHEMA_MARKER_INSERT = f"INSERT INTO alembic_version (version_num) VALUES ('{CURRENT_SCHEMA_REVISION}');"
_CREATE_TABLE_RE = re.compile(r"^CREATE TABLE ([a-z][a-z0-9_]*) \($")
_COLUMN_RE = re.compile(r"^ {4}([a-z][a-z0-9_]*)\s+")
_TABLE_COMMENT_RE = re.compile(
    r"^COMMENT ON TABLE ([a-z][a-z0-9_]*) IS '((?:''|[^'])*)';$",
)
_COLUMN_COMMENT_RE = re.compile(
    r"^COMMENT ON COLUMN ([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*) IS '((?:''|[^'])*)';$",
)
_SCHEMA_SHAPE_DIGEST_PREFIX = "-- Schema shape SHA-256: "
_COMMENT_STATEMENTS_DIGEST_PREFIX = "-- Comment statements SHA-256: "
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
SCHEMA_MUTATION_LOCK_KEY = 0x0DEE_12F1_0BEE_3682
_PG_LOCK_POLL_SECONDS = 0.1

_LANGGRAPH_TABLES = LANGGRAPH_TABLES
_FINAL_APP_TABLES = FINAL_APP_TABLES
_FINAL_ALLOWED_RELATIONS = _FINAL_APP_TABLES | _LANGGRAPH_TABLES | {"alembic_version"}


class SchemaRecreateRequired(RuntimeError):
    """The existing database is not the exact supported full-schema snapshot."""

    code = "SCHEMA_RECREATE_REQUIRED"

    def __init__(self) -> None:
        super().__init__(f"SCHEMA_RECREATE_REQUIRED: nonempty database is not the exact {CURRENT_SCHEMA_REVISION} catalog and must be recreated")


class SchemaUpgradeRequired(RuntimeError):
    """A packaged predecessor requires an explicit maintenance-window upgrade."""

    code = "SCHEMA_UPGRADE_REQUIRED"

    def __init__(self, revision: str) -> None:
        super().__init__(
            f"SCHEMA_UPGRADE_REQUIRED: {revision} must be upgraded to {CURRENT_SCHEMA_REVISION} with `make upgrade-db`",
        )


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
                 AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                 AND NOT EXISTS (
                     SELECT 1 FROM pg_inherits inheritance
                     WHERE inheritance.inhrelid = c.oid
                 )"""
        )
    )
    return frozenset(str(value) for value in rows.scalars())


async def classify_database(
    connection: AsyncConnection,
) -> Literal["empty", "current"]:
    """Classify a database without mutation.

    Only an empty database and the exact Schema V1 catalog are accepted.
    Every other nonempty schema stays fail-closed and must be recreated.
    """

    objects = await inventory_user_schema_objects(connection)
    if not objects:
        return "empty"
    if "relation:r:alembic_version" not in objects:
        raise SchemaRecreateRequired()

    markers = tuple(str(value) for value in (await connection.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num"))).scalars())
    if len(markers) == 1:
        from deerflow.persistence.schema_upgrade import (
            database_has_packaged_upgrade_path,
        )

        if await database_has_packaged_upgrade_path(connection, markers[0]):
            raise SchemaUpgradeRequired(markers[0])
    if len(markers) != 1 or markers[0] != CURRENT_SCHEMA_REVISION:
        raise SchemaRecreateRequired()
    if not inventory_is_schema_v1_allowed(objects) or not await verify_schema_v1_catalog(connection):
        raise SchemaRecreateRequired()
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


def _parse_static_schema_columns(schema: str) -> dict[str, tuple[str, ...]]:
    tables: dict[str, tuple[str, ...]] = {}
    active_table: str | None = None
    active_columns: list[str] = []

    for line in schema.splitlines():
        if active_table is None:
            match = _CREATE_TABLE_RE.fullmatch(line)
            if match is not None:
                active_table = match.group(1)
                active_columns = []
            continue

        if line.startswith(")"):
            if not active_columns or active_table in tables:
                raise RuntimeError("full schema SQL snapshot has invalid table structure")
            tables[active_table] = tuple(active_columns)
            active_table = None
            active_columns = []
            continue

        match = _COLUMN_RE.match(line)
        if match is not None:
            column = match.group(1)
            if column in active_columns:
                raise RuntimeError("full schema SQL snapshot has duplicate columns")
            active_columns.append(column)

    if active_table is not None or set(tables) != set(COMMENTED_ROOT_TABLES):
        raise RuntimeError("full schema SQL snapshot has invalid comment coverage")
    return tables


def _lines_digest(lines: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _schema_shape_digest(tables: dict[str, tuple[str, ...]]) -> str:
    lines = tuple(":".join((table_name, ",".join(tables[table_name]))) for table_name in sorted(tables))
    return _lines_digest(lines)


def _manifest_digest(lines: list[str], prefix: str) -> str:
    values = [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]
    if len(values) != 1 or _SHA256_RE.fullmatch(values[0]) is None:
        raise RuntimeError("schema comments SQL artifact manifest is invalid")
    return values[0]


def _validate_schema_comments_against_schema(schema: str, comments: str) -> None:
    tables = _parse_static_schema_columns(schema)
    table_comments: dict[str, str] = {}
    column_comments: dict[tuple[str, str], str] = {}
    statement_lines: list[str] = []

    for line in comments.splitlines():
        if not line or line.startswith("--"):
            continue
        statement_lines.append(line)
        table_match = _TABLE_COMMENT_RE.fullmatch(line)
        if table_match is not None:
            table_name, comment = table_match.groups()
            if table_name in table_comments:
                raise RuntimeError("schema comments SQL artifact has duplicate table comments")
            table_comments[table_name] = comment.replace("''", "'")
            continue
        column_match = _COLUMN_COMMENT_RE.fullmatch(line)
        if column_match is not None:
            table_name, column_name, comment = column_match.groups()
            identity = (table_name, column_name)
            if identity in column_comments:
                raise RuntimeError("schema comments SQL artifact has duplicate column comments")
            column_comments[identity] = comment.replace("''", "'")
            continue
        raise RuntimeError("schema comments SQL artifact contains a non-COMMENT statement")

    expected_columns = {(table_name, column_name) for table_name, columns in tables.items() for column_name in columns}
    if set(table_comments) != set(tables) or set(column_comments) != expected_columns:
        raise RuntimeError("schema comments SQL artifact does not exactly cover the schema")
    lines = comments.splitlines()
    if _manifest_digest(lines, _SCHEMA_SHAPE_DIGEST_PREFIX) != _schema_shape_digest(tables):
        raise RuntimeError("schema comments SQL artifact has a stale schema manifest")
    if _manifest_digest(lines, _COMMENT_STATEMENTS_DIGEST_PREFIX) != _lines_digest(
        tuple(statement_lines),
    ):
        raise RuntimeError("schema comments SQL artifact has a stale content manifest")


def _read_schema_comments_sql(schema: str) -> str:
    """Read and verify the generated, transaction-free COMMENT-only artifact."""

    try:
        if _SCHEMA_COMMENTS_PATH.is_symlink() or not _SCHEMA_COMMENTS_PATH.is_file():
            raise RuntimeError("schema comments SQL artifact must be a regular file")
        comments = _SCHEMA_COMMENTS_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("schema comments SQL artifact is unavailable") from exc

    lines = comments.splitlines()
    if not lines or lines[0] != "-- Generated by backend/scripts/generate_schema_comments.py; DO NOT EDIT." or comments.startswith("\ufeff") or "\x00" in comments or _SCHEMA_COMMENTS_PLACEHOLDER in comments:
        raise RuntimeError("schema comments SQL artifact is invalid")
    _validate_schema_comments_against_schema(schema, comments)
    return comments.rstrip()


def _read_full_schema_sql(*, publish_marker: bool = True) -> str:
    """Compose structural DDL and generated comments into one transaction."""

    try:
        if _FULL_SCHEMA_PATH.is_symlink() or not _FULL_SCHEMA_PATH.is_file():
            raise RuntimeError("full schema SQL snapshot must be a regular file")
        payload = _FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("full schema SQL snapshot is unavailable") from exc
    static_comment_lines = any(line.lstrip().startswith(("COMMENT ON TABLE ", "COMMENT ON COLUMN ")) for line in payload.splitlines())
    partition_creation = "SELECT ensure_run_events_month_partition(now());"
    next_partition_creation = "SELECT ensure_run_events_month_partition(now() + INTERVAL '1 month');"
    placeholder_index = payload.find(_SCHEMA_COMMENTS_PLACEHOLDER)
    post_comment_lines = tuple(line for line in payload[placeholder_index + len(_SCHEMA_COMMENTS_PLACEHOLDER) :].splitlines() if line) if placeholder_index >= 0 else ()
    if (
        payload.startswith("\ufeff")
        or "\x00" in payload
        or not payload.startswith("BEGIN;\n")
        or not payload.rstrip().endswith("COMMIT;")
        or payload.splitlines().count("BEGIN;") != 1
        or payload.splitlines().count("COMMIT;") != 1
        or payload.count(_SCHEMA_MARKER_INSERT) != 1
        or payload.count(_SCHEMA_COMMENTS_PLACEHOLDER) != 1
        or partition_creation not in payload
        or payload.index(_SCHEMA_MARKER_INSERT) > placeholder_index
        or payload.index(_SCHEMA_COMMENTS_PLACEHOLDER) > payload.index(partition_creation)
        or post_comment_lines != (partition_creation, next_partition_creation, "COMMIT;")
        or static_comment_lines
        or "-- Running upgrade" in payload
        or "UPDATE alembic_version" in payload
    ):
        raise RuntimeError("full schema SQL snapshot or schema comments placeholder is invalid")
    payload = payload.replace(
        _SCHEMA_COMMENTS_PLACEHOLDER,
        _read_schema_comments_sql(payload),
    )
    if not publish_marker:
        payload = payload.replace(
            _SCHEMA_MARKER_INSERT,
            "-- Schema V1 marker is published only after setup bootstrap completes.",
        )
    expected_marker_count = 1 if publish_marker else 0
    if payload.count(_SCHEMA_MARKER_INSERT) != expected_marker_count:
        raise RuntimeError("composed full schema SQL has an invalid completion marker")
    return payload


def validate_schema_installation_artifacts() -> None:
    """Fail closed when the two Schema V1 installation artifacts cannot compose."""

    _read_full_schema_sql()


def load_schema_comment_statements() -> tuple[str, ...]:
    """Return the validated generated COMMENT statements in deterministic order."""

    payload = _read_full_schema_sql()
    return tuple(line for line in payload.splitlines() if line.startswith(("COMMENT ON TABLE ", "COMMENT ON COLUMN ")))


async def _install_full_schema(
    engine: AsyncEngine,
    *,
    publish_marker: bool = True,
) -> None:
    """Execute the complete snapshot as one PostgreSQL transaction."""

    payload = await asyncio.to_thread(
        _read_full_schema_sql,
        publish_marker=publish_marker,
    )
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
                raise SchemaRecreateRequired()


async def _is_staged_schema(connection: AsyncConnection) -> bool:
    """Recognize the exact catalog while its completion marker is withheld."""

    objects = await inventory_user_schema_objects(connection)
    if "relation:r:alembic_version" not in objects:
        return False
    markers = tuple(str(value) for value in (await connection.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num"))).scalars())
    return not markers and inventory_is_schema_v1_allowed(objects) and await verify_schema_v1_catalog(connection)


async def stage_schema_for_setup(engine: AsyncEngine) -> None:
    """Install Schema V1 without publishing its completion marker."""

    if not isinstance(engine, AsyncEngine):
        raise TypeError("stage_schema_for_setup() requires an AsyncEngine")
    async with _postgres_lock(engine):
        async with engine.connect() as connection:
            if await classify_database(connection) == "current":
                return
            if await inventory_user_schema_objects(connection):
                raise SchemaRecreateRequired()
        await _install_full_schema(engine, publish_marker=False)
        async with engine.connect() as connection:
            if not await _is_staged_schema(connection):
                raise SchemaRecreateRequired()


async def finalize_staged_schema(engine: AsyncEngine) -> None:
    """Publish Schema V1 only after every local bootstrap stage succeeds."""

    if not isinstance(engine, AsyncEngine):
        raise TypeError("finalize_staged_schema() requires an AsyncEngine")
    async with _postgres_lock(engine):
        async with engine.begin() as connection:
            if await _is_staged_schema(connection):
                await connection.execute(
                    text(
                        "INSERT INTO alembic_version (version_num) VALUES (:revision)",
                    ),
                    {"revision": CURRENT_SCHEMA_REVISION},
                )
            elif await classify_database(connection) != "current":
                raise SchemaRecreateRequired()
        async with engine.connect() as connection:
            if await classify_database(connection) != "current":
                raise SchemaRecreateRequired()


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
    "SCHEMA_V1_REVISION",
    "SCHEMA_MUTATION_LOCK_KEY",
    "SchemaRecreateRequired",
    "SchemaSetupRequired",
    "SchemaUpgradeRequired",
    "bootstrap_schema",
    "classify_database",
    "finalize_staged_schema",
    "list_user_relations",
    "load_schema_comment_statements",
    "stage_schema_for_setup",
    "validate_schema",
    "validate_schema_installation_artifacts",
]
