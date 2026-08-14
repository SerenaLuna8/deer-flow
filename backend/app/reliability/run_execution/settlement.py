"""Atomic terminal convergence for private Run jobs."""

from __future__ import annotations

import asyncio

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.reliability.run_execution.ports import (
    NoopPrivateRunExecutionAudit,
    NoopPrivateRunExecutionQuota,
    PrivateRunExecutionAuditPort,
    PrivateRunExecutionQuotaPort,
)
from deerflow.persistence.jobs.sql import (
    JobTerminalEvent,
    JobTerminalResult,
)
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDreamPrepareRunRow,
)
from deerflow.persistence.private_work.model import PrivateFileRow
from deerflow.persistence.projects.model import (
    ProjectMembershipRow,
    ProjectRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.scheduled_task_runs.model import (
    ScheduledTaskRunRow,
)
from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.trace_context import normalize_trace_id


class PrivateRunJobTerminalPort:
    """Atomically converge an unowned private job, Run, and staging files."""

    def __init__(
        self,
        *,
        quota: PrivateRunExecutionQuotaPort | None = None,
        audit: PrivateRunExecutionAuditPort | None = None,
    ) -> None:
        self._automation_reconciliation_pending = asyncio.Event()
        self._quota = quota or NoopPrivateRunExecutionQuota()
        self._audit = audit or NoopPrivateRunExecutionAudit()

    def take_automation_reconciliation_pending(self) -> bool:
        if not self._automation_reconciliation_pending.is_set():
            return False
        self._automation_reconciliation_pending.clear()
        return True

    def restore_automation_reconciliation_pending(self) -> None:
        self._automation_reconciliation_pending.set()

    async def job_terminalized(
        self,
        session: AsyncSession,
        event: JobTerminalEvent,
    ) -> JobTerminalResult:
        if event.job_type == "memory_dream_prepare":
            # claim_next owns Project -> Membership -> Thread -> preparation
            # -> Job. Custom settlements own that same lock prefix.
            phase = "cancelled" if event.status == "cancelled" else "failed"
            disposition = "cancelled" if event.status == "cancelled" else "failed"
            await session.execute(
                sa.update(MemoryDreamPrepareRunRow)
                .where(
                    MemoryDreamPrepareRunRow.job_id == event.job_id,
                    MemoryDreamPrepareRunRow.project_id == event.project_id,
                    MemoryDreamPrepareRunRow.owner_user_id == event.owner_user_id,
                    MemoryDreamPrepareRunRow.completed_at.is_(None),
                )
                .values(
                    phase=phase,
                    result_disposition=disposition,
                    completed_at=event.occurred_at,
                    updated_at=event.occurred_at,
                )
            )
        if event.job_type not in {"private_run", "automation_run"}:
            await self._audit.job_terminalized(session, event)
            return JobTerminalResult(run_terminal_published=False)
        if event.owner_user_id is None or event.run_id is None:
            raise RuntimeError("private job terminal authority is incomplete")
        origin_trace_id = normalize_trace_id(event.origin_trace_id)
        if origin_trace_id is None:
            raise RuntimeError("private job terminal trace authority is incomplete")
        project_exists = await session.scalar(sa.select(ProjectRow.id).where(ProjectRow.id == event.project_id).with_for_update(of=ProjectRow))
        membership_version = await session.scalar(
            sa.select(ProjectMembershipRow.version)
            .where(
                ProjectMembershipRow.project_id == event.project_id,
                ProjectMembershipRow.user_id == event.owner_user_id,
            )
            .with_for_update(of=ProjectMembershipRow)
        )
        if project_exists is None or membership_version is None:
            raise RuntimeError("private job terminal membership is missing")
        run_status = "interrupted" if event.status == "cancelled" else "error"
        run_error = event.cancel_reason if event.status == "cancelled" else event.public_error_code
        finalization_status = sa.case(
            (RunRow.finalization_status == "finalizing", "failed"),
            else_=RunRow.finalization_status,
        )
        terminalized = await session.execute(
            sa.update(RunRow)
            .where(
                RunRow.project_id == event.project_id,
                RunRow.owner_user_id == event.owner_user_id,
                RunRow.run_id == event.run_id,
                RunRow.job_id == event.job_id,
                RunRow.origin_trace_id == origin_trace_id,
                RunRow.status.in_(("pending", "running")),
            )
            .values(
                status=run_status,
                error=run_error,
                execution_lease_token_hash=None,
                execution_lease_expires_at=None,
                execution_heartbeat_at=None,
                finalization_status=finalization_status,
                updated_at=event.occurred_at,
            )
        )
        if terminalized.rowcount == 1:
            scope = PrivateResourceScope(
                project_id=str(event.project_id),
                owner_user_id=event.owner_user_id,
                membership_version=membership_version,
            )
            await self._quota.release_concurrent_run(
                session,
                scope,
                run_id=event.run_id,
                request_id=origin_trace_id,
            )
            await self._audit.run_terminal(
                session,
                scope,
                run_id=event.run_id,
                job_id=event.job_id,
                job_type=event.job_type,
                status=run_status,
                public_error_code=event.public_error_code,
                request_id=origin_trace_id,
            )
        run_terminal_published = terminalized.rowcount == 1
        if event.job_type == "automation_run":
            if event.occurrence_id is None:
                raise RuntimeError("automation job terminal authority is incomplete")
            occurrence_status = "interrupted" if event.status == "cancelled" else "failed"
            error_code = event.cancel_reason if event.status == "cancelled" else event.public_error_code
            task_id = await session.scalar(
                sa.select(ScheduledTaskRunRow.task_id).where(
                    ScheduledTaskRunRow.id == event.occurrence_id,
                    ScheduledTaskRunRow.project_id == event.project_id,
                    ScheduledTaskRunRow.owner_user_id == event.owner_user_id,
                    ScheduledTaskRunRow.run_id == event.run_id,
                    ScheduledTaskRunRow.job_id == event.job_id,
                )
            )
            task = None
            if task_id is not None:
                task = (
                    await session.execute(
                        sa.select(ScheduledTaskRow)
                        .where(
                            ScheduledTaskRow.id == task_id,
                            ScheduledTaskRow.project_id == event.project_id,
                            ScheduledTaskRow.owner_user_id == event.owner_user_id,
                        )
                        .with_for_update(
                            of=ScheduledTaskRow,
                            skip_locked=True,
                        )
                    )
                ).scalar_one_or_none()
            occurrence = None
            if task is not None:
                occurrence = (
                    await session.execute(
                        sa.select(ScheduledTaskRunRow)
                        .where(
                            ScheduledTaskRunRow.id == event.occurrence_id,
                            ScheduledTaskRunRow.project_id == event.project_id,
                            ScheduledTaskRunRow.owner_user_id == event.owner_user_id,
                            ScheduledTaskRunRow.task_id == task.id,
                            ScheduledTaskRunRow.run_id == event.run_id,
                            ScheduledTaskRunRow.job_id == event.job_id,
                            ScheduledTaskRunRow.status.in_(
                                ("queued", "launching", "running"),
                            ),
                        )
                        .with_for_update(
                            of=ScheduledTaskRunRow,
                            skip_locked=True,
                        )
                    )
                ).scalar_one_or_none()
            if task is not None and occurrence is not None:
                occurrence.status = occurrence_status
                occurrence.error_code = error_code
                occurrence.error_message = None
                occurrence.finished_at = event.occurred_at
                occurrence.updated_at = event.occurred_at
                if task.schedule_type == "once":
                    task.status = "cancelled" if occurrence_status == "interrupted" else "failed"
                    task.next_run_at = None
                task.last_run_at = event.occurred_at
                task.last_outcome = occurrence_status
                task.last_error_code = error_code
                task.run_count += 1
                task.updated_at = event.occurred_at
            else:
                self._automation_reconciliation_pending.set()
        await session.execute(
            sa.delete(PrivateFileRow).where(
                PrivateFileRow.project_id == event.project_id,
                PrivateFileRow.owner_user_id == event.owner_user_id,
                PrivateFileRow.created_by_run_id == event.run_id,
                PrivateFileRow.status == "staging",
            )
        )
        await self._audit.job_terminalized(session, event)
        return JobTerminalResult(
            run_terminal_published=run_terminal_published,
        )


__all__ = ["PrivateRunJobTerminalPort"]
