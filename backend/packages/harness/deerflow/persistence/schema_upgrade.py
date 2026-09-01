"""Explicit, forward-only PostgreSQL schema upgrade registry and runner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from deerflow.persistence.bootstrap import (
    CURRENT_SCHEMA_REVISION,
    SCHEMA_MUTATION_LOCK_KEY,
    classify_database,
    load_schema_comment_statements,
    validate_schema_installation_artifacts,
)
from deerflow.persistence.final_schema_contract import (
    FINAL_SCHEMA_V1_CATALOG_SIGNATURE,
    LANGGRAPH_ROOT_OBJECTS,
    CatalogInvariant,
    inventory_is_schema_v1_allowed,
    inventory_user_schema_objects,
    read_schema_v1_catalog_signature,
    verify_schema_supporting_catalog,
    verify_schema_v1_catalog,
)

SCHEMA_COMMENTS_PLACEHOLDER = "-- INCLUDE GENERATED SCHEMA COMMENTS FROM schema_comments.sql"
SCHEMA_UPGRADE_BASE_REVISION = "schema_v1"
SCHEMA_REVISION_MAX_LENGTH = 32
_SCHEMA_REVISION_RE = re.compile(r"schema_v([1-9][0-9]*)")
_TRANSACTION_STATEMENT_RE = re.compile(
    r"\b(?:BEGIN|START|COMMIT|END|ROLLBACK|ABORT|SAVEPOINT|RELEASE|PREPARE|TRANSACTION)\b",
    re.IGNORECASE,
)
_MARKER_REFERENCE_RE = re.compile(
    r"\balembic_version\b",
    re.IGNORECASE,
)
_COMMENT_STATEMENT_RE = re.compile(
    r"^COMMENT\s+ON\s+(?:TABLE|COLUMN)\b",
    re.IGNORECASE,
)
_SCHEMA_CONTROL_RE = re.compile(
    r"\b(?:SCHEMA|search_path|set_config)\b",
    re.IGNORECASE,
)
_LEXICAL_BYPASS_RE = re.compile(
    r"/\*|\*/|\bU\s*&",
    re.IGNORECASE,
)
_QUALIFIED_IDENTIFIER_RE = re.compile(
    r'(?P<qualifier>"(?:""|[^"])+"|[a-z_][a-z0-9_$]*)\s*\.',
    re.IGNORECASE,
)


class SchemaUpgradeError(RuntimeError):
    """A schema cannot be upgraded through the packaged forward-only chain."""


@dataclass(frozen=True, slots=True)
class SchemaMigration:
    """One immutable forward migration into a newer schema revision."""

    source_revision: str
    target_revision: str
    sql_path: Path
    source_catalog_signature: Mapping[str, CatalogInvariant]
    source_inventory_digest: str


@dataclass(frozen=True, slots=True)
class SchemaUpgradeResult:
    previous_revision: str
    current_revision: str
    upgraded: bool


# Schema V1 is the current baseline. Add the first entry only when a later
# schema revision ships; runtime startup never invokes this registry.
MIGRATIONS: tuple[SchemaMigration, ...] = ()


def _schema_revision_number(revision: object) -> int:
    if not isinstance(revision, str) or len(revision) > SCHEMA_REVISION_MAX_LENGTH:
        raise SchemaUpgradeError(
            "schema revision must match schema_vN and fit VARCHAR(32)",
        )
    match = _SCHEMA_REVISION_RE.fullmatch(revision)
    if match is None:
        raise SchemaUpgradeError(
            "schema revision must match schema_vN and fit VARCHAR(32)",
        )
    return int(match.group(1))


def _read_migration_statements(migration: SchemaMigration) -> tuple[str, ...]:
    path = Path(migration.sql_path)
    try:
        if path.is_symlink() or not path.is_file():
            raise SchemaUpgradeError("schema migration artifact must be a regular file")
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SchemaUpgradeError("schema migration artifact is unavailable") from exc

    if payload.startswith("\ufeff") or "\x00" in payload:
        raise SchemaUpgradeError("schema migration artifact has invalid bytes")
    if payload.count(SCHEMA_COMMENTS_PLACEHOLDER) != 1:
        raise SchemaUpgradeError(
            "schema migration artifact must contain one comment placeholder",
        )

    executable: list[str] = []
    nonempty_lines = [line for line in payload.splitlines() if line]
    if not nonempty_lines or nonempty_lines[-1] != SCHEMA_COMMENTS_PLACEHOLDER:
        raise SchemaUpgradeError(
            "schema migration comment placeholder must be the final line",
        )
    for line in nonempty_lines:
        if line.startswith("--"):
            continue
        statement = line.strip()
        if (
            not statement.endswith(";")
            or statement.count(";") != 1
            or _TRANSACTION_STATEMENT_RE.search(statement)
            or _MARKER_REFERENCE_RE.search(statement)
            or _COMMENT_STATEMENT_RE.match(statement)
            or _SCHEMA_CONTROL_RE.search(statement)
            or _LEXICAL_BYPASS_RE.search(statement)
            or _uses_non_public_qualifier(statement)
        ):
            raise SchemaUpgradeError(
                "schema migration artifact contains a forbidden statement",
            )
        executable.append(statement)
    if not executable:
        raise SchemaUpgradeError("schema migration artifact contains no statements")
    return tuple(executable)


def _uses_non_public_qualifier(statement: str) -> bool:
    for match in _QUALIFIED_IDENTIFIER_RE.finditer(statement):
        qualifier = match.group("qualifier")
        if qualifier.lower() == "public" or qualifier == '"public"':
            continue
        return True
    return False


def _ordered_migrations() -> tuple[SchemaMigration, ...]:
    _schema_revision_number(SCHEMA_UPGRADE_BASE_REVISION)
    _schema_revision_number(CURRENT_SCHEMA_REVISION)
    migrations = tuple(MIGRATIONS)
    if not migrations:
        if CURRENT_SCHEMA_REVISION != SCHEMA_UPGRADE_BASE_REVISION:
            raise SchemaUpgradeError(
                "current schema moved beyond the baseline without a migration",
            )
        return migrations

    by_source: dict[str, SchemaMigration] = {}
    targets: set[str] = set()
    for migration in migrations:
        source_number = _schema_revision_number(migration.source_revision)
        target_number = _schema_revision_number(migration.target_revision)
        if target_number <= source_number:
            raise SchemaUpgradeError(
                "schema migration revisions must increase strictly",
            )
        source_signature = getattr(
            migration,
            "source_catalog_signature",
            None,
        )
        source_inventory_digest = getattr(
            migration,
            "source_inventory_digest",
            "",
        )
        if (
            migration.source_revision in by_source
            or migration.target_revision in targets
            or not isinstance(source_signature, Mapping)
            or set(source_signature) != set(FINAL_SCHEMA_V1_CATALOG_SIGNATURE)
            or any(not isinstance(invariant, CatalogInvariant) for invariant in source_signature.values())
            or re.fullmatch(r"[0-9a-f]{64}", source_inventory_digest) is None
        ):
            raise SchemaUpgradeError("schema migration registry is not linear")
        by_source[migration.source_revision] = migration
        targets.add(migration.target_revision)

    starts = set(by_source) - targets
    if len(starts) != 1:
        raise SchemaUpgradeError("schema migration registry has a fork or cycle")
    ordered: list[SchemaMigration] = []
    revision = starts.pop()
    if revision != SCHEMA_UPGRADE_BASE_REVISION:
        raise SchemaUpgradeError(
            "schema migration registry does not start at the baseline",
        )
    while revision in by_source:
        migration = by_source[revision]
        ordered.append(migration)
        revision = migration.target_revision
        if len(ordered) > len(migrations):
            raise SchemaUpgradeError("schema migration registry has a cycle")
    if len(ordered) != len(migrations) or revision != CURRENT_SCHEMA_REVISION:
        raise SchemaUpgradeError(
            "schema migration registry must have one head matching the current schema",
        )
    return tuple(ordered)


def validate_schema_upgrade_artifacts() -> None:
    """Validate the current snapshot and every packaged migration without I/O to PostgreSQL."""

    _load_migration_plan()


def _load_migration_plan() -> tuple[tuple[SchemaMigration, tuple[str, ...]], ...]:
    validate_schema_installation_artifacts()
    return tuple((migration, _read_migration_statements(migration)) for migration in _ordered_migrations())


def _resolve_upgrade_path(
    revision: str,
    migrations: tuple[SchemaMigration, ...] | None = None,
) -> tuple[SchemaMigration, ...]:
    if revision == CURRENT_SCHEMA_REVISION:
        return ()
    ordered = _ordered_migrations() if migrations is None else migrations
    by_source = {migration.source_revision: migration for migration in ordered}
    path: list[SchemaMigration] = []
    while revision != CURRENT_SCHEMA_REVISION:
        migration = by_source.get(revision)
        if migration is None:
            raise SchemaUpgradeError(
                "database revision has no packaged forward upgrade path",
            )
        path.append(migration)
        revision = migration.target_revision
    return tuple(path)


def schema_inventory_digest(objects: frozenset[str]) -> str:
    """Return the stable app digest frozen by a migration source contract.

    LangGraph is optional at this layer and is validated independently as
    either wholly absent or complete, so it cannot split one Schema revision
    into two different source digests.
    """

    payload = json.dumps(
        sorted(objects - LANGGRAPH_ROOT_OBJECTS),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _source_contract_matches(connection, migration: SchemaMigration) -> bool:
    objects = await inventory_user_schema_objects(connection)
    if schema_inventory_digest(objects) != migration.source_inventory_digest:
        return False
    signature = await read_schema_v1_catalog_signature(connection)
    if signature != dict(migration.source_catalog_signature):
        return False
    return await verify_schema_supporting_catalog(connection)


async def database_has_packaged_upgrade_path(connection, revision: str) -> bool:
    """Return whether the exact database catalog has a packaged forward path."""

    if revision == CURRENT_SCHEMA_REVISION:
        return False
    by_source = {migration.source_revision: migration for migration in _ordered_migrations()}
    first = by_source.get(revision)
    if first is None:
        return False
    return await _source_contract_matches(connection, first)


async def _read_revision(connection) -> str:
    has_marker = bool(
        await connection.scalar(
            text("SELECT to_regclass('alembic_version') IS NOT NULL"),
        )
    )
    if not has_marker:
        raise SchemaUpgradeError(
            "database is not initialized; run `make setup-db`",
        )
    markers = tuple(
        str(value)
        for value in (
            await connection.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num"),
            )
        ).scalars()
    )
    if len(markers) != 1:
        raise SchemaUpgradeError("database schema marker is invalid")
    return markers[0]


async def _synchronize_run_event_partition_comments(
    connection,
    comment_statements: tuple[str, ...],
) -> None:
    rows = await connection.execute(
        text(
            """SELECT child.relname
               FROM pg_inherits inheritance
               JOIN pg_class child ON child.oid=inheritance.inhrelid
               JOIN pg_class parent ON parent.oid=inheritance.inhparent
               JOIN pg_namespace namespace ON namespace.oid=parent.relnamespace
               WHERE namespace.nspname=current_schema()
                 AND parent.relname='run_events'
               ORDER BY child.relname""",
        )
    )
    partition_names = tuple(str(value) for value in rows.scalars())
    parent_table_comment = next(
        (statement for statement in comment_statements if statement.startswith("COMMENT ON TABLE run_events ")),
        None,
    )
    parent_column_comments = tuple(statement for statement in comment_statements if statement.startswith("COMMENT ON COLUMN run_events."))
    if parent_table_comment is None or not parent_column_comments:
        raise SchemaUpgradeError("run_events comments are missing from the target artifact")

    preparer = connection.dialect.identifier_preparer
    for partition_name in partition_names:
        if re.fullmatch(r"run_events_p[0-9]{6}", partition_name) is None:
            raise SchemaUpgradeError("run_events has an unsupported partition child")
        quoted_partition = preparer.quote(partition_name)
        await connection.exec_driver_sql(
            parent_table_comment.replace(
                "COMMENT ON TABLE run_events ",
                f"COMMENT ON TABLE {quoted_partition} ",
                1,
            ),
        )
        for statement in parent_column_comments:
            await connection.exec_driver_sql(
                statement.replace(
                    "COMMENT ON COLUMN run_events.",
                    f"COMMENT ON COLUMN {quoted_partition}.",
                    1,
                ),
            )


async def upgrade_schema(engine: AsyncEngine) -> SchemaUpgradeResult:
    """Upgrade a known predecessor to the packaged head in one transaction."""

    if not isinstance(engine, AsyncEngine):
        raise TypeError("upgrade_schema() requires an AsyncEngine")
    migration_plan = await asyncio.to_thread(_load_migration_plan)
    ordered_migrations = tuple(migration for migration, _statements in migration_plan)
    statements_by_source = {migration.source_revision: statements for migration, statements in migration_plan}
    comment_statements = await asyncio.to_thread(load_schema_comment_statements) if migration_plan else ()

    async with engine.begin() as connection:
        expected_database = engine.url.database
        actual_database = await connection.scalar(text("SELECT current_database()"))
        actual_schema = await connection.scalar(text("SELECT current_schema()"))
        if actual_database != expected_database or actual_schema != "public":
            raise SchemaUpgradeError(
                "database upgrade requires the exact URL target and public schema",
            )
        await connection.execute(text("SET LOCAL search_path = public"))
        await connection.execute(text("SET LOCAL lock_timeout = '10s'"))
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": SCHEMA_MUTATION_LOCK_KEY},
        )
        previous_revision = await _read_revision(connection)
        path = _resolve_upgrade_path(previous_revision, ordered_migrations)
        if not path:
            if await classify_database(connection) != "current":
                raise SchemaUpgradeError("current schema catalog validation failed")
            return SchemaUpgradeResult(
                previous_revision=previous_revision,
                current_revision=CURRENT_SCHEMA_REVISION,
                upgraded=False,
            )

        if not await _source_contract_matches(connection, path[0]):
            raise SchemaUpgradeError(
                "database catalog does not match the packaged migration source",
            )
        for migration in path:
            for statement in statements_by_source[migration.source_revision]:
                await connection.exec_driver_sql(statement)
        for statement in comment_statements:
            await connection.exec_driver_sql(statement)
        await _synchronize_run_event_partition_comments(
            connection,
            comment_statements,
        )

        objects = await inventory_user_schema_objects(connection)
        if not inventory_is_schema_v1_allowed(objects) or not await verify_schema_v1_catalog(connection):
            raise SchemaUpgradeError("upgraded schema does not match the packaged catalog")
        result = await connection.execute(
            text(
                "UPDATE alembic_version SET version_num=:target WHERE version_num=:source",
            ),
            {
                "source": previous_revision,
                "target": CURRENT_SCHEMA_REVISION,
            },
        )
        if result.rowcount != 1 or await classify_database(connection) != "current":
            raise SchemaUpgradeError("schema completion marker could not be published")
        return SchemaUpgradeResult(
            previous_revision=previous_revision,
            current_revision=CURRENT_SCHEMA_REVISION,
            upgraded=True,
        )


__all__ = [
    "MIGRATIONS",
    "SCHEMA_COMMENTS_PLACEHOLDER",
    "SCHEMA_UPGRADE_BASE_REVISION",
    "SchemaMigration",
    "SchemaUpgradeError",
    "SchemaUpgradeResult",
    "database_has_packaged_upgrade_path",
    "schema_inventory_digest",
    "upgrade_schema",
    "validate_schema_upgrade_artifacts",
]
