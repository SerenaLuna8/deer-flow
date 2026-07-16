from __future__ import annotations

from datetime import datetime

from app.automations.errors import AutomationUnavailable
from deerflow.persistence.scheduled_task_runs import (
    TERMINAL_OCCURRENCE_STATUSES,
    ScheduledTaskRunRecord,
    ScheduledTaskRunRepository,
)
from deerflow.persistence.scheduled_tasks import ScheduledTaskRecord, ScheduledTaskRepository
from deerflow.runtime.private_scope import PrivateResourceScope


def _once_terminal_status(outcome: str) -> str | None:
    return {
        "success": "completed",
        "failed": "failed",
        "rejected": "failed",
        "interrupted": "cancelled",
        "cancelled": "cancelled",
        "skipped": "cancelled",
    }.get(outcome)


async def _record_parent_outcome(
    tasks: ScheduledTaskRepository,
    scope: PrivateResourceScope,
    task: ScheduledTaskRecord,
    *,
    outcome: str,
    error_code: str | None,
    occurred_at: datetime,
    request_id: str,
) -> None:
    updated = await tasks.record_automation_outcome(
        scope,
        task.id,
        outcome=outcome,
        error_code=error_code,
        occurred_at=occurred_at,
        terminal_status=_once_terminal_status(outcome) if task.schedule_type == "once" else None,
    )
    if updated is None:
        raise AutomationUnavailable(request_id)


async def settle_created_terminal_occurrence(
    tasks: ScheduledTaskRepository,
    scope: PrivateResourceScope,
    task: ScheduledTaskRecord,
    occurrence: ScheduledTaskRunRecord,
    *,
    occurred_at: datetime,
    request_id: str,
) -> None:
    if occurrence.status not in TERMINAL_OCCURRENCE_STATUSES:
        raise ValueError("occurrence must already be terminal")
    await _record_parent_outcome(
        tasks,
        scope,
        task,
        outcome=occurrence.status,
        error_code=occurrence.error_code,
        occurred_at=occurred_at,
        request_id=request_id,
    )


async def settle_terminal_occurrence(
    tasks: ScheduledTaskRepository,
    occurrences: ScheduledTaskRunRepository,
    scope: PrivateResourceScope,
    task: ScheduledTaskRecord,
    occurrence: ScheduledTaskRunRecord,
    *,
    status: str,
    error_code: str | None,
    error_message: str | None,
    finished_at: datetime,
    request_id: str,
    thread_id: str | None = None,
    run_id: str | None = None,
) -> bool:
    run_coordinates = {}
    if thread_id is not None:
        run_coordinates["thread_id"] = thread_id
    if run_id is not None:
        run_coordinates["run_id"] = run_id
    changed = await occurrences.finish(
        scope,
        occurrence.id,
        status=status,
        error_code=error_code,
        error_message=error_message,
        finished_at=finished_at,
        **run_coordinates,
    )
    if not changed:
        return False
    await _record_parent_outcome(
        tasks,
        scope,
        task,
        outcome=status,
        error_code=error_code,
        occurred_at=finished_at,
        request_id=request_id,
    )
    return True


__all__ = ["settle_created_terminal_occurrence", "settle_terminal_occurrence"]
