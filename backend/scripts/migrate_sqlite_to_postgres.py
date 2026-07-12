#!/usr/bin/env python3
"""将只读 legacy SQLite source 显式迁移到 PostgreSQL。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import asyncpg
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import JSON, Boolean, DateTime, LargeBinary

try:
    from scripts.sqlite_inventory import SQLiteInventory, inspect_sqlite, open_read_only
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from sqlite_inventory import SQLiteInventory, inspect_sqlite, open_read_only

ORM_TABLE_ORDER = (
    "users",
    "threads_meta",
    "runs",
    "run_events",
    "feedback",
    "scheduled_tasks",
    "scheduled_task_runs",
    "channel_connections",
    "channel_credentials",
    "channel_oauth_states",
    "channel_conversations",
)
LANGGRAPH_SOURCE_TABLES = frozenset({"checkpoints", "writes", "store"})
SOURCE_METADATA_TABLES = frozenset({"alembic_version", "store_migrations"})
DEFERRED_EMPTY_TABLES = frozenset({"projects", "project_memberships"})
SOURCE_SCHEMA_SIGNATURES = {
    "users": frozenset({"id", "email", "password_hash", "system_role", "created_at", "oauth_provider", "oauth_id", "needs_setup", "token_version"}),
    "threads_meta": frozenset({"thread_id", "assistant_id", "user_id", "display_name", "status", "metadata_json", "created_at", "updated_at"}),
    "runs": frozenset(
        {
            "run_id",
            "thread_id",
            "assistant_id",
            "user_id",
            "status",
            "model_name",
            "multitask_strategy",
            "metadata_json",
            "kwargs_json",
            "error",
            "message_count",
            "first_human_message",
            "last_ai_message",
            "total_input_tokens",
            "total_output_tokens",
            "total_tokens",
            "llm_call_count",
            "lead_agent_tokens",
            "subagent_tokens",
            "middleware_tokens",
            "token_usage_by_model",
            "follow_up_to_run_id",
            "created_at",
            "updated_at",
        }
    ),
    "run_events": frozenset({"id", "thread_id", "run_id", "user_id", "event_type", "category", "content", "event_metadata", "seq", "created_at"}),
    "feedback": frozenset({"feedback_id", "run_id", "thread_id", "user_id", "message_id", "rating", "comment", "created_at"}),
    "scheduled_tasks": frozenset(
        {
            "id",
            "user_id",
            "thread_id",
            "context_mode",
            "assistant_id",
            "title",
            "prompt",
            "schedule_type",
            "schedule_spec",
            "timezone",
            "status",
            "overlap_policy",
            "next_run_at",
            "last_run_at",
            "last_run_id",
            "last_thread_id",
            "last_error",
            "lease_owner",
            "lease_expires_at",
            "run_count",
            "created_at",
            "updated_at",
        }
    ),
    "scheduled_task_runs": frozenset({"id", "task_id", "thread_id", "run_id", "scheduled_for", "trigger", "status", "error", "started_at", "finished_at", "created_at"}),
    "channel_connections": frozenset(
        {
            "id",
            "owner_user_id",
            "provider",
            "status",
            "external_account_id",
            "external_account_name",
            "workspace_id",
            "workspace_name",
            "bot_user_id",
            "scopes_json",
            "capabilities_json",
            "metadata_json",
            "created_at",
            "updated_at",
            "last_seen_at",
            "last_error_at",
        }
    ),
    "channel_credentials": frozenset({"connection_id", "encrypted_access_token", "encrypted_refresh_token", "token_type", "expires_at", "refresh_expires_at", "encrypted_extra_json", "version", "updated_at"}),
    "channel_oauth_states": frozenset({"state_hash", "owner_user_id", "provider", "code_verifier_encrypted", "nonce_hash", "redirect_after", "requested_scopes_json", "metadata_json", "expires_at", "consumed_at", "created_at"}),
    "channel_conversations": frozenset({"id", "connection_id", "owner_user_id", "provider", "external_conversation_id", "external_topic_id", "thread_id", "created_at", "updated_at"}),
}
SOURCE_PRIMARY_KEYS = {
    "users": ("id",),
    "threads_meta": ("thread_id",),
    "runs": ("run_id",),
    "run_events": ("id",),
    "feedback": ("feedback_id",),
    "scheduled_tasks": ("id",),
    "scheduled_task_runs": ("id",),
    "channel_connections": ("id",),
    "channel_credentials": ("connection_id",),
    "channel_oauth_states": ("state_hash",),
    "channel_conversations": ("id",),
}


class MigrationErrorCode(StrEnum):
    MIGRATION = "migration"
    SCHEMA = "schema"
    CONFLICT = "conflict"
    DECODE = "decode"
    FINGERPRINT = "fingerprint"
    BACKUP = "backup"


class MigrationError(RuntimeError):
    """Credential-safe migration failure."""

    def __init__(
        self,
        message: str,
        *,
        code: MigrationErrorCode | None = None,
        table: str | None = None,
        source_sha256: str | None = None,
        source_key: str | None = None,
    ) -> None:
        super().__init__(message)
        lowered = message.lower()
        if code is None:
            if "fingerprint" in lowered or "wal/shm" in lowered:
                code = MigrationErrorCode.FINGERPRINT
            elif "schema" in lowered or "unknown source table" in lowered or "primary key" in lowered:
                code = MigrationErrorCode.SCHEMA
            elif "conflict" in lowered or "missing" in lowered:
                code = MigrationErrorCode.CONFLICT
            elif "decode" in lowered or "invalid json" in lowered:
                code = MigrationErrorCode.DECODE
            elif "backup" in lowered:
                code = MigrationErrorCode.BACKUP
            else:
                code = MigrationErrorCode.MIGRATION
        self.code = code
        self.table = table
        self.source_sha256_prefix = source_sha256[:12] if source_sha256 else None
        self.key_hash = hashlib.sha256(source_key.encode()).hexdigest()[:12] if source_key else None

    def safe_fields(self) -> str:
        fields = [f"code={self.code.value}"]
        if self.table:
            fields.append(f"table={self.table}")
        if self.source_sha256_prefix:
            fields.append(f"source={self.source_sha256_prefix}")
        if self.key_hash:
            fields.append(f"key={self.key_hash}")
        return " ".join(fields)

    def enrich(
        self,
        *,
        code: MigrationErrorCode | None = None,
        table: str | None = None,
        source_sha256: str | None = None,
        source_key: str | None = None,
    ) -> MigrationError:
        if code is not None and self.code == MigrationErrorCode.MIGRATION:
            self.code = code
        if self.table is None and table is not None:
            self.table = table
        if self.source_sha256_prefix is None and source_sha256:
            self.source_sha256_prefix = source_sha256[:12]
        if self.key_hash is None and source_key:
            self.key_hash = hashlib.sha256(source_key.encode()).hexdigest()[:12]
        return self


@contextmanager
def _row_error_boundary(
    *,
    table: str,
    source_sha256: str,
    source_key: str,
    code: MigrationErrorCode = MigrationErrorCode.MIGRATION,
):
    try:
        yield
    except MigrationError as exc:
        raise exc.enrich(code=code, table=table, source_sha256=source_sha256, source_key=source_key) from None
    except Exception:
        raise MigrationError(
            "safe row processing failure",
            code=code,
            table=table,
            source_sha256=source_sha256,
            source_key=source_key,
        ) from None


def _raw_row_key(row: Any) -> str:
    try:
        return _json_canonical(list(row))
    except Exception:
        return hashlib.sha256(type(row).__name__.encode()).hexdigest()


@dataclass(frozen=True)
class SourceInspection:
    inventory: SQLiteInventory
    deferred_empty: tuple[str, ...]


@dataclass(frozen=True)
class BackupResult:
    path: Path
    sha256: str
    size_bytes: int
    reused: bool


@dataclass(frozen=True)
class NormalizedRow:
    source_key: str
    target_key: str
    values: dict[str, Any]
    digest: str


@dataclass(frozen=True)
class DecodedCheckpoint:
    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    parent_checkpoint_id: str | None
    checkpoint: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class DecodedWrite:
    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    task_id: str
    idx: int
    channel: str
    value: Any


@dataclass(frozen=True)
class TableMigrationReport:
    source_rows: int
    inserted: int = 0
    adopted: int = 0
    already_migrated: int = 0
    planned_insert: int = 0
    verified: bool = False


@dataclass(frozen=True)
class MigrationReport:
    source_sha256: str
    dry_run: bool
    tables: dict[str, TableMigrationReport]
    deferred_empty: tuple[str, ...]
    verified: bool
    source_size_bytes: int = 0
    atomicity: str = (
        "ORM, checkpoint writes, and store rows commit with ledger per table; checkpoint and blobs use direct "
        "transactional insert-or-compare with in-transaction semantic reconstruction, followed by Saver read-back "
        "and replay-safe ledger convergence"
    )


@dataclass(frozen=True)
class UnionPlan:
    reference_keys: frozenset[tuple[str, tuple[str, ...], str]]
    per_source_reference_keys: tuple[frozenset[tuple[str, tuple[str, ...], str]], ...] = ()
    per_source_checkpoint_keys: tuple[frozenset[tuple[str, str, str]], ...] = ()
    source_fingerprints: tuple[tuple[str, int], ...] = ()


def _known_orm_columns() -> dict[str, frozenset[str]]:
    import deerflow.persistence.models  # noqa: F401
    from deerflow.persistence.base import Base

    return {table: frozenset(column.name for column in Base.metadata.tables[table].columns) for table in ORM_TABLE_ORDER}


def inspect_source(source: Path) -> SourceInspection:
    try:
        inventory = inspect_sqlite(source)
    except Exception as exc:
        raise MigrationError("SQLite source inventory or integrity check failed") from exc
    for suffix in ("-wal", "-shm"):
        sidecar = source.with_name(f"{source.name}{suffix}")
        if sidecar.exists() and sidecar.stat().st_size:
            raise MigrationError(
                "active SQLite WAL/SHM sidecar detected",
                code=MigrationErrorCode.FINGERPRINT,
                source_sha256=inventory.sha256,
            )
    known_columns = _known_orm_columns()
    deferred = []

    def schema_error(message: str, table_name: str) -> MigrationError:
        return MigrationError(
            message,
            code=MigrationErrorCode.SCHEMA,
            table=table_name,
            source_sha256=inventory.sha256,
        )

    allowed = set(ORM_TABLE_ORDER) | LANGGRAPH_SOURCE_TABLES | SOURCE_METADATA_TABLES | DEFERRED_EMPTY_TABLES
    for table in inventory.tables:
        if table.name not in allowed:
            raise MigrationError(
                f"unknown source table: {table.name}",
                code=MigrationErrorCode.SCHEMA,
                table=table.name,
                source_sha256=inventory.sha256,
            )
        if table.name in DEFERRED_EMPTY_TABLES:
            if table.row_count:
                raise MigrationError(
                    f"deferred source table is not empty: {table.name}",
                    code=MigrationErrorCode.CONFLICT,
                    table=table.name,
                    source_sha256=inventory.sha256,
                )
            deferred.append(table.name)
        if table.name in known_columns:
            unknown = sorted(set(table.columns) - known_columns[table.name])
            if unknown:
                raise schema_error(f"{table.name} has unknown columns: {', '.join(unknown)}", table.name)
            if frozenset(table.columns) != SOURCE_SCHEMA_SIGNATURES[table.name]:
                raise schema_error(f"unsupported {table.name} source schema", table.name)
            if table.primary_key != SOURCE_PRIMARY_KEYS[table.name]:
                raise schema_error(f"unsupported {table.name} source primary key", table.name)
        if table.name == "checkpoints":
            expected = {
                "thread_id",
                "checkpoint_ns",
                "checkpoint_id",
                "parent_checkpoint_id",
                "type",
                "checkpoint",
                "metadata",
            }
            if set(table.columns) != expected:
                raise schema_error("unsupported checkpoints source schema", table.name)
            if table.primary_key != ("thread_id", "checkpoint_ns", "checkpoint_id"):
                raise schema_error("unsupported checkpoints source primary key", table.name)
        if table.name == "writes":
            expected = {
                "thread_id",
                "checkpoint_ns",
                "checkpoint_id",
                "task_id",
                "idx",
                "channel",
                "type",
                "value",
            }
            if set(table.columns) != expected:
                raise schema_error("unsupported writes source schema", table.name)
            if table.primary_key != ("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"):
                raise schema_error("unsupported writes source primary key", table.name)
        if table.name == "store":
            required = {"prefix", "key", "value", "created_at", "updated_at"}
            allowed_store = required | {"expires_at", "ttl_minutes"}
            if not required.issubset(table.columns) or not set(table.columns).issubset(allowed_store):
                raise schema_error("unsupported store source schema", table.name)
            if table.primary_key != ("prefix", "key"):
                raise schema_error("unsupported store source primary key", table.name)
    return SourceInspection(inventory=inventory, deferred_empty=tuple(sorted(deferred)))


def _require_fingerprint(inventory: SQLiteInventory, expected: tuple[str, int] | None) -> None:
    if expected is not None and (inventory.sha256, inventory.size_bytes) != expected:
        raise MigrationError(
            "SQLite source fingerprint changed after preflight",
            code=MigrationErrorCode.FINGERPRINT,
            source_sha256=inventory.sha256,
        )


def backup_source(
    source: Path,
    backup_dir: Path,
    expected_fingerprint: tuple[str, int] | None = None,
) -> BackupResult:
    inspection = inspect_source(source)
    inventory = inspection.inventory
    _require_fingerprint(inventory, expected_fingerprint)
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"{source.name}.{inventory.sha256[:12]}.bak"
    if destination.exists():
        existing = hashlib.sha256(destination.read_bytes()).hexdigest()
        if existing != inventory.sha256 or destination.stat().st_size != inventory.size_bytes:
            raise MigrationError("existing backup digest does not match source")
        return BackupResult(destination, existing, inventory.size_bytes, True)

    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=backup_dir)
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as target, source.open("rb") as source_file:
            for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        if digest.hexdigest() != inventory.sha256 or size != inventory.size_bytes:
            raise MigrationError("backup digest does not match source")
        reused = False
        try:
            os.link(temporary, destination)
        except FileExistsError:
            reused = True
            existing = hashlib.sha256(destination.read_bytes()).hexdigest()
            if existing != inventory.sha256 or destination.stat().st_size != inventory.size_bytes:
                raise MigrationError("concurrent backup publish conflict") from None
        temporary.unlink()
        directory_fd = os.open(backup_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return BackupResult(destination, inventory.sha256, inventory.size_bytes, reused)


def _read_rows(source: Path, table: str) -> list[sqlite3.Row]:
    with open_read_only(source) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return list(connection.execute(f'SELECT * FROM "{table}"'))


def _parse_datetime(value: object, *, table: str, column: str) -> datetime | None:
    if value is None or isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MigrationError(f"invalid UTC datetime in {table}.{column}") from exc
    else:
        raise MigrationError(f"invalid UTC datetime in {table}.{column}")
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC) if parsed is not None else None


def _normalize_value(value: object, column: Any, *, table: str) -> object:
    if value is None:
        return None
    if isinstance(column.type, JSON):
        if isinstance(value, (str, bytes, bytearray)):
            try:
                return json.loads(value)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
                raise MigrationError(f"invalid JSON in {table}.{column.name}") from exc
        if isinstance(value, (dict, list, int, float, bool)):
            return value
        raise MigrationError(f"invalid JSON in {table}.{column.name}")
    if isinstance(column.type, Boolean):
        if value not in (0, 1, False, True):
            raise MigrationError(f"invalid boolean in {table}.{column.name}")
        return bool(value)
    if isinstance(column.type, DateTime):
        return _parse_datetime(value, table=table, column=column.name)
    if isinstance(column.type, LargeBinary):
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise MigrationError(f"invalid binary value in {table}.{column.name}")
        return bytes(value)
    return value


def _canonical(value: object) -> object:
    if isinstance(value, datetime):
        return {"$datetime": value.astimezone(UTC).isoformat()}
    if isinstance(value, bytes):
        return {"$bytes_sha256": hashlib.sha256(value).hexdigest(), "$size": len(value)}
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        type_, blob = JsonPlusSerializer().dumps_typed(value)
    except Exception as exc:
        raise MigrationError(f"unsupported normalized value type: {type(value).__name__}") from exc
    return {
        "$typed": type_,
        "$typed_sha256": hashlib.sha256(blob).hexdigest(),
        "$size": len(blob),
    }


def _json_canonical(value: object) -> str:
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_business_rows(source: Path, table: str) -> list[NormalizedRow]:
    inspection = inspect_source(source)
    inventory_table = next((item for item in inspection.inventory.tables if item.name == table), None)
    if inventory_table is None:
        return []
    if table not in ORM_TABLE_ORDER:
        raise MigrationError(f"not an ORM source table: {table}")
    if inventory_table.row_count and not inventory_table.primary_key:
        raise MigrationError(f"source table has no primary key: {table}")

    import deerflow.persistence.models  # noqa: F401
    from deerflow.persistence.base import Base

    target = Base.metadata.tables[table]
    source_pk = inventory_table.primary_key
    target_pk = tuple(column.name for column in target.primary_key.columns)
    normalized: list[NormalizedRow] = []
    seen: set[str] = set()
    for row in _read_rows(source, table):
        boundary_key = _raw_row_key(row)
        with _row_error_boundary(
            table=table,
            source_sha256=inspection.inventory.sha256,
            source_key=boundary_key,
            code=MigrationErrorCode.DECODE,
        ):
            values = {name: _normalize_value(row[name], target.c[name], table=table) for name in row.keys()}
            source_key = _json_canonical([values[name] for name in source_pk])
            if source_key in seen:
                raise MigrationError(f"duplicate source key in {table}")
            seen.add(source_key)
            missing_target_key = [name for name in target_pk if name not in values]
            if missing_target_key:
                raise MigrationError(f"{table} is missing target primary key columns")
            target_key = _json_canonical([values[name] for name in target_pk])
            digest = hashlib.sha256(_json_canonical(values).encode()).hexdigest()
            normalized.append(NormalizedRow(source_key, target_key, values, digest))
    return sorted(normalized, key=lambda item: item.source_key.encode())


def decode_checkpoint_rows(source: Path) -> tuple[list[DecodedCheckpoint], list[DecodedWrite]]:
    inspection = inspect_source(source)
    tables = {table.name for table in inspection.inventory.tables}
    serde = JsonPlusSerializer()
    checkpoints: list[DecodedCheckpoint] = []
    writes: list[DecodedWrite] = []
    if "checkpoints" in tables:
        for row in _read_rows(source, "checkpoints"):
            source_key = _json_canonical([row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"]])
            with _row_error_boundary(
                table="checkpoints",
                source_sha256=inspection.inventory.sha256,
                source_key=source_key,
                code=MigrationErrorCode.DECODE,
            ):
                checkpoint = serde.loads_typed((row["type"], bytes(row["checkpoint"])))
                metadata = json.loads(bytes(row["metadata"]) if row["metadata"] is not None else b"{}")
                if not isinstance(checkpoint, dict) or not isinstance(metadata, dict):
                    raise MigrationError("invalid checkpoint semantic value")
                checkpoints.append(
                    DecodedCheckpoint(
                        str(row["thread_id"]),
                        str(row["checkpoint_ns"]),
                        str(row["checkpoint_id"]),
                        str(row["parent_checkpoint_id"]) if row["parent_checkpoint_id"] is not None else None,
                        checkpoint,
                        metadata,
                    )
                )
    if "writes" in tables:
        for row in _read_rows(source, "writes"):
            source_key = _json_canonical([row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"], row["task_id"], row["idx"]])
            with _row_error_boundary(
                table="writes",
                source_sha256=inspection.inventory.sha256,
                source_key=source_key,
                code=MigrationErrorCode.DECODE,
            ):
                writes.append(
                    DecodedWrite(
                        str(row["thread_id"]),
                        str(row["checkpoint_ns"]),
                        str(row["checkpoint_id"]),
                        str(row["task_id"]),
                        int(row["idx"]),
                        str(row["channel"]),
                        serde.loads_typed((row["type"], bytes(row["value"]))),
                    )
                )
    checkpoints = _order_checkpoints(checkpoints, source_sha256=inspection.inventory.sha256)
    writes.sort(key=lambda row: (row.thread_id, row.checkpoint_ns, row.checkpoint_id, row.task_id, row.idx))
    return checkpoints, writes


def _checkpoint_identity_set_key(checkpoints: list[DecodedCheckpoint]) -> str:
    identities = {_json_canonical([row.thread_id, row.checkpoint_ns, row.checkpoint_id]) for row in checkpoints}
    return _json_canonical(sorted(identities, key=lambda value: value.encode()))


def _order_checkpoints(
    checkpoints: list[DecodedCheckpoint],
    *,
    source_sha256: str = "",
) -> list[DecodedCheckpoint]:
    ordered: list[DecodedCheckpoint] = []
    groups: dict[tuple[str, str], dict[str, DecodedCheckpoint]] = {}
    related_key = _checkpoint_identity_set_key(checkpoints)
    try:
        for row in checkpoints:
            key = (row.thread_id, row.checkpoint_ns)
            if row.checkpoint_id in groups.setdefault(key, {}):
                raise MigrationError("duplicate checkpoint source key")
            groups[key][row.checkpoint_id] = row
        for group_key in sorted(groups, key=lambda key: (key[0].encode(), key[1].encode())):
            remaining = dict(groups[group_key])
            related_key = _checkpoint_identity_set_key(list(remaining.values()))
            emitted: set[str] = set()
            while remaining:
                ready = sorted(
                    (row for row in remaining.values() if row.parent_checkpoint_id not in remaining or row.parent_checkpoint_id in emitted),
                    key=lambda row: row.checkpoint_id.encode(),
                )
                if not ready:
                    raise MigrationError("checkpoint parent cycle detected")
                for row in ready:
                    ordered.append(row)
                    emitted.add(row.checkpoint_id)
                    del remaining[row.checkpoint_id]
    except MigrationError as exc:
        raise exc.enrich(
            code=MigrationErrorCode.DECODE,
            table="checkpoints",
            source_sha256=source_sha256,
            source_key=related_key,
        ) from None
    return ordered


def _target_value(value: object, column: Any, *, table: str) -> object:
    return _normalize_value(value, column, table=table)


async def _ledger_row(connection: Any, source_sha256: str, table: str, source_key: str) -> Any:
    return await connection.fetchrow(
        "SELECT target_table, target_key, row_digest, status FROM migration_ledger WHERE source_sha256=$1 AND source_table=$2 AND source_key=$3",
        source_sha256,
        table,
        source_key,
    )


async def _write_ledger(
    connection: Any,
    *,
    source_sha256: str,
    source_table: str,
    source_key: str,
    target_table: str,
    target_key: str,
    row_digest: str,
    status: str,
) -> None:
    await connection.execute(
        "INSERT INTO migration_ledger (id, source_sha256, source_table, source_key, target_table, target_key, row_digest, status, migrated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
        uuid.uuid4(),
        source_sha256,
        source_table,
        source_key,
        target_table,
        target_key,
        row_digest,
        status,
        datetime.now(UTC),
    )


def _validate_ledger(ledger: Any, *, target_table: str, target_key: str, digest: str) -> None:
    if ledger["target_table"] != target_table or ledger["target_key"] != target_key or ledger["row_digest"] != digest or ledger["status"] not in {"migrated", "adopted"}:
        raise MigrationError("migration ledger semantic conflict")


async def _fetch_business_target(connection: Any, target: Any, row: NormalizedRow) -> dict[str, Any] | None:
    pk = tuple(column.name for column in target.primary_key.columns)
    where = " AND ".join(f'"{name}" IS NOT DISTINCT FROM ${idx}' for idx, name in enumerate(pk, 1))
    record = await connection.fetchrow(
        f'SELECT * FROM "{target.name}" WHERE {where}',
        *(row.values[name] for name in pk),
    )
    if record is None:
        return None
    return {column.name: _target_value(record[column.name], column, table=target.name) for column in target.columns}


async def _insert_business_target(connection: Any, target: Any, row: NormalizedRow) -> None:
    columns = tuple(column.name for column in target.columns)
    expressions = []
    parameters = []
    for idx, name in enumerate(columns, 1):
        value = row.values[name]
        parameters.append(_json_canonical(value) if isinstance(target.c[name].type, JSON) else value)
        expressions.append(f"${idx}::jsonb" if isinstance(target.c[name].type, JSON) else f"${idx}")
    names = ", ".join(f'"{name}"' for name in columns)
    await connection.execute(
        f'INSERT INTO "{target.name}" ({names}) VALUES ({", ".join(expressions)})',
        *parameters,
    )


async def _preflight_target_uniques(connection: Any, target: Any, row: NormalizedRow) -> None:
    primary_key = tuple(column.name for column in target.primary_key.columns)
    for unique_name, unique_key in _business_unique_keys(target.name, row.values):
        values = json.loads(unique_key)
        matching_columns = None
        for constraint in list(target.constraints) + list(target.indexes):
            if getattr(constraint, "name", None) != unique_name:
                continue
            if not getattr(constraint, "unique", False) and constraint.__class__.__name__ != "UniqueConstraint":
                continue
            columns = tuple(column.name for column in constraint.columns)
            if _json_canonical([row.values[column] for column in columns]) == unique_key:
                matching_columns = columns
                break
        if not matching_columns:
            continue
        where = " AND ".join(f'"{column}" IS NOT DISTINCT FROM ${idx}' for idx, column in enumerate(matching_columns, 1))
        if unique_name == "uq_channel_connection_active_identity":
            where += " AND status != 'revoked'"
        existing = await connection.fetchrow(
            f'SELECT {", ".join(f"{column}" for column in primary_key)} FROM "{target.name}" WHERE {where}',
            *values,
        )
        if existing is not None and any(existing[column] != row.values[column] for column in primary_key):
            raise MigrationError(f"target unique constraint conflict in {target.name}")


async def _preflight_foreign_keys(
    connection: Any,
    target: Any,
    row: NormalizedRow,
    union_reference_keys: frozenset[tuple[str, tuple[str, ...], str]],
) -> None:
    for constraint in target.foreign_key_constraints:
        local_columns = tuple(element.parent.name for element in constraint.elements)
        remote_table = constraint.elements[0].column.table.name
        remote_columns = tuple(element.column.name for element in constraint.elements)
        values = [row.values[column] for column in local_columns]
        if any(value is None for value in values):
            continue
        identity = (remote_table, remote_columns, _json_canonical(values))
        if identity in union_reference_keys:
            continue
        where = " AND ".join(f'"{column}" IS NOT DISTINCT FROM ${idx}' for idx, column in enumerate(remote_columns, 1))
        if not await connection.fetchval(f'SELECT 1 FROM "{remote_table}" WHERE {where}', *values):
            raise MigrationError(f"missing foreign key target for {target.name}")


async def _migrate_business_table(
    connection: Any,
    source: Path,
    source_sha256: str,
    table: str,
    *,
    dry_run: bool,
    union_reference_keys: frozenset[tuple[str, tuple[str, ...], str]],
) -> TableMigrationReport:
    import deerflow.persistence.models  # noqa: F401
    from deerflow.persistence.base import Base

    target = Base.metadata.tables[table]
    rows = normalize_business_rows(source, table)
    inserted = adopted = already = planned = 0
    transaction = connection.transaction()
    await transaction.start()
    current_source_key = None
    try:
        for row in rows:
            current_source_key = row.source_key
            ledger = await _ledger_row(connection, source_sha256, table, row.source_key)
            if ledger is not None:
                _validate_ledger(ledger, target_table=table, target_key=row.target_key, digest=row.digest)
                target_row = await _fetch_business_target(connection, target, row)
                if target_row is None or hashlib.sha256(_json_canonical(target_row).encode()).hexdigest() != row.digest:
                    raise MigrationError(f"target verification conflict in {table}")
                already += 1
                continue

            await _preflight_target_uniques(connection, target, row)
            await _preflight_foreign_keys(connection, target, row, union_reference_keys)

            target_row = await _fetch_business_target(connection, target, row)
            status = "adopted"
            if target_row is None:
                if dry_run:
                    planned += 1
                    continue
                await _insert_business_target(connection, target, row)
                inserted_row = await _fetch_business_target(connection, target, row)
                if inserted_row is None or hashlib.sha256(_json_canonical(inserted_row).encode()).hexdigest() != row.digest:
                    raise MigrationError(f"target semantic read-back failed in {table}")
                inserted += 1
                status = "migrated"
            elif hashlib.sha256(_json_canonical(target_row).encode()).hexdigest() == row.digest:
                adopted += 1
            else:
                raise MigrationError(f"target row conflict in {table}")
            if not dry_run:
                await _write_ledger(
                    connection,
                    source_sha256=source_sha256,
                    source_table=table,
                    source_key=row.source_key,
                    target_table=table,
                    target_key=row.target_key,
                    row_digest=row.digest,
                    status=status,
                )
        if dry_run:
            await transaction.rollback()
        else:
            await transaction.commit()
    except Exception as exc:
        try:
            await transaction.rollback()
        except Exception:
            pass
        if isinstance(exc, MigrationError):
            raise exc.enrich(table=table, source_sha256=source_sha256, source_key=current_source_key)
        raise
    return TableMigrationReport(
        source_rows=len(rows),
        inserted=inserted,
        adopted=adopted,
        already_migrated=already,
        planned_insert=planned,
        verified=True,
    )


def _checkpoint_identity(row: DecodedCheckpoint) -> str:
    return _json_canonical([row.thread_id, row.checkpoint_ns, row.checkpoint_id])


def _write_identity(row: DecodedWrite) -> str:
    return _json_canonical([row.thread_id, row.checkpoint_ns, row.checkpoint_id, row.task_id, row.idx])


def _write_digest(channel: str, value: Any, task_path: str = "") -> str:
    return hashlib.sha256(_json_canonical({"task_path": task_path, "channel": channel, "value": value}).encode()).hexdigest()


def _checkpoint_digest(row: DecodedCheckpoint) -> str:
    return hashlib.sha256(
        _json_canonical(
            {
                "parent": row.parent_checkpoint_id,
                "checkpoint": row.checkpoint,
                "metadata": row.metadata,
            }
        ).encode()
    ).hexdigest()


async def _migrate_checkpoints(
    connection: Any,
    saver: Any,
    source_sha256: str,
    checkpoints: list[DecodedCheckpoint],
    *,
    dry_run: bool,
    planned_checkpoint_keys: frozenset[tuple[str, str, str]] = frozenset(),
) -> TableMigrationReport:
    inserted = adopted = already = planned = 0
    source_checkpoint_ids = {(item.thread_id, item.checkpoint_ns, item.checkpoint_id) for item in checkpoints}
    for row in checkpoints:
        source_key = _checkpoint_identity(row)
        if row.parent_checkpoint_id and (row.thread_id, row.checkpoint_ns, row.parent_checkpoint_id) not in source_checkpoint_ids:
            if (row.thread_id, row.checkpoint_ns, row.parent_checkpoint_id) in planned_checkpoint_keys:
                parent = True
            else:
                with _row_error_boundary(table="checkpoints", source_sha256=source_sha256, source_key=source_key):
                    parent = await saver.aget_tuple(
                        {
                            "configurable": {
                                "thread_id": row.thread_id,
                                "checkpoint_ns": row.checkpoint_ns,
                                "checkpoint_id": row.parent_checkpoint_id,
                            }
                        }
                    )
            if parent is None:
                raise MigrationError("checkpoint parent missing from source and target").enrich(table="checkpoints", source_sha256=source_sha256, source_key=source_key)
        digest = _checkpoint_digest(row)
        with _row_error_boundary(table="checkpoints", source_sha256=source_sha256, source_key=source_key):
            ledger = await _ledger_row(connection, source_sha256, "checkpoints", source_key)
        config = {
            "configurable": {
                "thread_id": row.thread_id,
                "checkpoint_ns": row.checkpoint_ns,
                "checkpoint_id": row.checkpoint_id,
            }
        }
        with _row_error_boundary(table="checkpoints", source_sha256=source_sha256, source_key=source_key):
            target_tuple = await saver.aget_tuple(config)
        if target_tuple is not None:
            with _row_error_boundary(table="checkpoints", source_sha256=source_sha256, source_key=source_key, code=MigrationErrorCode.CONFLICT):
                target_row = DecodedCheckpoint(
                    row.thread_id,
                    row.checkpoint_ns,
                    row.checkpoint_id,
                    target_tuple.parent_config["configurable"]["checkpoint_id"] if target_tuple.parent_config else None,
                    target_tuple.checkpoint,
                    target_tuple.metadata,
                )
                if _checkpoint_digest(target_row) != digest:
                    raise MigrationError("target checkpoint conflict")
            if ledger is not None:
                with _row_error_boundary(table="checkpoints", source_sha256=source_sha256, source_key=source_key, code=MigrationErrorCode.CONFLICT):
                    _validate_ledger(ledger, target_table="checkpoints", target_key=source_key, digest=digest)
                already += 1
                continue
            if dry_run:
                adopted += 1
                continue
            adopted += 1
            status = "adopted"
        else:
            if ledger is not None:
                raise MigrationError("checkpoint ledger points to missing target").enrich(
                    code=MigrationErrorCode.CONFLICT,
                    table="checkpoints",
                    source_sha256=source_sha256,
                    source_key=source_key,
                )
            if dry_run:
                planned += 1
                continue
            with _row_error_boundary(table="checkpoints", source_sha256=source_sha256, source_key=source_key):
                created = await _strict_insert_checkpoint(connection, row)
                read_back = await saver.aget_tuple(config)
            with _row_error_boundary(table="checkpoints", source_sha256=source_sha256, source_key=source_key, code=MigrationErrorCode.CONFLICT):
                if read_back is None:
                    raise MigrationError("checkpoint semantic verification failed")
                verified_row = DecodedCheckpoint(
                    row.thread_id,
                    row.checkpoint_ns,
                    row.checkpoint_id,
                    read_back.parent_config["configurable"]["checkpoint_id"] if read_back.parent_config else None,
                    read_back.checkpoint,
                    read_back.metadata,
                )
                if _checkpoint_digest(verified_row) != digest:
                    raise MigrationError("checkpoint semantic verification failed")
            if created:
                inserted += 1
                status = "migrated"
            else:
                adopted += 1
                status = "adopted"
        with _row_error_boundary(table="checkpoints", source_sha256=source_sha256, source_key=source_key):
            await _write_ledger(
                connection,
                source_sha256=source_sha256,
                source_table="checkpoints",
                source_key=source_key,
                target_table="checkpoints",
                target_key=source_key,
                row_digest=digest,
                status=status,
            )
    return TableMigrationReport(len(checkpoints), inserted, adopted, already, planned, True)


async def _strict_insert_checkpoint(connection: Any, row: DecodedCheckpoint) -> bool:
    serde = JsonPlusSerializer()
    checkpoint = dict(row.checkpoint)
    channel_values = dict(checkpoint.get("channel_values", {}))
    checkpoint["channel_values"] = dict(channel_values)
    versions = checkpoint.get("channel_versions", {})
    async with connection.transaction():
        for channel, value in channel_values.items():
            if value is None or isinstance(value, (str, int, float, bool)):
                continue
            checkpoint["channel_values"].pop(channel)
            if channel not in versions:
                raise MigrationError("checkpoint blob channel has no version")
            type_, blob = serde.dumps_typed(value)
            inserted = await connection.fetchval(
                "INSERT INTO checkpoint_blobs (thread_id,checkpoint_ns,channel,version,type,blob) VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING RETURNING version",
                row.thread_id,
                row.checkpoint_ns,
                channel,
                str(versions[channel]),
                type_,
                blob,
            )
            if inserted is None:
                existing = await connection.fetchrow(
                    "SELECT type,blob FROM checkpoint_blobs WHERE thread_id=$1 AND checkpoint_ns=$2 AND channel=$3 AND version=$4",
                    row.thread_id,
                    row.checkpoint_ns,
                    channel,
                    str(versions[channel]),
                )
                if existing is None or _json_canonical(serde.loads_typed((existing["type"], bytes(existing["blob"])))) != _json_canonical(value):
                    raise MigrationError("checkpoint blob conflict")
        inserted_checkpoint = await connection.fetchval(
            "INSERT INTO checkpoints (thread_id,checkpoint_ns,checkpoint_id,parent_checkpoint_id,checkpoint,metadata) VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb) ON CONFLICT DO NOTHING RETURNING checkpoint_id",
            row.thread_id,
            row.checkpoint_ns,
            row.checkpoint_id,
            row.parent_checkpoint_id,
            json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":")),
            json.dumps(row.metadata, ensure_ascii=False, separators=(",", ":")),
        )
        direct_readback = await _fetch_checkpoint_semantic(connection, row)
        if direct_readback is None or _checkpoint_digest(direct_readback) != _checkpoint_digest(row):
            raise MigrationError("checkpoint transactional semantic conflict")
        return inserted_checkpoint is not None


async def _fetch_checkpoint_semantic(connection: Any, expected: DecodedCheckpoint) -> DecodedCheckpoint | None:
    record = await connection.fetchrow(
        "SELECT parent_checkpoint_id,checkpoint,metadata FROM checkpoints WHERE thread_id=$1 AND checkpoint_ns=$2 AND checkpoint_id=$3",
        expected.thread_id,
        expected.checkpoint_ns,
        expected.checkpoint_id,
    )
    if record is None:
        return None
    checkpoint = record["checkpoint"]
    metadata = record["metadata"]
    if isinstance(checkpoint, str):
        checkpoint = json.loads(checkpoint)
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    checkpoint = dict(checkpoint)
    channel_values = dict(checkpoint.get("channel_values", {}))
    serde = JsonPlusSerializer()
    for channel, version in checkpoint.get("channel_versions", {}).items():
        if channel in channel_values:
            continue
        blob = await connection.fetchrow(
            "SELECT type,blob FROM checkpoint_blobs WHERE thread_id=$1 AND checkpoint_ns=$2 AND channel=$3 AND version=$4",
            expected.thread_id,
            expected.checkpoint_ns,
            channel,
            str(version),
        )
        if blob is not None:
            channel_values[channel] = serde.loads_typed((blob["type"], bytes(blob["blob"])))
    checkpoint["channel_values"] = channel_values
    return DecodedCheckpoint(
        expected.thread_id,
        expected.checkpoint_ns,
        expected.checkpoint_id,
        record["parent_checkpoint_id"],
        checkpoint,
        dict(metadata),
    )


async def _migrate_writes_rows(
    connection: Any,
    source_sha256: str,
    writes: list[DecodedWrite],
    *,
    dry_run: bool,
    planned_checkpoint_keys: frozenset[tuple[str, str, str]] = frozenset(),
) -> TableMigrationReport:
    serde = JsonPlusSerializer()
    inserted = adopted = already = planned = 0
    seen_identities: set[str] = set()
    for row in writes:
        source_key = _write_identity(row)
        if source_key in seen_identities:
            raise MigrationError("duplicate checkpoint write source identity").enrich(
                code=MigrationErrorCode.CONFLICT,
                table="writes",
                source_sha256=source_sha256,
                source_key=source_key,
            )
        seen_identities.add(source_key)
    for row in writes:
        source_key = _write_identity(row)
        with _row_error_boundary(table="writes", source_sha256=source_sha256, source_key=source_key):
            checkpoint_exists = (row.thread_id, row.checkpoint_ns, row.checkpoint_id) in planned_checkpoint_keys or await connection.fetchval(
                "SELECT 1 FROM checkpoints WHERE thread_id=$1 AND checkpoint_ns=$2 AND checkpoint_id=$3",
                row.thread_id,
                row.checkpoint_ns,
                row.checkpoint_id,
            )
        if not checkpoint_exists:
            raise MigrationError("checkpoint write references missing checkpoint").enrich(table="writes", source_sha256=source_sha256, source_key=source_key)
        digest = _write_digest(row.channel, row.value)
        with _row_error_boundary(table="writes", source_sha256=source_sha256, source_key=source_key):
            ledger = await _ledger_row(connection, source_sha256, "writes", source_key)
            target = await connection.fetchrow(
                "SELECT task_path,channel,type,blob FROM checkpoint_writes WHERE thread_id=$1 AND checkpoint_ns=$2 AND checkpoint_id=$3 AND task_id=$4 AND idx=$5",
                row.thread_id,
                row.checkpoint_ns,
                row.checkpoint_id,
                row.task_id,
                row.idx,
            )
        if target is not None:
            with _row_error_boundary(table="writes", source_sha256=source_sha256, source_key=source_key, code=MigrationErrorCode.DECODE):
                target_value = serde.loads_typed((target["type"], bytes(target["blob"])))
                target_digest = _write_digest(target["channel"], target_value, target["task_path"])
            if target_digest != digest:
                raise MigrationError("target checkpoint write conflict").enrich(table="writes", source_sha256=source_sha256, source_key=source_key)
            if ledger is not None:
                with _row_error_boundary(table="writes", source_sha256=source_sha256, source_key=source_key, code=MigrationErrorCode.CONFLICT):
                    _validate_ledger(ledger, target_table="checkpoint_writes", target_key=source_key, digest=digest)
                already += 1
                continue
            adopted += 1
            if dry_run:
                continue
            status = "adopted"
        else:
            if ledger is not None:
                raise MigrationError("checkpoint write ledger points to missing target").enrich(
                    code=MigrationErrorCode.CONFLICT,
                    table="writes",
                    source_sha256=source_sha256,
                    source_key=source_key,
                )
            if dry_run:
                planned += 1
                continue
            with _row_error_boundary(table="writes", source_sha256=source_sha256, source_key=source_key):
                type_, blob = serde.dumps_typed(row.value)
                await connection.execute(
                    "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, task_path, idx, channel, type, blob) VALUES ($1,$2,$3,$4,'',$5,$6,$7,$8)",
                    row.thread_id,
                    row.checkpoint_ns,
                    row.checkpoint_id,
                    row.task_id,
                    row.idx,
                    row.channel,
                    type_,
                    blob,
                )
                inserted_row = await connection.fetchrow(
                    "SELECT task_path,channel,type,blob FROM checkpoint_writes WHERE thread_id=$1 AND checkpoint_ns=$2 AND checkpoint_id=$3 AND task_id=$4 AND idx=$5",
                    row.thread_id,
                    row.checkpoint_ns,
                    row.checkpoint_id,
                    row.task_id,
                    row.idx,
                )
            if inserted_row is None:
                raise MigrationError("checkpoint write semantic read-back failed").enrich(
                    code=MigrationErrorCode.CONFLICT,
                    table="writes",
                    source_sha256=source_sha256,
                    source_key=source_key,
                )
            with _row_error_boundary(table="writes", source_sha256=source_sha256, source_key=source_key, code=MigrationErrorCode.DECODE):
                inserted_value = serde.loads_typed((inserted_row["type"], bytes(inserted_row["blob"])))
            if _write_digest(inserted_row["channel"], inserted_value, inserted_row["task_path"]) != digest:
                raise MigrationError("checkpoint write semantic read-back failed").enrich(
                    code=MigrationErrorCode.CONFLICT,
                    table="writes",
                    source_sha256=source_sha256,
                    source_key=source_key,
                )
            inserted += 1
            status = "migrated"
        with _row_error_boundary(table="writes", source_sha256=source_sha256, source_key=source_key):
            await _write_ledger(
                connection,
                source_sha256=source_sha256,
                source_table="writes",
                source_key=source_key,
                target_table="checkpoint_writes",
                target_key=source_key,
                row_digest=digest,
                status=status,
            )
    return TableMigrationReport(len(writes), inserted, adopted, already, planned, True)


async def _migrate_writes(
    connection: Any,
    source_sha256: str,
    writes: list[DecodedWrite],
    *,
    dry_run: bool,
    planned_checkpoint_keys: frozenset[tuple[str, str, str]] = frozenset(),
) -> TableMigrationReport:
    async with connection.transaction():
        return await _migrate_writes_rows(
            connection,
            source_sha256,
            writes,
            dry_run=dry_run,
            planned_checkpoint_keys=planned_checkpoint_keys,
        )


async def _verify_writes_with_saver(target_url: str, writes: list[DecodedWrite], source_sha256: str) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    try:
        from scripts.setup_postgres import _asyncpg_url
    except ModuleNotFoundError:
        from setup_postgres import _asyncpg_url
    grouped: dict[tuple[str, str, str], list[DecodedWrite]] = {}
    for row in writes:
        grouped.setdefault((row.thread_id, row.checkpoint_ns, row.checkpoint_id), []).append(row)
    async with AsyncPostgresSaver.from_conn_string(_asyncpg_url(target_url)) as saver:
        for (thread_id, namespace, checkpoint_id), rows in grouped.items():
            source_key = _write_identity(sorted(rows, key=lambda item: (item.task_id, item.idx))[0])
            with _row_error_boundary(
                table="writes",
                source_sha256=source_sha256,
                source_key=source_key,
                code=MigrationErrorCode.CONFLICT,
            ):
                checkpoint = await saver.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": namespace, "checkpoint_id": checkpoint_id}})
                expected = [(row.task_id, row.channel, row.value) for row in sorted(rows, key=lambda item: (item.task_id, item.idx))]
                if checkpoint is None or not _pending_writes_contains(checkpoint.pending_writes, expected):
                    raise MigrationError("checkpoint write Saver semantic read-back failed")


def _pending_writes_contains(actual: list[tuple[Any, Any, Any]], expected: list[tuple[Any, Any, Any]]) -> bool:
    actual_counts = Counter(_json_canonical(item) for item in actual)
    expected_counts = Counter(_json_canonical(item) for item in expected)
    return all(actual_counts[item] >= count for item, count in expected_counts.items())


def _decode_store_rows(source: Path) -> list[NormalizedRow]:
    inspection = inspect_source(source)
    if "store" not in {table.name for table in inspection.inventory.tables}:
        return []
    rows = []
    for row in _read_rows(source, "store"):
        key = _json_canonical([str(row["prefix"]), str(row["key"])])
        with _row_error_boundary(
            table="store",
            source_sha256=inspection.inventory.sha256,
            source_key=key,
            code=MigrationErrorCode.DECODE,
        ):
            value = json.loads(row["value"])
            ttl_minutes = row["ttl_minutes"] if "ttl_minutes" in row.keys() else None
            if ttl_minutes is not None:
                numeric_ttl = float(ttl_minutes)
                if not numeric_ttl.is_integer():
                    raise MigrationError("non-integral store TTL cannot be represented by PostgreSQL provider")
                ttl_minutes = int(numeric_ttl)
            values = {
                "prefix": str(row["prefix"]),
                "key": str(row["key"]),
                "value": value,
                "created_at": _parse_datetime(row["created_at"], table="store", column="created_at"),
                "updated_at": _parse_datetime(row["updated_at"], table="store", column="updated_at"),
                "expires_at": _parse_datetime(row["expires_at"], table="store", column="expires_at") if "expires_at" in row.keys() else None,
                "ttl_minutes": ttl_minutes,
            }
            rows.append(NormalizedRow(key, key, values, hashlib.sha256(_json_canonical(values).encode()).hexdigest()))
    return sorted(rows, key=lambda item: item.source_key.encode())


async def _migrate_store_rows(
    connection: Any,
    source: Path,
    source_sha256: str,
    *,
    dry_run: bool,
) -> TableMigrationReport:
    rows = _decode_store_rows(source)
    inserted = adopted = already = planned = 0
    for row in rows:
        with _row_error_boundary(table="store", source_sha256=source_sha256, source_key=row.source_key):
            ledger = await _ledger_row(connection, source_sha256, "store", row.source_key)
            target = await connection.fetchrow(
                "SELECT prefix, key, value, created_at, updated_at, expires_at, ttl_minutes FROM store WHERE prefix=$1 AND key=$2",
                row.values["prefix"],
                row.values["key"],
            )
        if target is not None:
            with _row_error_boundary(table="store", source_sha256=source_sha256, source_key=row.source_key, code=MigrationErrorCode.DECODE):
                values = dict(target)
                if isinstance(values["value"], str):
                    values["value"] = json.loads(values["value"])
                target_digest = hashlib.sha256(_json_canonical(values).encode()).hexdigest()
            if target_digest != row.digest:
                raise MigrationError("target store conflict").enrich(
                    code=MigrationErrorCode.CONFLICT,
                    table="store",
                    source_sha256=source_sha256,
                    source_key=row.source_key,
                )
            if ledger is not None:
                with _row_error_boundary(table="store", source_sha256=source_sha256, source_key=row.source_key, code=MigrationErrorCode.CONFLICT):
                    _validate_ledger(ledger, target_table="store", target_key=row.target_key, digest=row.digest)
                already += 1
                continue
            adopted += 1
            if dry_run:
                continue
            status = "adopted"
        else:
            if ledger is not None:
                raise MigrationError("store ledger points to missing target").enrich(
                    code=MigrationErrorCode.CONFLICT,
                    table="store",
                    source_sha256=source_sha256,
                    source_key=row.source_key,
                )
            if dry_run:
                planned += 1
                continue
            with _row_error_boundary(table="store", source_sha256=source_sha256, source_key=row.source_key):
                await connection.execute(
                    "INSERT INTO store (prefix,key,value,created_at,updated_at,expires_at,ttl_minutes) VALUES ($1,$2,$3::jsonb,$4,$5,$6,$7)",
                    row.values["prefix"],
                    row.values["key"],
                    _json_canonical(row.values["value"]),
                    row.values["created_at"],
                    row.values["updated_at"],
                    row.values["expires_at"],
                    row.values["ttl_minutes"],
                )
                read_back = await connection.fetchrow(
                    "SELECT prefix,key,value,created_at,updated_at,expires_at,ttl_minutes FROM store WHERE prefix=$1 AND key=$2",
                    row.values["prefix"],
                    row.values["key"],
                )
            if read_back is None:
                raise MigrationError("store semantic read-back failed").enrich(
                    code=MigrationErrorCode.CONFLICT,
                    table="store",
                    source_sha256=source_sha256,
                    source_key=row.source_key,
                )
            with _row_error_boundary(table="store", source_sha256=source_sha256, source_key=row.source_key, code=MigrationErrorCode.DECODE):
                read_values = dict(read_back)
                if isinstance(read_values["value"], str):
                    read_values["value"] = json.loads(read_values["value"])
                if hashlib.sha256(_json_canonical(read_values).encode()).hexdigest() != row.digest:
                    raise MigrationError("store semantic read-back failed")
            inserted += 1
            status = "migrated"
        with _row_error_boundary(table="store", source_sha256=source_sha256, source_key=row.source_key):
            await _write_ledger(
                connection,
                source_sha256=source_sha256,
                source_table="store",
                source_key=row.source_key,
                target_table="store",
                target_key=row.target_key,
                row_digest=row.digest,
                status=status,
            )
    return TableMigrationReport(len(rows), inserted, adopted, already, planned, True)


async def _migrate_store(
    connection: Any,
    source: Path,
    source_sha256: str,
    *,
    dry_run: bool,
) -> TableMigrationReport:
    async with connection.transaction():
        return await _migrate_store_rows(
            connection,
            source,
            source_sha256,
            dry_run=dry_run,
        )


async def _verify_store_rows_with_api(store: Any, rows: list[NormalizedRow], source_sha256: str) -> None:
    for row in rows:
        with _row_error_boundary(
            table="store",
            source_sha256=source_sha256,
            source_key=row.source_key,
            code=MigrationErrorCode.CONFLICT,
        ):
            namespace = tuple(row.values["prefix"].split("."))
            item = await store.aget(namespace, row.values["key"], refresh_ttl=False)
            if item is None or _json_canonical(item.value) != _json_canonical(row.values["value"]):
                raise MigrationError("store public API semantic read-back failed")


async def _verify_store_with_api(target_url: str, source: Path, source_sha256: str) -> None:
    from langgraph.store.postgres.aio import AsyncPostgresStore

    try:
        from scripts.setup_postgres import _asyncpg_url
    except ModuleNotFoundError:
        from setup_postgres import _asyncpg_url
    rows = _decode_store_rows(source)
    async with AsyncPostgresStore.from_conn_string(_asyncpg_url(target_url)) as store:
        await _verify_store_rows_with_api(store, rows, source_sha256)


async def _reset_run_event_sequence(connection: Any) -> None:
    sequence = await connection.fetchval("SELECT pg_get_serial_sequence('run_events', 'id')")
    if sequence:
        maximum = await connection.fetchval("SELECT COALESCE(MAX(id), 0) FROM run_events")
        await connection.execute("SELECT setval($1::regclass, $2, $3)", sequence, max(1, maximum), maximum > 0)
        observed = await connection.fetchval("SELECT nextval($1::regclass)", sequence)
        if observed != maximum + 1:
            raise MigrationError("run_events sequence verification failed")
        await connection.execute("SELECT setval($1::regclass, $2, $3)", sequence, max(1, maximum), maximum > 0)


async def migrate_source(
    source: Path,
    target_url: str,
    dry_run: bool,
    expected_fingerprint: tuple[str, int] | None = None,
    union_reference_keys: frozenset[tuple[str, tuple[str, ...], str]] | None = None,
    planned_checkpoint_keys: frozenset[tuple[str, str, str]] | None = None,
) -> MigrationReport:
    inspection = inspect_source(source)
    _require_fingerprint(inspection.inventory, expected_fingerprint)
    pinned_fingerprint = (inspection.inventory.sha256, inspection.inventory.size_bytes)
    tables = {table.name for table in inspection.inventory.tables}
    reports: dict[str, TableMigrationReport] = {}
    if union_reference_keys is None:
        union_reference_keys = _preflight_cross_source([source]).reference_keys
    planned_checkpoint_keys = planned_checkpoint_keys or frozenset()
    connection = None
    try:
        try:
            from scripts.setup_postgres import _asyncpg_url
        except ModuleNotFoundError:
            from setup_postgres import _asyncpg_url

        connection = await asyncpg.connect(_asyncpg_url(target_url))
        for table in ORM_TABLE_ORDER:
            if table in tables:
                reports[table] = await _migrate_business_table(
                    connection,
                    source,
                    inspection.inventory.sha256,
                    table,
                    dry_run=dry_run,
                    union_reference_keys=union_reference_keys,
                )
        checkpoints, writes = decode_checkpoint_rows(source)
        if checkpoints:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            async with AsyncPostgresSaver.from_conn_string(_asyncpg_url(target_url)) as saver:
                reports["checkpoints"] = await _migrate_checkpoints(
                    connection,
                    saver,
                    inspection.inventory.sha256,
                    checkpoints,
                    dry_run=dry_run,
                )
        if writes:
            reports["writes"] = await _migrate_writes(
                connection,
                inspection.inventory.sha256,
                writes,
                dry_run=dry_run,
                planned_checkpoint_keys=planned_checkpoint_keys | frozenset((row.thread_id, row.checkpoint_ns, row.checkpoint_id) for row in checkpoints),
            )
            if not dry_run:
                await _verify_writes_with_saver(target_url, writes, inspection.inventory.sha256)
        if "store" in tables:
            reports["store"] = await _migrate_store(
                connection,
                source,
                inspection.inventory.sha256,
                dry_run=dry_run,
            )
            if not dry_run:
                await _verify_store_with_api(target_url, source, inspection.inventory.sha256)
        if not dry_run and "run_events" in reports:
            await _reset_run_event_sequence(connection)
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError("SQLite to PostgreSQL migration failed; credentials were redacted") from exc
    finally:
        if connection is not None:
            await connection.close()
    final_inventory = inspect_source(source).inventory
    _require_fingerprint(final_inventory, pinned_fingerprint)
    return MigrationReport(
        source_sha256=inspection.inventory.sha256,
        dry_run=dry_run,
        tables=reports,
        deferred_empty=inspection.deferred_empty,
        verified=all(report.verified for report in reports.values()),
        source_size_bytes=inspection.inventory.size_bytes,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", required=True, type=Path, help="只读 SQLite source；可重复")
    parser.add_argument("--target-url-env", default="DATABASE_URL", help="保存目标 PostgreSQL URL 的环境变量名")
    parser.add_argument("--backup-dir", required=True, type=Path, help="写迁移前的只读备份目录")
    parser.add_argument("--dry-run", action="store_true", help="完整预检，不写目标或 ledger，也不创建备份")
    return parser


def _print_report(source: Path, report: MigrationReport) -> None:
    print(f"来源: {source.name} / SHA256 {report.source_sha256[:12]}")
    for table, summary in report.tables.items():
        print(f"  {table}: rows={summary.source_rows} inserted={summary.inserted} adopted={summary.adopted} already-migrated={summary.already_migrated} planned={summary.planned_insert} verified={'是' if summary.verified else '否'}")
    for table in report.deferred_empty:
        print(f"  {table}: deferred-empty")


async def _run_cli(args: argparse.Namespace, target_url: str) -> None:
    union_plan = _preflight_cross_source(args.source)
    preflights = []
    for index, source in enumerate(args.source):
        preflight = await migrate_source(
            source,
            target_url,
            dry_run=True,
            union_reference_keys=union_plan.per_source_reference_keys[index],
            planned_checkpoint_keys=union_plan.per_source_checkpoint_keys[index],
        )
        if not preflight.verified:
            raise MigrationError("dry-run verification failed")
        if (preflight.source_sha256, preflight.source_size_bytes) != union_plan.source_fingerprints[index]:
            raise MigrationError("SQLite source fingerprint changed after ordered plan")
        preflights.append((source, preflight))
    if args.dry_run:
        for source, report in preflights:
            _print_report(source, report)
            print("  未创建备份（dry-run）")
        return

    frozen_sources = []
    for source, _report in preflights:
        backup = backup_source(
            source,
            args.backup_dir,
            (_report.source_sha256, _report.source_size_bytes),
        )
        frozen_sources.append((source, _report, backup))
        print(f"备份: {backup.path.name} / SHA256 {backup.sha256[:12]}")
    snapshot_paths = [item[2].path for item in frozen_sources]
    snapshot_plan = _preflight_cross_source(snapshot_paths)
    if snapshot_plan.source_fingerprints != union_plan.source_fingerprints:
        raise MigrationError("backup snapshot plan fingerprint mismatch")
    for index, snapshot in enumerate(snapshot_paths):
        await migrate_source(
            snapshot,
            target_url,
            dry_run=True,
            expected_fingerprint=snapshot_plan.source_fingerprints[index],
            union_reference_keys=snapshot_plan.per_source_reference_keys[index],
            planned_checkpoint_keys=snapshot_plan.per_source_checkpoint_keys[index],
        )
    for index, (source, _report, backup) in enumerate(frozen_sources):
        report = await migrate_source(
            backup.path,
            target_url,
            dry_run=False,
            expected_fingerprint=(_report.source_sha256, _report.source_size_bytes),
            union_reference_keys=snapshot_plan.per_source_reference_keys[index],
            planned_checkpoint_keys=snapshot_plan.per_source_checkpoint_keys[index],
        )
        if not report.verified:
            raise MigrationError("migration verification failed")
        _print_report(source, report)


def _preflight_cross_source(sources: list[Path]) -> UnionPlan:
    seen: dict[tuple[str, str], str] = {}

    def register(table: str, key: str, digest: str) -> None:
        identity = (table, key)
        previous = seen.get(identity)
        if previous is not None and previous != digest:
            raise MigrationError(f"cross-source target conflict in {table}")
        seen[identity] = digest

    business_rows: list[tuple[str, NormalizedRow]] = []
    source_fingerprints = []
    for source in sources:
        inspection = inspect_source(source)
        source_fingerprints.append((inspection.inventory.sha256, inspection.inventory.size_bytes))
        table_names = {table.name for table in inspection.inventory.tables}
        for table in ORM_TABLE_ORDER:
            if table in table_names:
                for row in normalize_business_rows(source, table):
                    business_rows.append((table, row))
                    register(table, row.target_key, row.digest)
                    for constraint_name, unique_key in _business_unique_keys(table, row.values):
                        register(f"{table}.{constraint_name}", unique_key, row.digest)
        checkpoints, writes = decode_checkpoint_rows(source)
        for row in checkpoints:
            register("checkpoints", _checkpoint_identity(row), _checkpoint_digest(row))
            versions = row.checkpoint.get("channel_versions", {})
            for channel, value in row.checkpoint.get("channel_values", {}).items():
                if value is None or isinstance(value, (str, int, float, bool)):
                    continue
                if channel not in versions:
                    raise MigrationError("checkpoint blob channel has no version")
                blob_key = _json_canonical([row.thread_id, row.checkpoint_ns, channel, str(versions[channel])])
                blob_digest = hashlib.sha256(_json_canonical(value).encode()).hexdigest()
                register("checkpoint_blobs", blob_key, blob_digest)
        for row in writes:
            digest = _write_digest(row.channel, row.value)
            register("checkpoint_writes", _write_identity(row), digest)
        for row in _decode_store_rows(source):
            register("store", row.target_key, row.digest)
    import deerflow.persistence.models  # noqa: F401
    from deerflow.persistence.base import Base

    referenced_specs: set[tuple[str, tuple[str, ...]]] = set()
    for table in Base.metadata.tables.values():
        for constraint in table.foreign_key_constraints:
            referenced_specs.add(
                (
                    constraint.elements[0].column.table.name,
                    tuple(element.column.name for element in constraint.elements),
                )
            )
    reference_keys = {(table, columns, _json_canonical([row.values[column] for column in columns])) for table, row in business_rows for referenced_table, columns in referenced_specs if table == referenced_table}
    accumulated_refs: set[tuple[str, tuple[str, ...], str]] = set()
    accumulated_checkpoints: set[tuple[str, str, str]] = set()
    per_source_refs = []
    per_source_checkpoints = []
    for source in sources:
        inspection = inspect_source(source)
        table_names = {table.name for table in inspection.inventory.tables}
        for table in ORM_TABLE_ORDER:
            if table not in table_names:
                continue
            for row in normalize_business_rows(source, table):
                for referenced_table, columns in referenced_specs:
                    if table == referenced_table:
                        accumulated_refs.add((table, columns, _json_canonical([row.values[column] for column in columns])))
        checkpoints, _writes = decode_checkpoint_rows(source)
        per_source_refs.append(frozenset(accumulated_refs))
        per_source_checkpoints.append(frozenset(accumulated_checkpoints))
        accumulated_checkpoints.update((row.thread_id, row.checkpoint_ns, row.checkpoint_id) for row in checkpoints)
    return UnionPlan(
        frozenset(reference_keys),
        tuple(per_source_refs),
        tuple(per_source_checkpoints),
        tuple(source_fingerprints),
    )


def _business_unique_keys(table_name: str, values: dict[str, Any]) -> list[tuple[str, str]]:
    import deerflow.persistence.models  # noqa: F401
    from deerflow.persistence.base import Base

    table = Base.metadata.tables[table_name]
    results: list[tuple[str, str]] = []
    for constraint in table.constraints:
        if constraint.__class__.__name__ != "UniqueConstraint":
            continue
        columns = tuple(column.name for column in constraint.columns)
        if any(values[column] is None for column in columns):
            continue
        results.append((constraint.name or "+".join(columns), _json_canonical([values[column] for column in columns])))
    for index in table.indexes:
        if not index.unique:
            continue
        columns = tuple(column.name for column in index.columns)
        if any(values[column] is None for column in columns):
            continue
        if index.name == "uq_channel_connection_active_identity" and values.get("status") == "revoked":
            continue
        results.append((index.name, _json_canonical([values[column] for column in columns])))
    return results


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    target_url = os.getenv(args.target_url_env)
    if not target_url:
        print(f"错误: 必须设置 {args.target_url_env}", file=sys.stderr)
        return 2
    try:
        asyncio.run(_run_cli(args, target_url))
    except MigrationError as exc:
        print(f"错误: SQLite 到 PostgreSQL 迁移失败；{exc.safe_fields()}；已隐藏凭据和业务数据", file=sys.stderr)
        return 1
    except ValueError:
        print("错误: SQLite 到 PostgreSQL 迁移失败；code=migration；已隐藏凭据和业务数据", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
