#!/usr/bin/env python3
"""Explicitly migrate legacy scheduled Automations into project scope."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from alembic import command
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from deerflow.persistence.automations.migration_digest import (
    AUTOMATION_EXPANDED_COLUMNS,
    AUTOMATION_FINALIZE_LOCK_SQL,
    AUTOMATION_LEGACY_COLUMNS,
    AUTOMATION_TARGET_COLUMNS,
    canonical_digest,
    canonical_value,
)
from deerflow.persistence.bootstrap import _get_alembic_config


class AutomationMigrationError(RuntimeError):
    """Stable migration error that is safe to show without private values."""


class FreshThreadAgentMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: uuid.UUID
    scope: Literal["project", "system"]


class AutomationOwnerMapItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    fresh_thread_agent: FreshThreadAgentMap


@dataclass(frozen=True, slots=True)
class AutomationOwnerTarget:
    owner_user_id: str = field(repr=False)
    project_id: uuid.UUID = field(repr=False)
    agent_asset_id: uuid.UUID = field(repr=False)
    agent_scope: str


@dataclass(frozen=True, slots=True)
class AutomationInventory:
    source_fingerprint: str
    task_rows: tuple[Mapping[str, object], ...] = field(repr=False)
    run_rows: tuple[Mapping[str, object], ...] = field(repr=False)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "scheduled_tasks": len(self.task_rows),
            "scheduled_task_runs": len(self.run_rows),
        }

    @property
    def status_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for row in self.task_rows:
            counts[f"tasks:{row.get('status')}"] += 1
        for row in self.run_rows:
            counts[f"runs:{row.get('status')}"] += 1
        return dict(sorted(counts.items()))

    @property
    def empty(self) -> bool:
        return not self.task_rows and not self.run_rows


@dataclass(frozen=True, slots=True)
class _PlannedRow:
    source_key: str = field(repr=False)
    values: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class AutomationMigrationPlan:
    source_fingerprint: str
    owner_targets: tuple[AutomationOwnerTarget, ...] = field(repr=False)
    tasks: tuple[_PlannedRow, ...] = field(repr=False)
    runs: tuple[_PlannedRow, ...] = field(repr=False)
    counts: dict[str, int]
    status_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class AutomationMigrationReport:
    mode: str
    counts: dict[str, int]
    status_counts: dict[str, int]
    source_key_hash: str
    cutover_complete: bool = False
    empty_install: bool = False
    noop: bool = False


_PRE_EXPAND_REVISION = "0011_private_artifact_tombstone"
_EXPAND_REVISION = "0012_project_automation_expand"
_FINAL_REVISION = "0013_project_automation_finalize"
_DRY_RUN_REVISIONS = frozenset({_PRE_EXPAND_REVISION, _EXPAND_REVISION, _FINAL_REVISION})
_EXECUTE_REVISIONS = frozenset({_PRE_EXPAND_REVISION, _EXPAND_REVISION})

_LEGACY_TASK_COLUMNS = AUTOMATION_LEGACY_COLUMNS["scheduled_tasks"]
_LEGACY_RUN_COLUMNS = AUTOMATION_LEGACY_COLUMNS["scheduled_task_runs"]
_TARGET_TASK_COLUMNS = AUTOMATION_TARGET_COLUMNS["scheduled_tasks"]
_TARGET_RUN_COLUMNS = AUTOMATION_TARGET_COLUMNS["scheduled_task_runs"]
_EXPANDED_TASK_COLUMNS = AUTOMATION_EXPANDED_COLUMNS["scheduled_tasks"]
_EXPANDED_RUN_COLUMNS = AUTOMATION_EXPANDED_COLUMNS["scheduled_task_runs"]
_TASK_STATUSES = frozenset({"enabled", "paused", "running", "completed", "failed", "cancelled"})
_RUN_STATUSES = frozenset({"queued", "running", "success", "failed", "skipped", "interrupted"})
_TERMINAL_RUN_STATUSES = frozenset({"success", "failed", "skipped", "interrupted", "cancelled", "rejected"})
_SCHEDULE_TYPES = frozenset({"once", "cron"})
_CONTEXT_MODES = frozenset({"fresh_thread_per_run", "reuse_thread"})
_TRIGGERS = frozenset({"scheduled", "manual"})


def owner_map_item_schema(value: object) -> AutomationOwnerMapItem:
    if not isinstance(value, Mapping) or "fresh_thread_agent" not in value:
        raise AutomationMigrationError("fresh thread agent mapping is required")
    try:
        return AutomationOwnerMapItem.model_validate(value)
    except ValidationError:
        raise AutomationMigrationError("owner map is invalid") from None


def normalize_owner_map(
    raw: Mapping[str, object],
) -> tuple[AutomationOwnerTarget, ...]:
    if not isinstance(raw, Mapping):
        raise AutomationMigrationError("owner map is invalid")
    targets: list[AutomationOwnerTarget] = []
    try:
        for owner, value in sorted(raw.items()):
            if not isinstance(owner, str):
                raise ValueError
            owner_id = str(uuid.UUID(owner))
            if owner_id != owner:
                raise ValueError
            item = owner_map_item_schema(value)
            targets.append(
                AutomationOwnerTarget(
                    owner_user_id=owner_id,
                    project_id=item.project_id,
                    agent_asset_id=item.fresh_thread_agent.asset_id,
                    agent_scope=item.fresh_thread_agent.scope,
                )
            )
    except AutomationMigrationError:
        raise
    except (TypeError, ValueError):
        raise AutomationMigrationError("owner map is invalid") from None
    return tuple(targets)


def load_owner_map(path: Path) -> tuple[AutomationOwnerTarget, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AutomationMigrationError("owner map is invalid") from None
    if not isinstance(raw, Mapping):
        raise AutomationMigrationError("owner map is invalid")
    return normalize_owner_map(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and inventory without database writes",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="run the reviewed staged migration",
    )
    parser.add_argument(
        "--owner-map",
        required=True,
        type=Path,
        help="reviewed owner/project/fresh-Agent JSON map",
    )
    parser.add_argument(
        "--backup-dir",
        required=True,
        type=Path,
        help="operator-managed external backup and restore proof directory",
    )
    return parser


def render_inventory(inventory: AutomationInventory) -> str:
    return json.dumps(
        {
            "counts": inventory.counts,
            "source_key_hash": inventory.source_fingerprint[:12],
            "status_counts": inventory.status_counts,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def render_report(
    value: AutomationMigrationReport | AutomationMigrationPlan,
) -> str:
    if isinstance(value, AutomationMigrationPlan):
        value = AutomationMigrationReport(
            mode="dry-run",
            counts=value.counts,
            status_counts=value.status_counts,
            source_key_hash=value.source_fingerprint[:12],
            empty_install=not any(value.counts.values()),
        )
    return json.dumps(
        {
            "counts": value.counts,
            "cutover_complete": value.cutover_complete,
            "empty_install": value.empty_install,
            "mode": value.mode,
            "noop": value.noop,
            "source_key_hash": value.source_key_hash,
            "status_counts": value.status_counts,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


async def _table_exists(connection: AsyncConnection, table: str) -> bool:
    return (
        await connection.scalar(
            text("SELECT to_regclass(:table) IS NOT NULL"),
            {"table": table},
        )
        is True
    )


async def _table_columns(
    connection: AsyncConnection,
    table: str,
) -> set[str]:
    rows = await connection.execute(
        text(
            """SELECT column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name=:table"""
        ),
        {"table": table},
    )
    return set(rows.scalars())


async def _current_revision(connection: AsyncConnection) -> str:
    if not await _table_exists(connection, "alembic_version"):
        raise AutomationMigrationError("versioned PostgreSQL database is required")
    revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    if not isinstance(revision, str):
        raise AutomationMigrationError("database revision is unavailable")
    return revision


async def _automation_cutover_state(
    connection: AsyncConnection,
) -> tuple[bool, bool]:
    if not await _table_exists(connection, "automation_cutover_state"):
        return False, False
    row = (
        await connection.execute(
            text(
                """SELECT stage,migration_run_id,empty_domain_probe_complete,
                final_schema_probe_complete,cutover_at
                FROM automation_cutover_state WHERE id=1"""
            )
        )
    ).one_or_none()
    if row is None:
        return False, False
    complete = row.stage == "cutover_complete" and row.final_schema_probe_complete is True and row.cutover_at is not None
    empty_install = complete and row.migration_run_id is None and row.empty_domain_probe_complete is True
    return complete, empty_install


async def _assert_m4_cutover_complete(connection: AsyncConnection) -> None:
    if not await _table_exists(connection, "private_work_cutover_state"):
        raise AutomationMigrationError("M4 private-work cutover is incomplete")
    ready = await connection.scalar(
        text(
            """SELECT stage='cutover_complete' AND cutover_at IS NOT NULL
            FROM private_work_cutover_state WHERE id=1"""
        )
    )
    if ready is not True:
        raise AutomationMigrationError("M4 private-work cutover is incomplete")


async def _collect_inventory_connection(
    connection: AsyncConnection,
) -> AutomationInventory:
    task_columns = await _table_columns(connection, "scheduled_tasks")
    run_columns = await _table_columns(connection, "scheduled_task_runs")
    schemas = (
        (_LEGACY_TASK_COLUMNS, _LEGACY_RUN_COLUMNS),
        (_EXPANDED_TASK_COLUMNS, _EXPANDED_RUN_COLUMNS),
        (_TARGET_TASK_COLUMNS, _TARGET_RUN_COLUMNS),
    )
    projections = next(
        ((task_schema, run_schema) for task_schema, run_schema in schemas if task_columns == set(task_schema) and run_columns == set(run_schema)),
        None,
    )
    if projections is None:
        raise AutomationMigrationError("legacy Automation schema is unsupported")
    task_projection = ",".join(f'"{column}"' for column in projections[0])
    run_projection = ",".join(f'"{column}"' for column in projections[1])
    task_rows = tuple(
        dict(row)
        for row in (
            (
                await connection.execute(
                    text(
                        f"SELECT {task_projection} FROM scheduled_tasks ORDER BY id"  # noqa: S608 - fixed projection
                    )
                )
            )
            .mappings()
            .all()
        )
    )
    run_rows = tuple(
        dict(row)
        for row in (
            (
                await connection.execute(
                    text(
                        f"SELECT {run_projection} FROM scheduled_task_runs ORDER BY id"  # noqa: S608 - fixed projection
                    )
                )
            )
            .mappings()
            .all()
        )
    )
    source_fingerprint = canonical_digest({"scheduled_tasks": task_rows, "scheduled_task_runs": run_rows})
    return AutomationInventory(
        source_fingerprint=source_fingerprint,
        task_rows=task_rows,
        run_rows=run_rows,
    )


async def _collect_inventory(engine: AsyncEngine) -> AutomationInventory:
    async with engine.connect() as connection:
        return await _collect_inventory_connection(connection)


def _targets_by_owner(
    targets: Sequence[AutomationOwnerTarget],
) -> dict[str, AutomationOwnerTarget]:
    return {target.owner_user_id: target for target in targets}


def _validated_owner(row: Mapping[str, object]) -> str:
    owner = row.get("user_id")
    if not isinstance(owner, str):
        raise AutomationMigrationError("legacy Automation owner is invalid")
    try:
        canonical = str(uuid.UUID(owner))
    except ValueError:
        raise AutomationMigrationError("legacy Automation owner is invalid") from None
    if canonical != owner:
        raise AutomationMigrationError("legacy Automation owner is invalid")
    return owner


def _validate_source_values(inventory: AutomationInventory) -> None:
    task_ids: set[str] = set()
    for row in inventory.task_rows:
        task_id = row.get("id")
        if not isinstance(task_id, str) or not task_id or len(task_id) > 64:
            raise AutomationMigrationError("legacy Automation key is invalid")
        if task_id in task_ids:
            raise AutomationMigrationError("legacy Automation key conflicts")
        task_ids.add(task_id)
        _validated_owner(row)
        if row.get("context_mode") not in _CONTEXT_MODES:
            raise AutomationMigrationError("unsupported legacy Automation context")
        if row.get("schedule_type") not in _SCHEDULE_TYPES:
            raise AutomationMigrationError("unsupported legacy Automation schedule")
        if row.get("status") not in _TASK_STATUSES:
            raise AutomationMigrationError("unsupported legacy Automation status")
        if row.get("overlap_policy") != "skip":
            raise AutomationMigrationError("unsupported legacy Automation overlap policy")
    for row in inventory.run_rows:
        if row.get("task_id") not in task_ids:
            raise AutomationMigrationError("orphan Automation occurrence")
        if row.get("trigger") not in _TRIGGERS:
            raise AutomationMigrationError("unsupported legacy Automation trigger")
        if row.get("status") not in _RUN_STATUSES:
            raise AutomationMigrationError("unsupported legacy Automation status")


async def _validate_owner_target(
    connection: AsyncConnection,
    target: AutomationOwnerTarget,
) -> None:
    membership = (
        await connection.execute(
            text(
                """SELECT membership.id,membership.version,membership.role
                FROM projects project
                JOIN project_memberships membership
                  ON membership.project_id=project.id
                WHERE project.id=:project
                  AND project.status='active'
                  AND project.is_suspended=false
                  AND project.deletion_requested_at IS NULL
                  AND membership.user_id=:owner
                  AND membership.status='active'
                  AND membership.role IN ('admin','editor','runner')"""
            ),
            {"project": target.project_id, "owner": target.owner_user_id},
        )
    ).one_or_none()
    if membership is None:
        raise AutomationMigrationError("mapped project membership is unavailable")


async def _validate_executable_agent(
    connection: AsyncConnection,
    *,
    project_id: uuid.UUID,
    agent_asset_id: uuid.UUID,
    agent_scope: str,
    error_message: str,
) -> None:
    if agent_scope == "project":
        valid = await connection.scalar(
            text(
                """SELECT EXISTS (
                    SELECT 1 FROM agents agent
                    JOIN agent_versions version
                      ON version.id=agent.current_published_version_id
                     AND version.agent_id=agent.id
                    WHERE agent.id=:agent
                      AND agent.scope='project'
                      AND agent.project_id=:project
                      AND agent.status='active'
                      AND version.workflow_status='published'
                )"""
            ),
            {"agent": agent_asset_id, "project": project_id},
        )
    elif agent_scope == "system":
        valid = await connection.scalar(
            text(
                """SELECT EXISTS (
                    SELECT 1 FROM project_system_agent_bindings binding
                    JOIN agents agent
                      ON agent.id=binding.system_agent_id
                     AND agent.scope='system'
                    JOIN agent_versions version
                      ON version.id=binding.agent_version_id
                     AND version.agent_id=agent.id
                    WHERE binding.project_id=:project
                      AND binding.system_agent_id=:agent
                      AND binding.enabled=true
                      AND agent.project_id IS NULL
                      AND agent.status='active'
                      AND version.workflow_status='published'
                )"""
            ),
            {"agent": agent_asset_id, "project": project_id},
        )
    else:
        valid = False
    if valid is not True:
        raise AutomationMigrationError(error_message)


async def _reuse_thread_agent(
    connection: AsyncConnection,
    *,
    owner_user_id: str,
    project_id: uuid.UUID,
    thread_id: object,
) -> tuple[uuid.UUID, str]:
    if not isinstance(thread_id, str) or not thread_id:
        raise AutomationMigrationError("reuse Thread mapping is required")
    row = (
        await connection.execute(
            text(
                """SELECT project_id,owner_user_id,agent_asset_id,agent_scope
                FROM threads_meta
                WHERE thread_id=:thread
                  AND deleted_at IS NULL
                  AND frozen_at IS NULL"""
            ),
            {"thread": thread_id},
        )
    ).one_or_none()
    if row is None:
        raise AutomationMigrationError("reuse Thread mapping is unavailable")
    if row.project_id != project_id or row.owner_user_id != owner_user_id:
        raise AutomationMigrationError("reuse Thread scope does not match owner map")
    agent_id = uuid.UUID(str(row.agent_asset_id))
    agent_scope = str(row.agent_scope)
    await _validate_executable_agent(
        connection,
        project_id=project_id,
        agent_asset_id=agent_id,
        agent_scope=agent_scope,
        error_message="reuse Thread Agent is not executable",
    )
    return agent_id, agent_scope


def _latest_outcome(
    task_id: str,
    run_rows: Sequence[Mapping[str, object]],
) -> str | None:
    candidates = [row for row in run_rows if row.get("task_id") == task_id]
    if not candidates:
        return None
    candidates.sort(
        key=lambda row: (
            str(row.get("scheduled_for") or ""),
            str(row.get("created_at") or ""),
            str(row.get("id") or ""),
        )
    )
    status = candidates[-1].get("status")
    return str(status) if status in _TERMINAL_RUN_STATUSES else None


def _task_target_values(
    row: Mapping[str, object],
    *,
    target: AutomationOwnerTarget,
    agent_asset_id: uuid.UUID,
    agent_scope: str,
    run_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    status = str(row["status"])
    final_status = "paused" if status == "running" else status
    next_run_at = None if status == "running" else row.get("next_run_at")
    last_error_code = "LEGACY_AUTOMATION_ERROR" if row.get("last_error") is not None else "LEGACY_AUTOMATION_PAUSED" if status == "running" else None
    context_mode = str(row["context_mode"])
    return {
        "id": row["id"],
        "project_id": target.project_id,
        "owner_user_id": target.owner_user_id,
        "thread_id": row.get("thread_id") if context_mode == "reuse_thread" else None,
        "context_mode": context_mode,
        "agent_asset_id": agent_asset_id,
        "agent_scope": agent_scope,
        "title": row["title"],
        "prompt": row["prompt"],
        "schedule_type": row["schedule_type"],
        "schedule_spec": row["schedule_spec"],
        "timezone": row["timezone"],
        "status": final_status,
        "overlap_policy": row["overlap_policy"],
        "next_run_at": next_run_at,
        "last_run_at": row.get("last_run_at"),
        "last_outcome": _latest_outcome(str(row["id"]), run_rows),
        "last_error_code": last_error_code,
        "run_count": row["run_count"],
        "version": 1,
        "frozen_at": None,
        "deleted_at": None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def _occurrence_scope(
    connection: AsyncConnection,
    row: Mapping[str, object],
    *,
    target: AutomationOwnerTarget,
) -> tuple[str | None, str | None]:
    thread_id = row.get("thread_id")
    run_id = row.get("run_id")
    thread_exists = False
    if isinstance(thread_id, str) and thread_id:
        thread_exists = (
            await connection.scalar(
                text(
                    """SELECT EXISTS (
                        SELECT 1 FROM threads_meta
                        WHERE project_id=:project
                          AND owner_user_id=:owner
                          AND thread_id=:thread
                    )"""
                ),
                {
                    "project": target.project_id,
                    "owner": target.owner_user_id,
                    "thread": thread_id,
                },
            )
            is True
        )
    if run_id is not None:
        if not isinstance(run_id, str) or not run_id or not thread_exists:
            raise AutomationMigrationError("orphan automation run")
        run_exists = await connection.scalar(
            text(
                """SELECT EXISTS (
                    SELECT 1 FROM runs
                    WHERE project_id=:project
                      AND owner_user_id=:owner
                      AND thread_id=:thread
                      AND run_id=:run
                )"""
            ),
            {
                "project": target.project_id,
                "owner": target.owner_user_id,
                "thread": thread_id,
                "run": run_id,
            },
        )
        if run_exists is not True:
            raise AutomationMigrationError("orphan automation run")
        return thread_id, run_id
    if thread_exists:
        return str(thread_id), None
    if row.get("status") == "skipped":
        return None, None
    raise AutomationMigrationError("orphan automation Thread")


async def _validate_legacy_task_history(
    connection: AsyncConnection,
    row: Mapping[str, object],
    *,
    target: AutomationOwnerTarget,
) -> None:
    thread_id = row.get("last_thread_id")
    run_id = row.get("last_run_id")
    if thread_id is None and run_id is None:
        return
    await _occurrence_scope(
        connection,
        {
            "thread_id": thread_id,
            "run_id": run_id,
            "status": "failed",
        },
        target=target,
    )


def _occurrence_key(row: Mapping[str, object]) -> str:
    return canonical_digest(
        {
            "legacy_id": row.get("id"),
            "task_id": row.get("task_id"),
            "scheduled_for": row.get("scheduled_for"),
            "trigger": row.get("trigger"),
        }
    )


def _stable_source_fingerprint(
    inventory: AutomationInventory,
    task_plans: Sequence[_PlannedRow],
    run_plans: Sequence[_PlannedRow],
) -> str:
    """Hash source semantics after deterministic migration normalizations.

    Revision 0012 adds nullable target columns and staging fills them. Overlay
    every deterministic target field while retaining every legacy-only source
    field, so 0011, pristine 0012, and a partially staged 0012 database produce
    one receipt without hiding changes to legacy source semantics.
    """

    task_targets = {row.source_key: row.values for row in task_plans}
    run_targets = {row.source_key: row.values for row in run_plans}
    tasks: list[dict[str, object]] = []
    for row in inventory.task_rows:
        normalized = dict(row)
        target = task_targets[str(row["id"])]
        for column in _TARGET_TASK_COLUMNS:
            normalized[column] = target[column]
        tasks.append(normalized)
    runs: list[dict[str, object]] = []
    for row in inventory.run_rows:
        normalized = dict(row)
        target = run_targets[str(row["id"])]
        for column in _TARGET_RUN_COLUMNS:
            normalized[column] = target[column]
        runs.append(normalized)
    return canonical_digest({"scheduled_tasks": tasks, "scheduled_task_runs": runs})


async def _build_validated_plan(
    connection: AsyncConnection,
    inventory: AutomationInventory,
    targets: Sequence[AutomationOwnerTarget],
) -> AutomationMigrationPlan:
    _validate_source_values(inventory)
    by_owner = _targets_by_owner(targets)
    inventory_owners = {_validated_owner(row) for row in inventory.task_rows}
    if inventory.run_rows and not inventory.task_rows:
        raise AutomationMigrationError("orphan Automation occurrence")
    if inventory_owners != set(by_owner):
        raise AutomationMigrationError("owner map does not exactly match legacy owners")
    for target in targets:
        await _validate_owner_target(connection, target)

    task_plans: list[_PlannedRow] = []
    targets_by_task: dict[str, AutomationOwnerTarget] = {}
    for row in inventory.task_rows:
        owner = _validated_owner(row)
        target = by_owner[owner]
        targets_by_task[str(row["id"])] = target
        if row["context_mode"] == "fresh_thread_per_run":
            agent_id = target.agent_asset_id
            agent_scope = target.agent_scope
            await _validate_executable_agent(
                connection,
                project_id=target.project_id,
                agent_asset_id=agent_id,
                agent_scope=agent_scope,
                error_message="fresh Agent is not executable",
            )
        else:
            agent_id, agent_scope = await _reuse_thread_agent(
                connection,
                owner_user_id=owner,
                project_id=target.project_id,
                thread_id=row.get("thread_id"),
            )
        await _validate_legacy_task_history(
            connection,
            row,
            target=target,
        )
        task_plans.append(
            _PlannedRow(
                source_key=str(row["id"]),
                values=_task_target_values(
                    row,
                    target=target,
                    agent_asset_id=agent_id,
                    agent_scope=agent_scope,
                    run_rows=inventory.run_rows,
                ),
            )
        )

    run_plans: list[_PlannedRow] = []
    for row in inventory.run_rows:
        task_id = str(row["task_id"])
        target = targets_by_task[task_id]
        thread_id, run_id = await _occurrence_scope(
            connection,
            row,
            target=target,
        )
        launch_attempt_count = 1 if run_id is not None or row.get("started_at") is not None and row.get("status") != "skipped" else 0
        run_plans.append(
            _PlannedRow(
                source_key=str(row["id"]),
                values={
                    "id": row["id"],
                    "project_id": target.project_id,
                    "owner_user_id": target.owner_user_id,
                    "task_id": task_id,
                    "task_version": 1,
                    "occurrence_key": _occurrence_key(row),
                    "manual_idempotency_hash": None,
                    "scheduled_for": row["scheduled_for"],
                    "trigger": row["trigger"],
                    "status": row["status"],
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "resolved_membership_id": None,
                    "resolved_membership_version": None,
                    "launch_attempt_count": launch_attempt_count,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "next_attempt_at": None,
                    "error_code": "LEGACY_AUTOMATION_ERROR" if row.get("error") is not None else None,
                    "error_message": None,
                    "started_at": row.get("started_at"),
                    "finished_at": row.get("finished_at"),
                    "created_at": row["created_at"],
                    "updated_at": row.get("finished_at") or row.get("started_at") or row["created_at"],
                },
            )
        )
    task_rows = tuple(task_plans)
    run_rows = tuple(run_plans)
    return AutomationMigrationPlan(
        source_fingerprint=_stable_source_fingerprint(
            inventory,
            task_rows,
            run_rows,
        ),
        owner_targets=tuple(targets),
        tasks=task_rows,
        runs=run_rows,
        counts=inventory.counts,
        status_counts=inventory.status_counts,
    )


def build_migration_plan(
    inventory: AutomationInventory,
    owner_map: Mapping[str, object] | Sequence[AutomationOwnerTarget],
    *,
    reuse_threads: Mapping[tuple[str, str], tuple[uuid.UUID, str]] | None = None,
) -> AutomationMigrationPlan:
    """Build a pure plan when callers already validated reuse Thread relations.

    The executable path uses ``_build_validated_plan`` so database authority is
    rechecked in the same preflight. This helper exists for deterministic
    inventory/report tests and offline review tooling.
    """

    targets = tuple(owner_map) if not isinstance(owner_map, Mapping) else normalize_owner_map(owner_map)
    _validate_source_values(inventory)
    by_owner = _targets_by_owner(targets)
    owners = {_validated_owner(row) for row in inventory.task_rows}
    if owners != set(by_owner):
        raise AutomationMigrationError("owner map does not exactly match legacy owners")
    reuse_threads = reuse_threads or {}
    tasks: list[_PlannedRow] = []
    for row in inventory.task_rows:
        owner = _validated_owner(row)
        target = by_owner[owner]
        if row["context_mode"] == "fresh_thread_per_run":
            agent_id, agent_scope = target.agent_asset_id, target.agent_scope
        else:
            key = (owner, str(row.get("thread_id") or ""))
            relation = reuse_threads.get(key)
            if relation is None:
                raise AutomationMigrationError("reuse Thread mapping is required")
            agent_id, agent_scope = relation
        tasks.append(
            _PlannedRow(
                source_key=str(row["id"]),
                values=_task_target_values(
                    row,
                    target=target,
                    agent_asset_id=agent_id,
                    agent_scope=agent_scope,
                    run_rows=inventory.run_rows,
                ),
            )
        )
    if inventory.run_rows:
        raise AutomationMigrationError("database relation validation is required")
    task_rows = tuple(tasks)
    return AutomationMigrationPlan(
        source_fingerprint=_stable_source_fingerprint(
            inventory,
            task_rows,
            (),
        ),
        owner_targets=targets,
        tasks=task_rows,
        runs=(),
        counts=inventory.counts,
        status_counts=inventory.status_counts,
    )


def _owner_map_digest(targets: Sequence[AutomationOwnerTarget]) -> str:
    return canonical_digest(
        [
            {
                "owner_user_id": target.owner_user_id,
                "project_id": target.project_id,
                "fresh_thread_agent": {
                    "asset_id": target.agent_asset_id,
                    "scope": target.agent_scope,
                },
            }
            for target in targets
        ]
    )


async def _validate_existing_marker(
    connection: AsyncConnection,
    *,
    source_fingerprint: str,
    owner_map_digest: str,
) -> None:
    if not await _table_exists(connection, "automation_cutover_state"):
        return
    existing_runs = (
        await connection.execute(
            text(
                """SELECT DISTINCT source_fingerprint,owner_map_digest
                FROM automation_migration_runs"""
            )
        )
    ).all()
    if any(row.source_fingerprint != source_fingerprint for row in existing_runs):
        raise AutomationMigrationError("legacy source fingerprint changed")
    if any(row.owner_map_digest != owner_map_digest for row in existing_runs):
        raise AutomationMigrationError("owner map digest conflicts")
    marker = (
        await connection.execute(
            text(
                """SELECT state.stage,run.source_fingerprint,run.owner_map_digest
                FROM automation_cutover_state state
                LEFT JOIN automation_migration_runs run
                  ON run.id=state.migration_run_id
                WHERE state.id=1"""
            )
        )
    ).one_or_none()
    if marker is None or marker.stage == "empty_install":
        return
    if marker.source_fingerprint != source_fingerprint:
        raise AutomationMigrationError("legacy source fingerprint changed")
    if marker.owner_map_digest != owner_map_digest:
        raise AutomationMigrationError("owner map digest conflicts")


async def _assert_existing_targets_compatible(
    connection: AsyncConnection,
    plan: AutomationMigrationPlan,
) -> None:
    if not await _table_exists(connection, "automation_migration_ledger"):
        return
    ledger_domains = set(
        (
            await connection.execute(
                text(
                    """SELECT DISTINCT domain FROM automation_migration_ledger
                    WHERE status='complete'"""
                )
            )
        ).scalars()
    )
    if not ledger_domains:
        return
    for table, rows, columns in (
        ("scheduled_tasks", plan.tasks, _TARGET_TASK_COLUMNS),
        ("scheduled_task_runs", plan.runs, _TARGET_RUN_COLUMNS),
    ):
        if table not in ledger_domains:
            continue
        projection = ",".join(f'"{column}"' for column in columns)
        actual_rows = tuple(
            dict(row)
            for row in (
                (
                    await connection.execute(
                        text(
                            f'SELECT {projection} FROM "{table}" ORDER BY id'  # noqa: S608 - fixed domain
                        )
                    )
                )
                .mappings()
                .all()
            )
        )
        expected_rows = tuple(dict(row.values) for row in rows)
        if len(actual_rows) != len(expected_rows):
            raise AutomationMigrationError("migration ledger conflicts")
        for actual, expected in zip(actual_rows, expected_rows, strict=True):
            for column in columns:
                if actual[column] is not None and canonical_value(actual[column]) != canonical_value(expected[column]):
                    raise AutomationMigrationError("migration ledger conflicts")


async def _preflight(
    connection: AsyncConnection,
    inventory: AutomationInventory,
    targets: Sequence[AutomationOwnerTarget],
) -> AutomationMigrationPlan:
    await _assert_m4_cutover_complete(connection)
    plan = await _build_validated_plan(connection, inventory, targets)
    map_digest = _owner_map_digest(targets)
    await _validate_existing_marker(
        connection,
        source_fingerprint=plan.source_fingerprint,
        owner_map_digest=map_digest,
    )
    await _assert_existing_targets_compatible(connection, plan)
    return plan


def _has_operator_backup_proof(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_dir():
            return False
        for item in path.iterdir():
            if item.is_symlink():
                continue
            if item.is_file() and item.stat().st_size > 0:
                return True
        return False
    except OSError:
        return False


async def _assert_source_fingerprint(
    engine: AsyncEngine,
    expected: str,
) -> AutomationInventory:
    inventory = await _collect_inventory(engine)
    if inventory.source_fingerprint != expected:
        raise AutomationMigrationError("legacy source fingerprint changed")
    return inventory


async def _lock_automation_sources(connection: AsyncConnection) -> None:
    """Block legacy Automation writers for the protected migration snapshot."""

    await connection.execute(text("LOCK TABLE scheduled_tasks, scheduled_task_runs IN SHARE ROW EXCLUSIVE MODE"))


async def _lock_automation_sources_for_finalize(
    connection: AsyncConnection,
) -> None:
    """Serialize final receipt verification with all Automation writers."""

    await connection.execute(text(AUTOMATION_FINALIZE_LOCK_SQL))


async def _migration_run_id(
    connection: AsyncConnection,
    *,
    plan: AutomationMigrationPlan,
    owner_map_digest: str,
) -> uuid.UUID:
    existing = (
        await connection.execute(
            text(
                """SELECT id FROM automation_migration_runs
                WHERE mode='execute'
                  AND source_fingerprint=:source
                  AND owner_map_digest=:owner_map
                  AND status IN ('running','completed')
                ORDER BY started_at DESC,id DESC LIMIT 1"""
            ),
            {
                "source": plan.source_fingerprint,
                "owner_map": owner_map_digest,
            },
        )
    ).scalar_one_or_none()
    if existing is not None:
        return uuid.UUID(str(existing))
    run_id = uuid.uuid4()
    await connection.execute(
        text(
            """INSERT INTO automation_migration_runs
            (id,mode,status,source_fingerprint,owner_map_digest,
             source_task_count,source_run_count,source_probe_complete,
             scope_relation_probe_complete,started_at)
            VALUES (:id,'execute','running',:source,:owner_map,:tasks,:runs,
                    false,false,now())"""
        ),
        {
            "id": run_id,
            "source": plan.source_fingerprint,
            "owner_map": owner_map_digest,
            "tasks": plan.counts["scheduled_tasks"],
            "runs": plan.counts["scheduled_task_runs"],
        },
    )
    return run_id


def _domain_plan(
    plan: AutomationMigrationPlan,
    domain: str,
) -> tuple[tuple[_PlannedRow, ...], tuple[str, ...]]:
    if domain == "scheduled_tasks":
        return plan.tasks, _TARGET_TASK_COLUMNS
    if domain == "scheduled_task_runs":
        return plan.runs, _TARGET_RUN_COLUMNS
    raise AutomationMigrationError("migration domain is invalid")


def _domain_target_digest(plan: AutomationMigrationPlan, domain: str) -> str:
    rows, _columns = _domain_plan(plan, domain)
    return canonical_digest([dict(row.values) for row in rows])


async def _actual_domain_digest(
    connection: AsyncConnection,
    domain: str,
    columns: Sequence[str],
) -> tuple[int, str]:
    projection = ",".join(f'"{column}"' for column in columns)
    rows = tuple(
        dict(row)
        for row in (
            (
                await connection.execute(
                    text(
                        f'SELECT {projection} FROM "{domain}" ORDER BY id'  # noqa: S608 - fixed domain
                    )
                )
            )
            .mappings()
            .all()
        )
    )
    return len(rows), canonical_digest(rows)


async def _write_task_target(
    connection: AsyncConnection,
    row: _PlannedRow,
) -> None:
    values = dict(row.values)
    values["schedule_spec"] = json.dumps(
        canonical_value(values["schedule_spec"]),
        separators=(",", ":"),
        sort_keys=True,
    )
    result = await connection.execute(
        text(
            """UPDATE scheduled_tasks SET
            project_id=:project_id,owner_user_id=:owner_user_id,
            thread_id=:thread_id,context_mode=:context_mode,
            agent_asset_id=:agent_asset_id,agent_scope=:agent_scope,
            title=:title,prompt=:prompt,schedule_type=:schedule_type,
            schedule_spec=CAST(:schedule_spec AS JSONB),timezone=:timezone,
            status=:status,overlap_policy=:overlap_policy,
            next_run_at=:next_run_at,last_run_at=:last_run_at,
            last_outcome=:last_outcome,last_error_code=:last_error_code,
            run_count=:run_count,version=:version,frozen_at=:frozen_at,
            deleted_at=:deleted_at,created_at=:created_at,updated_at=:updated_at
            WHERE id=:id AND user_id=:owner_user_id"""
        ),
        values,
    )
    if result.rowcount != 1:
        raise AutomationMigrationError("migration row count conflicts")


async def _write_run_target(
    connection: AsyncConnection,
    row: _PlannedRow,
) -> None:
    result = await connection.execute(
        text(
            """UPDATE scheduled_task_runs SET
            project_id=:project_id,owner_user_id=:owner_user_id,
            task_version=:task_version,occurrence_key=:occurrence_key,
            manual_idempotency_hash=:manual_idempotency_hash,
            scheduled_for=:scheduled_for,trigger=:trigger,status=:status,
            thread_id=:thread_id,run_id=:run_id,
            resolved_membership_id=:resolved_membership_id,
            resolved_membership_version=:resolved_membership_version,
            launch_attempt_count=:launch_attempt_count,lease_owner=:lease_owner,
            lease_expires_at=:lease_expires_at,next_attempt_at=:next_attempt_at,
            error_code=:error_code,error_message=:error_message,
            started_at=:started_at,finished_at=:finished_at,
            created_at=:created_at,updated_at=:updated_at
            WHERE id=:id AND task_id=:task_id"""
        ),
        dict(row.values),
    )
    if result.rowcount != 1:
        raise AutomationMigrationError("migration row count conflicts")


async def _write_domain_ledger(
    connection: AsyncConnection,
    *,
    migration_run_id: uuid.UUID,
    plan: AutomationMigrationPlan,
    domain: str,
) -> None:
    rows, columns = _domain_plan(plan, domain)
    expected_count = len(rows)
    expected_digest = _domain_target_digest(plan, domain)
    existing = (
        await connection.execute(
            text(
                """SELECT source_fingerprint,target_digest,status,
                source_row_count,target_row_count
                FROM automation_migration_ledger
                WHERE migration_run_id=:run_id AND domain=:domain"""
            ),
            {"run_id": migration_run_id, "domain": domain},
        )
    ).one_or_none()
    if existing is not None:
        actual_count, actual_digest = await _actual_domain_digest(connection, domain, columns)
        if (
            existing.source_fingerprint != plan.source_fingerprint
            or existing.target_digest != expected_digest
            or existing.status != "complete"
            or int(existing.source_row_count) != expected_count
            or int(existing.target_row_count) != expected_count
            or actual_count != expected_count
            or actual_digest != expected_digest
        ):
            raise AutomationMigrationError("migration ledger conflicts")
        return

    writer = _write_task_target if domain == "scheduled_tasks" else _write_run_target
    for row in rows:
        await writer(connection, row)
    actual_count, actual_digest = await _actual_domain_digest(connection, domain, columns)
    if actual_count != expected_count or actual_digest != expected_digest:
        raise AutomationMigrationError("migration target digest conflicts")
    await connection.execute(
        text(
            """INSERT INTO automation_migration_ledger
            (migration_run_id,domain,source_fingerprint,target_digest,status,
             source_row_count,target_row_count,completed_at)
            VALUES (:run_id,:domain,:source,:target,'complete',:rows,:rows,now())"""
        ),
        {
            "run_id": migration_run_id,
            "domain": domain,
            "source": plan.source_fingerprint,
            "target": expected_digest,
            "rows": expected_count,
        },
    )


async def _scope_relation_probe(connection: AsyncConnection) -> None:
    invalid_task = await connection.scalar(
        text(
            """SELECT EXISTS (
                SELECT 1 FROM scheduled_tasks task
                LEFT JOIN projects project ON project.id=task.project_id
                LEFT JOIN project_memberships membership
                  ON membership.project_id=task.project_id
                 AND membership.user_id=task.owner_user_id
                LEFT JOIN agents agent
                  ON agent.id=task.agent_asset_id
                 AND agent.scope=task.agent_scope
                LEFT JOIN threads_meta thread
                  ON thread.project_id=task.project_id
                 AND thread.owner_user_id=task.owner_user_id
                 AND thread.thread_id=task.thread_id
                WHERE task.project_id IS NULL OR task.owner_user_id IS NULL
                   OR task.agent_asset_id IS NULL OR task.agent_scope IS NULL
                   OR task.version IS NULL OR project.id IS NULL
                   OR membership.id IS NULL OR agent.id IS NULL
                   OR (agent.scope='project'
                       AND agent.project_id IS DISTINCT FROM task.project_id)
                   OR (task.thread_id IS NOT NULL AND thread.thread_id IS NULL)
                   OR (task.context_mode='reuse_thread' AND task.thread_id IS NULL)
                   OR (task.context_mode='fresh_thread_per_run'
                       AND task.thread_id IS NOT NULL)
            )"""
        )
    )
    if invalid_task:
        raise AutomationMigrationError("target task scope relation probe failed")
    invalid_run = await connection.scalar(
        text(
            """SELECT EXISTS (
                SELECT 1 FROM scheduled_task_runs occurrence
                LEFT JOIN scheduled_tasks task
                  ON task.project_id=occurrence.project_id
                 AND task.owner_user_id=occurrence.owner_user_id
                 AND task.id=occurrence.task_id
                LEFT JOIN threads_meta thread
                  ON thread.project_id=occurrence.project_id
                 AND thread.owner_user_id=occurrence.owner_user_id
                 AND thread.thread_id=occurrence.thread_id
                LEFT JOIN runs run
                  ON run.project_id=occurrence.project_id
                 AND run.owner_user_id=occurrence.owner_user_id
                 AND run.thread_id=occurrence.thread_id
                 AND run.run_id=occurrence.run_id
                WHERE occurrence.project_id IS NULL
                   OR occurrence.owner_user_id IS NULL
                   OR occurrence.task_version IS NULL
                   OR occurrence.occurrence_key IS NULL
                   OR occurrence.launch_attempt_count IS NULL
                   OR occurrence.updated_at IS NULL OR task.id IS NULL
                   OR (occurrence.thread_id IS NOT NULL AND thread.thread_id IS NULL)
                   OR (occurrence.run_id IS NOT NULL AND run.run_id IS NULL)
            )"""
        )
    )
    if invalid_run:
        raise AutomationMigrationError("target occurrence scope relation probe failed")


async def _execute_staging(
    engine: AsyncEngine,
    *,
    plan: AutomationMigrationPlan,
) -> uuid.UUID:
    owner_map_digest = _owner_map_digest(plan.owner_targets)
    async with engine.begin() as connection:
        await _lock_automation_sources(connection)
        locked_inventory = await _collect_inventory_connection(connection)
        locked_plan = await _preflight(
            connection,
            locked_inventory,
            plan.owner_targets,
        )
        if locked_plan.source_fingerprint != plan.source_fingerprint:
            raise AutomationMigrationError("legacy source fingerprint changed")
        run_id = await _migration_run_id(
            connection,
            plan=locked_plan,
            owner_map_digest=owner_map_digest,
        )
        for domain in ("scheduled_tasks", "scheduled_task_runs"):
            await _write_domain_ledger(
                connection,
                migration_run_id=run_id,
                plan=locked_plan,
                domain=domain,
            )
        await _scope_relation_probe(connection)
        domains = set(
            (
                await connection.execute(
                    text(
                        """SELECT domain FROM automation_migration_ledger
                        WHERE migration_run_id=:run_id AND status='complete'"""
                    ),
                    {"run_id": run_id},
                )
            ).scalars()
        )
        if domains != {"scheduled_tasks", "scheduled_task_runs"}:
            raise AutomationMigrationError("migration ledger is incomplete")
        await connection.execute(
            text(
                """UPDATE automation_migration_runs
                SET status='completed',source_probe_complete=true,
                    scope_relation_probe_complete=true,completed_at=now()
                WHERE id=:run_id"""
            ),
            {"run_id": run_id},
        )
        marker = (
            await connection.execute(
                text(
                    """SELECT stage,migration_run_id
                    FROM automation_cutover_state WHERE id=1 FOR UPDATE"""
                )
            )
        ).one_or_none()
        if marker is not None and (marker.stage != "migration_ready" or marker.migration_run_id != run_id):
            raise AutomationMigrationError("automation cutover marker conflicts")
        if marker is None:
            await connection.execute(
                text(
                    """INSERT INTO automation_cutover_state
                    (id,stage,migration_run_id,empty_domain_probe_complete,
                     final_schema_probe_complete,cutover_at,updated_at)
                    VALUES (1,'migration_ready',:run_id,false,false,NULL,now())"""
                ),
                {"run_id": run_id},
            )
    return run_id


async def _upgrade_database(engine: AsyncEngine, revision: str) -> None:
    await asyncio.to_thread(
        command.upgrade,
        _get_alembic_config(engine),
        revision,
    )


async def _mark_cutover_complete(
    connection: AsyncConnection,
    migration_run_id: uuid.UUID,
) -> None:
    result = await connection.execute(
        text(
            """UPDATE automation_cutover_state
            SET stage='cutover_complete',final_schema_probe_complete=true,
                cutover_at=now(),updated_at=now()
            WHERE id=1 AND stage='migration_ready'
              AND migration_run_id=:run_id
              AND final_schema_probe_complete=true
              AND cutover_at IS NULL"""
        ),
        {"run_id": migration_run_id},
    )
    if result.rowcount != 1:
        raise AutomationMigrationError("automation cutover marker is incomplete")


_FINAL_TASK_CONSTRAINTS = frozenset(
    {
        "uq_scheduled_tasks_private_scope",
        "fk_scheduled_tasks_project",
        "fk_scheduled_tasks_owner",
        "fk_scheduled_tasks_project_membership",
        "fk_scheduled_tasks_private_thread",
        "fk_scheduled_tasks_agent_asset",
        "ck_scheduled_tasks_context_mode",
        "ck_scheduled_tasks_schedule_type",
        "ck_scheduled_tasks_status",
        "ck_scheduled_tasks_overlap_policy",
        "ck_scheduled_tasks_thread_mode",
        "ck_scheduled_tasks_agent_scope",
        "ck_scheduled_tasks_version",
        "ck_scheduled_tasks_run_count",
        "ck_scheduled_tasks_last_outcome",
    }
)
_FINAL_RUN_CONSTRAINTS = frozenset(
    {
        "uq_scheduled_task_runs_occurrence",
        "fk_scheduled_task_runs_project",
        "fk_scheduled_task_runs_owner",
        "fk_scheduled_task_runs_task",
        "fk_scheduled_task_runs_private_thread",
        "fk_scheduled_task_runs_private_run",
        "ck_scheduled_task_runs_trigger",
        "ck_scheduled_task_runs_status",
        "ck_scheduled_task_runs_run_requires_thread",
        "ck_scheduled_task_runs_attempt_count",
        "ck_scheduled_task_runs_task_version",
    }
)
_FINAL_RUN_INDEXES = frozenset(
    {
        "uq_scheduled_task_runs_manual_idempotency",
        "ix_scheduled_task_runs_active_occurrence",
        "ix_scheduled_task_runs_history",
    }
)
_FINAL_TRIGGERS = frozenset(
    {
        "trg_scheduled_tasks_agent_project",
        "trg_agents_scheduled_task_project",
    }
)


async def _assert_final_schema(connection: AsyncConnection) -> None:
    task_columns = await _table_columns(connection, "scheduled_tasks")
    run_columns = await _table_columns(connection, "scheduled_task_runs")
    if task_columns != set(_TARGET_TASK_COLUMNS) or run_columns != set(_TARGET_RUN_COLUMNS):
        raise AutomationMigrationError("automation final schema probe failed")
    nullable_rows = (
        await connection.execute(
            text(
                """SELECT table_name,column_name,is_nullable
                FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name IN ('scheduled_tasks','scheduled_task_runs')"""
            )
        )
    ).all()
    nullable = {(row.table_name, row.column_name): row.is_nullable == "YES" for row in nullable_rows}
    required = {
        "scheduled_tasks": {
            "project_id",
            "owner_user_id",
            "agent_asset_id",
            "agent_scope",
            "version",
        },
        "scheduled_task_runs": {
            "project_id",
            "owner_user_id",
            "task_version",
            "occurrence_key",
            "launch_attempt_count",
            "updated_at",
        },
    }
    if any(nullable.get((table, column), True) for table, columns in required.items() for column in columns):
        raise AutomationMigrationError("automation final schema probe failed")
    constraints = {
        table: set(
            (
                await connection.execute(
                    text(
                        """SELECT conname FROM pg_constraint
                        WHERE conrelid=CAST(:table AS regclass)"""
                    ),
                    {"table": table},
                )
            ).scalars()
        )
        for table in ("scheduled_tasks", "scheduled_task_runs")
    }
    if not _FINAL_TASK_CONSTRAINTS <= constraints["scheduled_tasks"] or not (_FINAL_RUN_CONSTRAINTS <= constraints["scheduled_task_runs"]):
        raise AutomationMigrationError("automation final schema probe failed")
    indexes = set(
        (
            await connection.execute(
                text(
                    """SELECT indexname FROM pg_indexes
                    WHERE schemaname=current_schema()
                      AND tablename='scheduled_task_runs'"""
                )
            )
        ).scalars()
    )
    triggers = set(
        (
            await connection.execute(
                text(
                    """SELECT trigger_name FROM information_schema.triggers
                    WHERE trigger_schema=current_schema()
                      AND trigger_name IN
                        ('trg_scheduled_tasks_agent_project',
                         'trg_agents_scheduled_task_project')"""
                )
            )
        ).scalars()
    )
    if not _FINAL_RUN_INDEXES <= indexes or triggers != _FINAL_TRIGGERS:
        raise AutomationMigrationError("automation final schema probe failed")


async def _resume_final_cutover(
    engine: AsyncEngine,
    *,
    targets: Sequence[AutomationOwnerTarget],
    complete_marker: bool,
) -> AutomationInventory:
    """Validate immutable 0013 receipts and optionally finish only the marker."""

    async with engine.begin() as connection:
        await _lock_automation_sources_for_finalize(connection)
        if await _current_revision(connection) != _FINAL_REVISION:
            raise AutomationMigrationError("automation finalize revision is incomplete")
        await _assert_m4_cutover_complete(connection)
        marker = (
            await connection.execute(
                text(
                    """SELECT stage,migration_run_id,empty_domain_probe_complete,
                              final_schema_probe_complete,cutover_at
                    FROM automation_cutover_state WHERE id=1 FOR UPDATE"""
                )
            )
        ).one_or_none()
        if marker is None or marker.stage != "migration_ready" or marker.migration_run_id is None or marker.empty_domain_probe_complete is not False or marker.final_schema_probe_complete is not True or marker.cutover_at is not None:
            raise AutomationMigrationError("automation cutover marker is incomplete")
        run = (
            await connection.execute(
                text(
                    """SELECT mode,status,source_fingerprint,owner_map_digest,
                              source_task_count,source_run_count,
                              source_probe_complete,scope_relation_probe_complete,
                              completed_at
                    FROM automation_migration_runs WHERE id=:run_id"""
                ),
                {"run_id": marker.migration_run_id},
            )
        ).one_or_none()
        if (
            run is None
            or run.mode != "execute"
            or run.status != "completed"
            or run.completed_at is None
            or run.source_probe_complete is not True
            or run.scope_relation_probe_complete is not True
            or run.owner_map_digest != _owner_map_digest(targets)
        ):
            raise AutomationMigrationError("automation migration receipt conflicts")
        ledgers = {
            row.domain: row
            for row in (
                await connection.execute(
                    text(
                        """SELECT domain,source_fingerprint,target_digest,status,
                                  source_row_count,target_row_count
                        FROM automation_migration_ledger
                        WHERE migration_run_id=:run_id"""
                    ),
                    {"run_id": marker.migration_run_id},
                )
            )
        }
        if set(ledgers) != {"scheduled_tasks", "scheduled_task_runs"}:
            raise AutomationMigrationError("migration ledger is incomplete")
        inventory = await _collect_inventory_connection(connection)
        expected_counts = {
            "scheduled_tasks": int(run.source_task_count),
            "scheduled_task_runs": int(run.source_run_count),
        }
        rows_by_domain = {
            "scheduled_tasks": inventory.task_rows,
            "scheduled_task_runs": inventory.run_rows,
        }
        for domain, rows in rows_by_domain.items():
            ledger = ledgers[domain]
            expected_count = expected_counts[domain]
            if (
                ledger.status != "complete"
                or ledger.source_fingerprint != run.source_fingerprint
                or int(ledger.source_row_count) != expected_count
                or int(ledger.target_row_count) != expected_count
                or len(rows) != expected_count
                or canonical_digest(rows) != ledger.target_digest
            ):
                raise AutomationMigrationError("migration target digest conflicts")
        await _scope_relation_probe(connection)
        await _assert_final_schema(connection)
        if complete_marker:
            await _mark_cutover_complete(
                connection,
                uuid.UUID(str(marker.migration_run_id)),
            )
        return AutomationInventory(
            source_fingerprint=run.source_fingerprint,
            task_rows=inventory.task_rows,
            run_rows=inventory.run_rows,
        )


def _normalize_targets(
    owner_map: Mapping[str, object] | Sequence[AutomationOwnerTarget],
) -> tuple[AutomationOwnerTarget, ...]:
    if isinstance(owner_map, Mapping):
        return normalize_owner_map(owner_map)
    targets = tuple(owner_map)
    if any(type(target) is not AutomationOwnerTarget for target in targets):
        raise AutomationMigrationError("owner map is invalid")
    return targets


def _completed_report(
    *,
    mode: str,
    inventory: AutomationInventory | None,
    empty_install: bool,
    noop: bool,
) -> AutomationMigrationReport:
    counts = inventory.counts if inventory is not None else {"scheduled_tasks": 0, "scheduled_task_runs": 0}
    status_counts = inventory.status_counts if inventory is not None else {}
    source_hash = inventory.source_fingerprint[:12] if inventory is not None else hashlib.sha256(b"cutover_complete").hexdigest()[:12]
    return AutomationMigrationReport(
        mode=mode,
        counts=counts,
        status_counts=status_counts,
        source_key_hash=source_hash,
        cutover_complete=True,
        empty_install=empty_install,
        noop=noop,
    )


async def run_automation_migration(
    database_url: str,
    *,
    owner_map: Mapping[str, object] | Sequence[AutomationOwnerTarget],
    backup_dir: Path,
    execute: bool,
) -> AutomationMigrationReport:
    targets = _normalize_targets(owner_map)
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            revision = await _current_revision(connection)
            cutover_complete, existing_empty_install = await _automation_cutover_state(connection)
        if cutover_complete:
            inventory = await _collect_inventory(engine)
            return _completed_report(
                mode="execute" if execute else "dry-run",
                inventory=inventory,
                empty_install=existing_empty_install,
                noop=True,
            )
        if revision not in _DRY_RUN_REVISIONS:
            raise AutomationMigrationError("unsupported database revision")
        if revision == _FINAL_REVISION:
            if execute and not await asyncio.to_thread(
                _has_operator_backup_proof,
                backup_dir,
            ):
                raise AutomationMigrationError("operator backup proof is required")
            inventory = await _resume_final_cutover(
                engine,
                targets=targets,
                complete_marker=execute,
            )
            if not execute:
                return AutomationMigrationReport(
                    mode="dry-run",
                    counts=inventory.counts,
                    status_counts=inventory.status_counts,
                    source_key_hash=inventory.source_fingerprint[:12],
                    cutover_complete=False,
                    empty_install=False,
                )
            async with engine.connect() as connection:
                complete, empty_install = await _automation_cutover_state(connection)
            if not complete or empty_install:
                raise AutomationMigrationError("automation cutover marker is incomplete")
            return _completed_report(
                mode="execute",
                inventory=inventory,
                empty_install=False,
                noop=False,
            )

        inventory = await _collect_inventory(engine)
        async with engine.connect() as connection:
            plan = await _preflight(connection, inventory, targets)
        if not execute:
            return AutomationMigrationReport(
                mode="dry-run",
                counts=inventory.counts,
                status_counts=inventory.status_counts,
                source_key_hash=inventory.source_fingerprint[:12],
                cutover_complete=False,
                empty_install=inventory.empty,
            )
        if revision not in _EXECUTE_REVISIONS:
            raise AutomationMigrationError("unsupported database revision")
        if not await asyncio.to_thread(_has_operator_backup_proof, backup_dir):
            raise AutomationMigrationError("operator backup proof is required")

        if revision == _PRE_EXPAND_REVISION:
            await _upgrade_database(engine, _EXPAND_REVISION)
            await engine.dispose()
            engine = create_async_engine(database_url)

        if inventory.empty:
            await _upgrade_database(engine, "head")
            await engine.dispose()
            engine = create_async_engine(database_url)
            async with engine.connect() as connection:
                final_revision = await _current_revision(connection)
                complete, empty_install = await _automation_cutover_state(connection)
            if final_revision != _FINAL_REVISION or not complete or not empty_install:
                raise AutomationMigrationError("empty-install cutover is incomplete")
            return _completed_report(
                mode="execute",
                inventory=inventory,
                empty_install=True,
                noop=False,
            )

        await _execute_staging(engine, plan=plan)
        await _upgrade_database(engine, "head")
        await engine.dispose()
        engine = create_async_engine(database_url)
        inventory = await _resume_final_cutover(
            engine,
            targets=targets,
            complete_marker=True,
        )
        async with engine.connect() as connection:
            complete, empty_install = await _automation_cutover_state(connection)
        if not complete or empty_install:
            raise AutomationMigrationError("automation cutover marker is incomplete")
        return _completed_report(
            mode="execute",
            inventory=inventory,
            empty_install=False,
            noop=False,
        )
    except AutomationMigrationError:
        raise
    except Exception:
        raise AutomationMigrationError("automation migration failed safely") from None
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise AutomationMigrationError("DATABASE_URL is required")
        owner_map = load_owner_map(args.owner_map)
        report = asyncio.run(
            run_automation_migration(
                database_url,
                owner_map=owner_map,
                backup_dir=args.backup_dir,
                execute=args.execute,
            )
        )
        print(render_report(report))
        return 0
    except AutomationMigrationError:
        print("automation migration failed safely", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
