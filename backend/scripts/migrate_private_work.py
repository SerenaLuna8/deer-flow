#!/usr/bin/env python3
"""Explicitly move legacy private work into project scope."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from deerflow.persistence.bootstrap import _get_alembic_config


class PrivateWorkMigrationError(RuntimeError):
    """Stable, private-data-safe migration failure."""


@dataclass(frozen=True)
class LegacyOwnerInventory:
    owner_user_id: str = field(repr=False)
    thread_count: int = 0
    run_count: int = 0
    event_count: int = 0
    feedback_count: int = 0


@dataclass(frozen=True)
class PrivateWorkInventory:
    source_fingerprint: str
    owners: tuple[LegacyOwnerInventory, ...]
    checkpoint_count: int
    filesystem_source_count: int


@dataclass(frozen=True)
class OwnerTarget:
    owner_user_id: str = field(repr=False)
    project_id: uuid.UUID


@dataclass(frozen=True)
class PrivateWorkMigrationPlan:
    source_fingerprint: str
    owner_targets: tuple[OwnerTarget, ...]
    checkpoint_count: int


@dataclass(frozen=True)
class PrivateWorkMigrationReport:
    mode: str
    counts: dict[str, int]
    source_key_hash: str
    backup_written: bool = False
    cutover_complete: bool = False
    empty_install: bool = False
    noop: bool = False


_CORE_TABLES: tuple[str, ...] = (
    "threads_meta",
    "runs",
    "run_events",
    "feedback",
)
_CORE_LEGACY_COLUMNS: dict[str, tuple[str, ...]] = {
    "threads_meta": (
        "thread_id",
        "assistant_id",
        "user_id",
        "display_name",
        "status",
        "metadata_json",
        "created_at",
        "updated_at",
    ),
    "runs": (
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
    ),
    "run_events": (
        "id",
        "thread_id",
        "run_id",
        "user_id",
        "event_type",
        "category",
        "content",
        "event_metadata",
        "seq",
        "created_at",
    ),
    "feedback": (
        "feedback_id",
        "run_id",
        "thread_id",
        "user_id",
        "message_id",
        "rating",
        "comment",
        "created_at",
    ),
}
_ORDER_COLUMNS: dict[str, str] = {
    "threads_meta": "thread_id",
    "runs": "run_id",
    "run_events": "id",
    "feedback": "feedback_id",
}
_FINALIZE_DOMAINS: tuple[str, ...] = (
    "threads",
    "runs",
    "run_events",
    "feedback",
    "checkpoints",
    "files",
    "memory",
    "channel_connections",
    "channel_oauth_states",
    "channel_conversations",
    "counts_probe",
    "scope_probe",
)
_UNSUPPORTED_DATABASE_TABLES: tuple[str, ...] = (
    "files",
    "file_chunks",
    "artifacts",
    "user_project_memories",
    "user_project_memory_facts",
    "channel_connections",
    "channel_oauth_states",
    "channel_conversations",
)


def load_owner_map(path: Path) -> dict[str, uuid.UUID]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError
        result: dict[str, uuid.UUID] = {}
        for owner, project in raw.items():
            if not isinstance(owner, str) or not isinstance(project, str):
                raise ValueError
            canonical_owner = str(uuid.UUID(owner))
            if canonical_owner != owner:
                raise ValueError
            project_id = uuid.UUID(project)
            if canonical_owner in result:
                raise ValueError
            result[canonical_owner] = project_id
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise PrivateWorkMigrationError("owner map is invalid") from None


def build_migration_plan(
    inventory: PrivateWorkInventory,
    owner_map: Mapping[str, uuid.UUID],
) -> PrivateWorkMigrationPlan:
    if inventory.filesystem_source_count:
        raise PrivateWorkMigrationError("unsupported legacy source is present")
    inventory_owners = {owner.owner_user_id for owner in inventory.owners}
    missing = inventory_owners - set(owner_map)
    if missing:
        raise PrivateWorkMigrationError("owner map incomplete")
    targets = tuple(OwnerTarget(owner_user_id=owner, project_id=owner_map[owner]) for owner in sorted(inventory_owners))
    return PrivateWorkMigrationPlan(
        source_fingerprint=inventory.source_fingerprint,
        owner_targets=targets,
        checkpoint_count=inventory.checkpoint_count,
    )


def render_inventory(inventory: PrivateWorkInventory) -> str:
    counts = {
        "legacy_owners": len(inventory.owners),
        "threads": sum(owner.thread_count for owner in inventory.owners),
        "runs": sum(owner.run_count for owner in inventory.owners),
        "run_events": sum(owner.event_count for owner in inventory.owners),
        "feedback": sum(owner.feedback_count for owner in inventory.owners),
        "checkpoints": inventory.checkpoint_count,
        "filesystem_sources": inventory.filesystem_source_count,
    }
    return json.dumps(
        {
            "counts": counts,
            "source_key_hash": inventory.source_fingerprint[:12],
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="inventory and validate without writes")
    mode.add_argument("--execute", action="store_true", help="perform the staged migration")
    parser.add_argument("--owner-map", required=True, type=Path, help="JSON mapping of legacy owner UUID to active project UUID")
    parser.add_argument("--backup-dir", required=True, type=Path, help="operator-managed backup directory")
    parser.add_argument("--repo-root", type=Path, default=_default_repo_root())
    parser.add_argument("--data-root", type=Path)
    return parser


def _digest_json(value: object) -> str:
    encoded = json.dumps(
        _canonical_value(value),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (datetime, date, uuid.UUID)):
        return str(value)
    if isinstance(value, bytes):
        return {
            "sha256": hashlib.sha256(value).hexdigest(),
            "size": len(value),
        }
    return value


async def _table_exists(connection: AsyncConnection, table: str) -> bool:
    return (
        await connection.scalar(
            text("SELECT to_regclass(:table) IS NOT NULL"),
            {"table": table},
        )
        is True
    )


async def _table_columns(connection: AsyncConnection, table: str) -> set[str]:
    rows = await connection.execute(
        text(
            """SELECT column_name
            FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name=:table"""
        ),
        {"table": table},
    )
    return set(rows.scalars())


async def _owner_column(connection: AsyncConnection, table: str) -> str:
    columns = await _table_columns(connection, table)
    if "owner_user_id" in columns:
        return "owner_user_id"
    if "user_id" in columns:
        return "user_id"
    raise PrivateWorkMigrationError("legacy owner column is unavailable")


async def _legacy_core_rows(
    connection: AsyncConnection,
) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for table in _CORE_TABLES:
        if not await _table_exists(connection, table):
            result[table] = []
            continue
        owner_column = await _owner_column(connection, table)
        selections = [f'"{owner_column}" AS "user_id"' if column == "user_id" else f'"{column}"' for column in _CORE_LEGACY_COLUMNS[table]]
        statement = text(
            f'SELECT {", ".join(selections)} FROM "{table}" ORDER BY "{_ORDER_COLUMNS[table]}"'  # noqa: S608 - fixed internal identifiers
        )
        rows = (await connection.execute(statement)).mappings().all()
        result[table] = [dict(row) for row in rows]
    return result


def _filesystem_source_count(data_root: Path) -> int:
    if not data_root.exists():
        return 0
    count = 0
    memory_candidates = [data_root / "memory.json"]
    memory_candidates.extend(data_root.glob("agents/*/memory.json"))
    memory_candidates.extend(data_root.glob("users/*/memory.json"))
    memory_candidates.extend(data_root.glob("users/*/agents/*/memory.json"))
    count += sum(1 for path in memory_candidates if path.is_file() or path.is_symlink())
    for pattern in ("threads/*/user-data", "users/*/threads/*/user-data"):
        for user_data in data_root.glob(pattern):
            for directory_name in ("uploads", "workspace", "outputs"):
                directory = user_data / directory_name
                if directory.is_dir() and any(path.is_file() or path.is_symlink() for path in directory.rglob("*")):
                    count += 1
    return count


def _checkpoint_metadata_without_scope(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    return {key: item for key, item in value.items() if key != "deerflow_private_scope"}


async def _checkpoint_inventory(
    connection: AsyncConnection,
) -> tuple[int, dict[str, list[dict[str, object]]]]:
    snapshot: dict[str, list[dict[str, object]]] = {}
    checkpoint_count = 0
    for table, order in (
        (
            "checkpoints",
            "thread_id, checkpoint_ns, checkpoint_id",
        ),
        (
            "checkpoint_blobs",
            "thread_id, checkpoint_ns, channel, version",
        ),
        (
            "checkpoint_writes",
            "thread_id, checkpoint_ns, checkpoint_id, task_id, idx",
        ),
    ):
        if not await _table_exists(connection, table):
            snapshot[table] = []
            continue
        rows = (
            (
                await connection.execute(
                    text(
                        f'SELECT * FROM "{table}" ORDER BY {order}'  # noqa: S608 - fixed internal identifiers
                    )
                )
            )
            .mappings()
            .all()
        )
        normalized = [dict(row) for row in rows]
        if table == "checkpoints":
            checkpoint_count = len(normalized)
            for row in normalized:
                row["metadata"] = _checkpoint_metadata_without_scope(row.get("metadata"))
        snapshot[table] = normalized
    return checkpoint_count, snapshot


async def _collect_inventory(
    engine: AsyncEngine,
    data_root: Path,
) -> PrivateWorkInventory:
    async with engine.connect() as connection:
        rows = await _legacy_core_rows(connection)
        checkpoint_count, checkpoint_rows = await _checkpoint_inventory(connection)

    owner_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "threads_meta": 0,
            "runs": 0,
            "run_events": 0,
            "feedback": 0,
        }
    )
    for table, table_rows in rows.items():
        for row in table_rows:
            owner = row.get("user_id")
            if not isinstance(owner, str) or not owner:
                raise PrivateWorkMigrationError("legacy owner is missing")
            try:
                canonical_owner = str(uuid.UUID(owner))
            except ValueError:
                raise PrivateWorkMigrationError("legacy owner is invalid") from None
            if canonical_owner != owner:
                raise PrivateWorkMigrationError("legacy owner is invalid")
            owner_counts[owner][table] += 1

    source_fingerprint = _digest_json(
        {
            "core": rows,
            "checkpoints": checkpoint_rows,
        }
    )
    owners = tuple(
        LegacyOwnerInventory(
            owner_user_id=owner,
            thread_count=counts["threads_meta"],
            run_count=counts["runs"],
            event_count=counts["run_events"],
            feedback_count=counts["feedback"],
        )
        for owner, counts in sorted(owner_counts.items())
    )
    return PrivateWorkInventory(
        source_fingerprint=source_fingerprint,
        owners=owners,
        checkpoint_count=checkpoint_count,
        filesystem_source_count=await asyncio.to_thread(
            _filesystem_source_count,
            data_root,
        ),
    )


def _report_counts(inventory: PrivateWorkInventory) -> dict[str, int]:
    return {
        "threads": sum(owner.thread_count for owner in inventory.owners),
        "runs": sum(owner.run_count for owner in inventory.owners),
        "run_events": sum(owner.event_count for owner in inventory.owners),
        "feedback": sum(owner.feedback_count for owner in inventory.owners),
        "checkpoints": inventory.checkpoint_count,
    }


def _normalize_owner_map(
    owner_map: Mapping[str, uuid.UUID | str],
) -> dict[str, uuid.UUID]:
    try:
        normalized: dict[str, uuid.UUID] = {}
        for owner, project in owner_map.items():
            canonical_owner = str(uuid.UUID(owner))
            if canonical_owner != owner:
                raise ValueError
            normalized[canonical_owner] = uuid.UUID(str(project))
        return normalized
    except (TypeError, ValueError):
        raise PrivateWorkMigrationError("owner map is invalid") from None


async def _current_revision(connection: AsyncConnection) -> str:
    if not await _table_exists(connection, "alembic_version"):
        raise PrivateWorkMigrationError("versioned PostgreSQL database is required")
    value = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    if not isinstance(value, str):
        raise PrivateWorkMigrationError("database revision is unavailable")
    return value


async def _cutover_complete(connection: AsyncConnection) -> bool:
    if not await _table_exists(connection, "private_work_cutover_state"):
        return False
    value = await connection.scalar(
        text(
            """SELECT stage='cutover_complete' AND cutover_at IS NOT NULL
            FROM private_work_cutover_state WHERE id=1"""
        )
    )
    return value is True


async def _validate_owner_targets(
    connection: AsyncConnection,
    plan: PrivateWorkMigrationPlan,
) -> None:
    for target in plan.owner_targets:
        valid = await connection.scalar(
            text(
                """SELECT EXISTS (
                    SELECT 1
                    FROM projects project
                    JOIN project_memberships membership
                      ON membership.project_id=project.id
                    WHERE project.id=:project
                      AND project.status='active'
                      AND project.is_suspended=false
                      AND membership.user_id=:owner
                      AND membership.status='active'
                )"""
            ),
            {
                "project": target.project_id,
                "owner": target.owner_user_id,
            },
        )
        if valid is not True:
            raise PrivateWorkMigrationError("mapped project membership is unavailable")


async def _validate_relations(connection: AsyncConnection) -> None:
    owner_columns = {table: await _owner_column(connection, table) for table in _CORE_TABLES}
    mismatched_run = await connection.scalar(
        text(
            f"""SELECT EXISTS (
                SELECT 1 FROM runs run
                LEFT JOIN threads_meta thread ON thread.thread_id=run.thread_id
                WHERE thread.thread_id IS NULL
                   OR thread.{owner_columns["threads_meta"]} <> run.{owner_columns["runs"]}
            )"""  # noqa: S608 - fixed owner column alternatives
        )
    )
    if mismatched_run:
        raise PrivateWorkMigrationError("legacy scope graph conflicts")
    for table in ("run_events", "feedback"):
        mismatch = await connection.scalar(
            text(
                f"""SELECT EXISTS (
                    SELECT 1 FROM {table} child
                    LEFT JOIN runs run
                      ON run.run_id=child.run_id AND run.thread_id=child.thread_id
                    WHERE run.run_id IS NULL
                       OR run.{owner_columns["runs"]} <> child.{owner_columns[table]}
                )"""  # noqa: S608 - fixed table and owner column alternatives
            )
        )
        if mismatch:
            raise PrivateWorkMigrationError("legacy scope graph conflicts")
    if await _table_exists(connection, "checkpoints"):
        orphan = await connection.scalar(
            text(
                """SELECT EXISTS (
                    SELECT 1 FROM checkpoints checkpoint
                    LEFT JOIN threads_meta thread
                      ON thread.thread_id=checkpoint.thread_id
                    WHERE thread.thread_id IS NULL
                )"""
            )
        )
        if orphan:
            raise PrivateWorkMigrationError("checkpoint scope graph conflicts")
    if await _table_exists(connection, "checkpoint_blobs"):
        orphan_blob = await connection.scalar(
            text(
                """SELECT EXISTS (
                    SELECT 1 FROM checkpoint_blobs blob
                    WHERE NOT EXISTS (
                        SELECT 1 FROM checkpoints checkpoint
                        WHERE checkpoint.thread_id=blob.thread_id
                          AND checkpoint.checkpoint_ns=blob.checkpoint_ns
                    )
                )"""
            )
        )
        if orphan_blob:
            raise PrivateWorkMigrationError("checkpoint scope graph conflicts")
    if await _table_exists(connection, "checkpoint_writes"):
        orphan_write = await connection.scalar(
            text(
                """SELECT EXISTS (
                    SELECT 1 FROM checkpoint_writes write
                    WHERE NOT EXISTS (
                        SELECT 1 FROM checkpoints checkpoint
                        WHERE checkpoint.thread_id=write.thread_id
                          AND checkpoint.checkpoint_ns=write.checkpoint_ns
                          AND checkpoint.checkpoint_id=write.checkpoint_id
                    )
                )"""
            )
        )
        if orphan_write:
            raise PrivateWorkMigrationError("checkpoint scope graph conflicts")


async def _validate_unsupported_tables_empty(
    connection: AsyncConnection,
) -> None:
    for table in _UNSUPPORTED_DATABASE_TABLES:
        if not await _table_exists(connection, table):
            continue
        has_rows = await connection.scalar(
            text(
                f'SELECT EXISTS (SELECT 1 FROM "{table}" LIMIT 1)'  # noqa: S608 - fixed table allowlist
            )
        )
        if has_rows:
            raise PrivateWorkMigrationError("unsupported legacy source is present")


def _agent_source_key(assistant_id: object) -> str:
    if assistant_id is None or assistant_id == "" or assistant_id == "lead-agent":
        return "system-agent:lead-agent"
    if not isinstance(assistant_id, str) or len(assistant_id) > 128:
        raise PrivateWorkMigrationError("legacy agent mapping is unavailable")
    return f"system-agent:{assistant_id}"


async def _resolve_thread_agents(
    connection: AsyncConnection,
) -> dict[str, tuple[uuid.UUID, str]]:
    rows = (
        (
            await connection.execute(
                text(
                    """SELECT thread_id, assistant_id
                FROM threads_meta ORDER BY thread_id"""
                )
            )
        )
        .mappings()
        .all()
    )
    resolved: dict[str, tuple[uuid.UUID, str]] = {}
    by_source: dict[str, tuple[uuid.UUID, str]] = {}
    for row in rows:
        source_key = _agent_source_key(row["assistant_id"])
        agent = by_source.get(source_key)
        if agent is None:
            agent_row = (
                await connection.execute(
                    text(
                        """SELECT id, scope FROM agents
                        WHERE source_key=:source_key
                          AND status='active'
                          AND current_published_version_id IS NOT NULL"""
                    ),
                    {"source_key": source_key},
                )
            ).one_or_none()
            if agent_row is None:
                raise PrivateWorkMigrationError("legacy agent mapping is unavailable")
            agent = (uuid.UUID(str(agent_row.id)), str(agent_row.scope))
            by_source[source_key] = agent
        resolved[str(row["thread_id"])] = agent
    return resolved


async def _assert_source_fingerprint(
    engine: AsyncEngine,
    data_root: Path,
    expected: str,
) -> None:
    current = await _collect_inventory(engine, data_root)
    if current.source_fingerprint != expected:
        raise PrivateWorkMigrationError("legacy source fingerprint changed")


async def _migration_run_id(
    connection: AsyncConnection,
    *,
    source_fingerprint: str,
    owner_map_digest: str,
) -> uuid.UUID:
    existing = (
        await connection.execute(
            text(
                """SELECT id FROM private_work_migration_runs
                WHERE mode='execute'
                  AND source_fingerprint=:source
                  AND owner_map_digest=:owner_map
                  AND status IN ('running','completed')
                ORDER BY started_at DESC, id DESC
                LIMIT 1"""
            ),
            {"source": source_fingerprint, "owner_map": owner_map_digest},
        )
    ).scalar_one_or_none()
    if existing is not None:
        return uuid.UUID(str(existing))
    run_id = uuid.uuid4()
    await connection.execute(
        text(
            """INSERT INTO private_work_migration_runs
            (id,mode,status,source_fingerprint,owner_map_digest,
             database_backup_proof_digest,legacy_source_probe_complete,
             checkpoint_marker_probe_complete,cross_scope_probe_complete,started_at)
            VALUES (:id,'execute','running',:source,:owner_map,NULL,false,false,false,now())"""
        ),
        {
            "id": run_id,
            "source": source_fingerprint,
            "owner_map": owner_map_digest,
        },
    )
    return run_id


def _domain_digest(
    domain: str,
    source_fingerprint: str,
    row_count: int,
    plan: PrivateWorkMigrationPlan,
) -> str:
    return _digest_json(
        {
            "domain": domain,
            "source": source_fingerprint,
            "rows": row_count,
            "targets": [
                {
                    "owner_hash": hashlib.sha256(target.owner_user_id.encode("utf-8")).hexdigest(),
                    "project": str(target.project_id),
                }
                for target in plan.owner_targets
            ],
        }
    )


async def _write_ledger(
    connection: AsyncConnection,
    *,
    run_id: uuid.UUID,
    domain: str,
    source_fingerprint: str,
    row_count: int,
    plan: PrivateWorkMigrationPlan,
) -> None:
    source_key_hash = hashlib.sha256(f"private-work:{domain}".encode()).hexdigest()
    target_digest = _domain_digest(
        domain,
        source_fingerprint,
        row_count,
        plan,
    )
    existing = (
        await connection.execute(
            text(
                """SELECT source_fingerprint,target_digest,status,row_count
                FROM private_work_migration_ledger
                WHERE migration_run_id=:run_id
                  AND domain=:domain
                  AND source_key_hash=:source_key"""
            ),
            {
                "run_id": run_id,
                "domain": domain,
                "source_key": source_key_hash,
            },
        )
    ).one_or_none()
    if existing is not None:
        if existing.source_fingerprint != source_fingerprint or existing.target_digest != target_digest or existing.status != "complete" or int(existing.row_count) != row_count:
            raise PrivateWorkMigrationError("migration ledger conflicts")
        return
    await connection.execute(
        text(
            """INSERT INTO private_work_migration_ledger
            (migration_run_id,domain,source_key_hash,source_fingerprint,
             target_digest,status,row_count,byte_count,completed_at)
            VALUES (:run_id,:domain,:source_key,:source,:target,
                    'complete',:rows,0,now())"""
        ),
        {
            "run_id": run_id,
            "domain": domain,
            "source_key": source_key_hash,
            "source": source_fingerprint,
            "target": target_digest,
            "rows": row_count,
        },
    )


async def _apply_threads(
    connection: AsyncConnection,
    plan: PrivateWorkMigrationPlan,
    agents: Mapping[str, tuple[uuid.UUID, str]],
) -> int:
    targets = {target.owner_user_id: target.project_id for target in plan.owner_targets}
    rows = (await connection.execute(text("SELECT thread_id,user_id,project_id FROM threads_meta ORDER BY thread_id"))).mappings().all()
    for row in rows:
        owner = str(row["user_id"])
        project = targets.get(owner)
        agent = agents.get(str(row["thread_id"]))
        if project is None or agent is None:
            raise PrivateWorkMigrationError("migration plan conflicts")
        existing_project = row["project_id"]
        if existing_project is not None and uuid.UUID(str(existing_project)) != project:
            raise PrivateWorkMigrationError("target scope conflicts")
        await connection.execute(
            text(
                """UPDATE threads_meta
                SET project_id=:project,
                    agent_asset_id=:agent,
                    agent_scope=:scope,
                    checkpoint_delete_status=COALESCE(checkpoint_delete_status,'not_requested'),
                    version=COALESCE(version,1)
                WHERE thread_id=:thread AND user_id=:owner"""
            ),
            {
                "project": project,
                "agent": agent[0],
                "scope": agent[1],
                "thread": row["thread_id"],
                "owner": owner,
            },
        )
    return len(rows)


async def _apply_owner_domain(
    connection: AsyncConnection,
    *,
    table: str,
    plan: PrivateWorkMigrationPlan,
) -> int:
    count = 0
    for target in plan.owner_targets:
        values = {"project": target.project_id, "owner": target.owner_user_id}
        if table == "runs":
            statement = text(
                """UPDATE runs
                SET project_id=:project,
                    finalization_status=COALESCE(finalization_status,'complete')
                WHERE user_id=:owner
                  AND (project_id IS NULL OR project_id=:project)"""
            )
        else:
            statement = text(
                f"""UPDATE {table}
                SET project_id=:project
                WHERE user_id=:owner
                  AND (project_id IS NULL OR project_id=:project)"""  # noqa: S608 - fixed domain table allowlist
            )
        result = await connection.execute(statement, values)
        count += int(result.rowcount or 0)
    return count


async def _apply_checkpoints(connection: AsyncConnection) -> int:
    if not await _table_exists(connection, "checkpoints"):
        return 0
    mismatch = await connection.scalar(
        text(
            """SELECT EXISTS (
                SELECT 1
                FROM checkpoints checkpoint
                JOIN threads_meta thread ON thread.thread_id=checkpoint.thread_id
                WHERE checkpoint.metadata ? 'deerflow_private_scope'
                  AND checkpoint.metadata -> 'deerflow_private_scope'
                      <> jsonb_build_object(
                          'project_id',thread.project_id::text,
                          'owner_user_id',thread.user_id
                      )
            )"""
        )
    )
    if mismatch:
        raise PrivateWorkMigrationError("checkpoint target scope conflicts")
    result = await connection.execute(
        text(
            """UPDATE checkpoints checkpoint
            SET metadata=jsonb_set(
                COALESCE(checkpoint.metadata,'{}'::jsonb),
                '{deerflow_private_scope}',
                jsonb_build_object(
                    'project_id',thread.project_id::text,
                    'owner_user_id',thread.user_id
                ),
                true
            )
            FROM threads_meta thread
            WHERE thread.thread_id=checkpoint.thread_id"""
        )
    )
    return int(result.rowcount or 0)


async def _scope_probe(connection: AsyncConnection) -> None:
    for table in _CORE_TABLES:
        has_null = await connection.scalar(
            text(
                f"""SELECT EXISTS (
                    SELECT 1 FROM {table}
                    WHERE project_id IS NULL OR user_id IS NULL
                )"""  # noqa: S608 - fixed core table allowlist
            )
        )
        if has_null:
            raise PrivateWorkMigrationError("target scope probe failed")
    if await _table_exists(connection, "checkpoints"):
        invalid_marker = await connection.scalar(
            text(
                """SELECT EXISTS (
                    SELECT 1
                    FROM checkpoints checkpoint
                    JOIN threads_meta thread ON thread.thread_id=checkpoint.thread_id
                    WHERE checkpoint.metadata #>> '{deerflow_private_scope,project_id}'
                            IS DISTINCT FROM thread.project_id::text
                       OR checkpoint.metadata #>> '{deerflow_private_scope,owner_user_id}'
                            IS DISTINCT FROM thread.user_id
                )"""
            )
        )
        if invalid_marker:
            raise PrivateWorkMigrationError("checkpoint scope probe failed")


async def _execute_staging(
    engine: AsyncEngine,
    *,
    plan: PrivateWorkMigrationPlan,
    inventory: PrivateWorkInventory,
    data_root: Path,
    owner_map_digest: str,
) -> uuid.UUID:
    await _assert_source_fingerprint(
        engine,
        data_root,
        inventory.source_fingerprint,
    )
    async with engine.begin() as connection:
        await _validate_owner_targets(connection, plan)
        await _validate_relations(connection)
        await _validate_unsupported_tables_empty(connection)
        agents = await _resolve_thread_agents(connection)
        run_id = await _migration_run_id(
            connection,
            source_fingerprint=inventory.source_fingerprint,
            owner_map_digest=owner_map_digest,
        )

    counts = _report_counts(inventory)
    domain_actions = (
        (
            "threads",
            counts["threads"],
            lambda connection: _apply_threads(connection, plan, agents),
        ),
        (
            "runs",
            counts["runs"],
            lambda connection: _apply_owner_domain(
                connection,
                table="runs",
                plan=plan,
            ),
        ),
        (
            "run_events",
            counts["run_events"],
            lambda connection: _apply_owner_domain(
                connection,
                table="run_events",
                plan=plan,
            ),
        ),
        (
            "feedback",
            counts["feedback"],
            lambda connection: _apply_owner_domain(
                connection,
                table="feedback",
                plan=plan,
            ),
        ),
        (
            "checkpoints",
            counts["checkpoints"],
            _apply_checkpoints,
        ),
    )
    for domain, expected_count, action in domain_actions:
        await _assert_source_fingerprint(
            engine,
            data_root,
            inventory.source_fingerprint,
        )
        async with engine.begin() as connection:
            actual_count = await action(connection)
            if actual_count != expected_count:
                raise PrivateWorkMigrationError("migration row count conflicts")
            await _write_ledger(
                connection,
                run_id=run_id,
                domain=domain,
                source_fingerprint=inventory.source_fingerprint,
                row_count=expected_count,
                plan=plan,
            )

    for domain in (
        "files",
        "memory",
        "channel_connections",
        "channel_oauth_states",
        "channel_conversations",
    ):
        async with engine.begin() as connection:
            await _validate_unsupported_tables_empty(connection)
            await _write_ledger(
                connection,
                run_id=run_id,
                domain=domain,
                source_fingerprint=inventory.source_fingerprint,
                row_count=0,
                plan=plan,
            )

    async with engine.begin() as connection:
        await _scope_probe(connection)
        for domain in ("counts_probe", "scope_probe"):
            await _write_ledger(
                connection,
                run_id=run_id,
                domain=domain,
                source_fingerprint=inventory.source_fingerprint,
                row_count=0,
                plan=plan,
            )
        completed_domains = set(
            (
                await connection.execute(
                    text(
                        """SELECT domain
                        FROM private_work_migration_ledger
                        WHERE migration_run_id=:run_id AND status='complete'"""
                    ),
                    {"run_id": run_id},
                )
            ).scalars()
        )
        if not set(_FINALIZE_DOMAINS) <= completed_domains:
            raise PrivateWorkMigrationError("migration ledger is incomplete")
        await connection.execute(
            text(
                """UPDATE private_work_migration_runs
                SET status='completed',
                    legacy_source_probe_complete=true,
                    checkpoint_marker_probe_complete=true,
                    cross_scope_probe_complete=true,
                    completed_at=now()
                WHERE id=:run_id"""
            ),
            {"run_id": run_id},
        )
        await connection.execute(
            text(
                """INSERT INTO private_work_cutover_state
                (id,stage,migration_run_id,empty_domain_probe_complete,
                 checkpoint_marker_probe_complete,cutover_at,updated_at)
                VALUES (1,'migration_ready',:run_id,true,true,NULL,now())
                ON CONFLICT (id) DO UPDATE SET
                    stage='migration_ready',
                    migration_run_id=EXCLUDED.migration_run_id,
                    empty_domain_probe_complete=true,
                    checkpoint_marker_probe_complete=true,
                    cutover_at=NULL,
                    updated_at=now()"""
            ),
            {"run_id": run_id},
        )
    return run_id


async def run_private_work_migration(
    database_url: str,
    *,
    owner_map: Mapping[str, uuid.UUID | str],
    repo_root: Path,
    data_root: Path,
    backup_dir: Path,
    execute: bool,
) -> PrivateWorkMigrationReport:
    del repo_root, backup_dir  # Reserved CLI contract; backup proof is a later gate.
    normalized_map = _normalize_owner_map(owner_map)
    engine = create_async_engine(database_url)
    try:
        if execute:
            async with engine.connect() as connection:
                if await _cutover_complete(connection):
                    return PrivateWorkMigrationReport(
                        mode="execute",
                        counts={
                            "threads": 0,
                            "runs": 0,
                            "run_events": 0,
                            "feedback": 0,
                            "checkpoints": 0,
                        },
                        source_key_hash=hashlib.sha256(b"cutover_complete").hexdigest()[:12],
                        cutover_complete=True,
                        noop=True,
                    )
        inventory = await _collect_inventory(engine, data_root)
        plan = build_migration_plan(inventory, normalized_map)
        counts = _report_counts(inventory)
        async with engine.connect() as connection:
            revision = await _current_revision(connection)
            already_complete = await _cutover_complete(connection)
            await _validate_owner_targets(connection, plan)
            await _validate_relations(connection)
            await _validate_unsupported_tables_empty(connection)
            if not execute:
                if revision not in {
                    "0007_project_shared_assets",
                    "0008_project_private_work_expand",
                    "0009_project_private_work_finalize",
                    "0010_private_file_source",
                    "0011_private_artifact_tombstone",
                }:
                    raise PrivateWorkMigrationError("unsupported database revision")
                return PrivateWorkMigrationReport(
                    mode="dry-run",
                    counts=counts,
                    source_key_hash=inventory.source_fingerprint[:12],
                    cutover_complete=already_complete,
                    empty_install=not any(counts.values()),
                    noop=already_complete,
                )
            if already_complete:
                return PrivateWorkMigrationReport(
                    mode="execute",
                    counts=counts,
                    source_key_hash=inventory.source_fingerprint[:12],
                    cutover_complete=True,
                    empty_install=not any(counts.values()),
                    noop=True,
                )
            if revision not in {
                "0007_project_shared_assets",
                "0008_project_private_work_expand",
            }:
                raise PrivateWorkMigrationError("unsupported database revision")

        if revision == "0007_project_shared_assets":
            await asyncio.to_thread(
                command.upgrade,
                _get_alembic_config(engine),
                "0008_project_private_work_expand",
            )
            await engine.dispose()
            engine = create_async_engine(database_url)
            await _assert_source_fingerprint(
                engine,
                data_root,
                inventory.source_fingerprint,
            )

        owner_map_digest = _digest_json({target.owner_user_id: str(target.project_id) for target in plan.owner_targets})
        run_id = await _execute_staging(
            engine,
            plan=plan,
            inventory=inventory,
            data_root=data_root,
            owner_map_digest=owner_map_digest,
        )
        await asyncio.to_thread(
            command.upgrade,
            _get_alembic_config(engine),
            "head",
        )
        await engine.dispose()
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            revision = await _current_revision(connection)
            if revision != "0011_private_artifact_tombstone":
                raise PrivateWorkMigrationError("private-work finalize revision is incomplete")
            await connection.execute(
                text(
                    """UPDATE private_work_cutover_state
                    SET stage='cutover_complete',
                        migration_run_id=:run_id,
                        cutover_at=now(),
                        updated_at=now()
                    WHERE id=1 AND stage='migration_ready'"""
                ),
                {"run_id": run_id},
            )
            if not await _cutover_complete(connection):
                raise PrivateWorkMigrationError("private-work cutover marker is incomplete")
        return PrivateWorkMigrationReport(
            mode="execute",
            counts=counts,
            source_key_hash=inventory.source_fingerprint[:12],
            cutover_complete=True,
            empty_install=not any(counts.values()),
        )
    except PrivateWorkMigrationError:
        raise
    except Exception:
        raise PrivateWorkMigrationError("private-work migration failed safely") from None
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise PrivateWorkMigrationError("DATABASE_URL is required")
        owner_map = load_owner_map(args.owner_map)
        data_root = args.data_root or args.repo_root / ".deer-flow"
        report = asyncio.run(
            run_private_work_migration(
                database_url,
                owner_map=owner_map,
                repo_root=args.repo_root,
                data_root=data_root,
                backup_dir=args.backup_dir,
                execute=args.execute,
            )
        )
        print(
            json.dumps(
                {
                    "backup_written": report.backup_written,
                    "counts": report.counts,
                    "cutover_complete": report.cutover_complete,
                    "empty_install": report.empty_install,
                    "mode": report.mode,
                    "noop": report.noop,
                    "source_key_hash": report.source_key_hash,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    except PrivateWorkMigrationError:
        print("private-work migration failed safely", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
