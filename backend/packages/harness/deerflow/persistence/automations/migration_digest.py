"""Canonical Automation migration target projections and digests.

The explicit migration runner and the destructive finalize revision must agree
on the exact bytes represented by a migration ledger receipt.  Keep that
contract in this dependency-light module so both paths use one implementation.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
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
