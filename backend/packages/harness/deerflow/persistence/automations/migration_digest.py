"""Canonical Automation migration target projections and digests.

The explicit migration runner and the destructive finalize revision must agree
on the exact bytes represented by a migration ledger receipt.  Keep that
contract in this dependency-light module so both paths use one implementation.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime

AUTOMATION_FINALIZE_LOCK_SQL = "LOCK TABLE scheduled_tasks, scheduled_task_runs IN ACCESS EXCLUSIVE MODE"

AUTOMATION_LEGACY_COLUMNS: dict[str, tuple[str, ...]] = {
    "scheduled_tasks": (
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
    ),
    "scheduled_task_runs": (
        "id",
        "task_id",
        "thread_id",
        "run_id",
        "scheduled_for",
        "trigger",
        "status",
        "error",
        "started_at",
        "finished_at",
        "created_at",
    ),
}


AUTOMATION_TARGET_COLUMNS: dict[str, tuple[str, ...]] = {
    "scheduled_tasks": (
        "id",
        "project_id",
        "owner_user_id",
        "thread_id",
        "context_mode",
        "agent_asset_id",
        "agent_scope",
        "title",
        "prompt",
        "schedule_type",
        "schedule_spec",
        "timezone",
        "status",
        "overlap_policy",
        "next_run_at",
        "last_run_at",
        "last_outcome",
        "last_error_code",
        "run_count",
        "version",
        "frozen_at",
        "deleted_at",
        "created_at",
        "updated_at",
    ),
    "scheduled_task_runs": (
        "id",
        "project_id",
        "owner_user_id",
        "task_id",
        "task_version",
        "occurrence_key",
        "manual_idempotency_hash",
        "scheduled_for",
        "trigger",
        "status",
        "thread_id",
        "run_id",
        "resolved_membership_id",
        "resolved_membership_version",
        "launch_attempt_count",
        "lease_owner",
        "lease_expires_at",
        "next_attempt_at",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    ),
}


AUTOMATION_EXPANDED_COLUMNS: dict[str, tuple[str, ...]] = {domain: legacy + tuple(column for column in AUTOMATION_TARGET_COLUMNS[domain] if column not in legacy) for domain, legacy in AUTOMATION_LEGACY_COLUMNS.items()}

_FINAL_TASK_CONTEXT_MODES = frozenset({"fresh_thread_per_run", "reuse_thread"})
_FINAL_TASK_SCHEDULE_TYPES = frozenset({"once", "cron"})
_FINAL_TASK_STATUSES = frozenset({"enabled", "paused", "completed", "failed", "cancelled"})
_FINAL_TASK_AGENT_SCOPES = frozenset({"system", "project"})
_FINAL_TASK_OUTCOMES = frozenset({"success", "failed", "skipped", "interrupted", "cancelled", "rejected"})
_FINAL_RUN_TRIGGERS = frozenset({"scheduled", "manual"})
_FINAL_RUN_STATUSES = frozenset({"queued", "launching", "running", "success", "failed", "skipped", "interrupted", "cancelled", "rejected"})


def _integer_at_least(value: object, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _has_duplicate_non_null_key(
    rows: Sequence[Mapping[str, object]],
    columns: Sequence[str],
    *,
    predicate_column: str | None = None,
) -> bool:
    seen: set[tuple[object, ...]] = set()
    for row in rows:
        if predicate_column is not None and row.get(predicate_column) is None:
            continue
        key = tuple(row.get(column) for column in columns)
        if any(value is None for value in key):
            continue
        if key in seen:
            return True
        seen.add(key)
    return False


def final_target_rows_satisfy_constraints(
    task_rows: Sequence[Mapping[str, object]],
    run_rows: Sequence[Mapping[str, object]],
) -> bool:
    """Mirror every CHECK and data-uniqueness constraint installed by 0013."""

    for row in task_rows:
        context_mode = row.get("context_mode")
        thread_id = row.get("thread_id")
        if (
            context_mode not in _FINAL_TASK_CONTEXT_MODES
            or row.get("schedule_type") not in _FINAL_TASK_SCHEDULE_TYPES
            or row.get("status") not in _FINAL_TASK_STATUSES
            or row.get("overlap_policy") != "skip"
            or not ((context_mode == "reuse_thread" and thread_id is not None) or (context_mode == "fresh_thread_per_run" and thread_id is None))
            or row.get("agent_scope") not in _FINAL_TASK_AGENT_SCOPES
            or not _integer_at_least(row.get("version"), 1)
            or not _integer_at_least(row.get("run_count"), 0)
            or (row.get("last_outcome") is not None and row.get("last_outcome") not in _FINAL_TASK_OUTCOMES)
        ):
            return False

    for row in run_rows:
        if (
            row.get("trigger") not in _FINAL_RUN_TRIGGERS
            or row.get("status") not in _FINAL_RUN_STATUSES
            or (row.get("run_id") is not None and row.get("thread_id") is None)
            or not _integer_at_least(row.get("launch_attempt_count"), 0)
            or (row.get("resolved_membership_version") is not None and not _integer_at_least(row.get("resolved_membership_version"), 1))
            or not _integer_at_least(row.get("task_version"), 1)
        ):
            return False

    if _has_duplicate_non_null_key(task_rows, ("project_id", "owner_user_id", "id")):
        return False
    if _has_duplicate_non_null_key(run_rows, ("project_id", "owner_user_id", "task_id", "occurrence_key")):
        return False
    return not _has_duplicate_non_null_key(
        run_rows,
        ("project_id", "owner_user_id", "task_id", "manual_idempotency_hash"),
        predicate_column="manual_idempotency_hash",
    )


def canonical_value(value: object) -> object:
    """Convert database values into a stable JSON representation."""

    if isinstance(value, Mapping):
        return {str(key): canonical_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    if isinstance(value, (datetime, date, uuid.UUID)):
        return str(value)
    if isinstance(value, bytes):
        return {
            "sha256": hashlib.sha256(value).hexdigest(),
            "size": len(value),
        }
    return value


def canonical_digest(value: object) -> str:
    """Return the stable SHA-256 receipt digest for canonical JSON values."""

    encoded = json.dumps(
        canonical_value(value),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def target_select_sql(domain: str) -> str:
    """Return a fixed target projection ordered by the immutable row key."""

    try:
        columns = AUTOMATION_TARGET_COLUMNS[domain]
    except KeyError:
        raise ValueError("unsupported Automation migration domain") from None
    projection = ",".join(f'"{column}"' for column in columns)
    return f'SELECT {projection} FROM "{domain}" ORDER BY id'


def expanded_select_sql(domain: str) -> str:
    """Return the exact 0012 source projection ordered by immutable row key."""

    try:
        columns = AUTOMATION_EXPANDED_COLUMNS[domain]
    except KeyError:
        raise ValueError("unsupported Automation migration domain") from None
    projection = ",".join(f'"{column}"' for column in columns)
    return f'SELECT {projection} FROM "{domain}" ORDER BY id'
