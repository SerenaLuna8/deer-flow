"""Atomic terminal convergence for private Run jobs."""

from __future__ import annotations

import asyncio

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.execution_approval_audit import (
    NoopHostExecutionApprovalAudit,
)
from app.private_work.execution_approval_lifecycle import (
    converge_execution_approvals_for_terminal_job,
)
from app.reliability.run_execution.ports import (
    NoopPrivateRunExecutionAudit,
    NoopPrivateRunExecutionQuota,
    PrivateRunExecutionAuditPort,
    PrivateRunExecutionQuotaPort,
)
from deerflow.persistence.jobs.sql import (
    DurableDeadTerminalReconciliationRequest,
    DurableTerminalSuccessorRebindRequest,
    DurableTerminalTakeoverRequest,
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
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.trace_context import normalize_trace_id


class PrivateRunJobTerminalPort:
    """Atomically converge an unowned private job, Run, and staging files."""

    def __init__(
        self,
        *,
        quota: PrivateRunExecutionQuotaPort | None = None,
        audit: PrivateRunExecutionAuditPort | None = None,
        event_store: DbRunEventStore | None = None,
    ) -> None:
        self._automation_reconciliation_pending = asyncio.Event()
        self._quota = quota or NoopPrivateRunExecutionQuota()
        self._audit = audit or NoopPrivateRunExecutionAudit()
        self._event_store = event_store
        self._execution_approval_audit = (
            self._audit
            if callable(
                getattr(
                    self._audit,
                    "host_execution_approval_terminal",
                    None,
                ),
            )
            else NoopHostExecutionApprovalAudit()
        )

    def take_automation_reconciliation_pending(self) -> bool:
        if not self._automation_reconciliation_pending.is_set():
            return False
        self._automation_reconciliation_pending.clear()
        return True

    def restore_automation_reconciliation_pending(self) -> None:
        self._automation_reconciliation_pending.set()

    async def durable_terminal_takeover_allowed(
        self,
        session: AsyncSession,
        event: DurableTerminalTakeoverRequest,
    ) -> bool:
        """Authorize a settlement-only Job Attempt from an immutable terminal."""

        if type(event) is not DurableTerminalTakeoverRequest:
            raise TypeError("durable terminal takeover request is required")
        if self._event_store is None:
            return False
        origin_trace_id = normalize_trace_id(event.origin_trace_id)
        if origin_trace_id is None:
            return False
        run = await session.scalar(
            sa.select(RunRow)
            .where(
                RunRow.project_id == event.project_id,
                RunRow.owner_user_id == event.owner_user_id,
                RunRow.run_id == event.run_id,
                RunRow.job_id == event.job_id,
                RunRow.origin_trace_id == origin_trace_id,
                RunRow.status == "running",
                RunRow.authorization_cancel_requested_at.is_(None),
            )
            .with_for_update(of=RunRow)
            .execution_options(populate_existing=True)
        )
        if run is None:
            return False
        membership_version = await session.scalar(
            sa.select(ProjectMembershipRow.version).where(
                ProjectMembershipRow.project_id == event.project_id,
                ProjectMembershipRow.user_id == event.owner_user_id,
                ProjectMembershipRow.status == "active",
            )
        )
        if not isinstance(membership_version, int) or membership_version < 1:
            return False
        return await self._has_durable_terminal_proof(
            session,
            scope=PrivateResourceScope(
                project_id=str(event.project_id),
                owner_user_id=event.owner_user_id,
                membership_version=membership_version,
            ),
            thread_id=run.thread_id,
            run_id=event.run_id,
        )

    async def durable_dead_terminal_reconciliation_allowed(
        self,
        session: AsyncSession,
        event: DurableDeadTerminalReconciliationRequest,
    ) -> bool:
        """Prove a dead Job can only settle an already-produced terminal."""

        if type(event) is not DurableDeadTerminalReconciliationRequest:
            raise TypeError("durable dead terminal reconciliation request is required")
        if self._event_store is None:
            return False
        origin_trace_id = normalize_trace_id(event.origin_trace_id)
        if origin_trace_id is None:
            return False
        run = await session.scalar(
            sa.select(RunRow)
            .where(
                RunRow.project_id == event.project_id,
                RunRow.owner_user_id == event.owner_user_id,
                RunRow.run_id == event.run_id,
                RunRow.job_id == event.predecessor_job_id,
                RunRow.origin_trace_id == origin_trace_id,
                RunRow.status == "running",
                RunRow.authorization_cancel_requested_at.is_(None),
            )
            .with_for_update(of=RunRow)
            .execution_options(populate_existing=True)
        )
        if run is None:
            return False
        if event.job_type == "automation_run":
            if event.occurrence_id is None:
                return False
            occurrence = await session.scalar(
                sa.select(ScheduledTaskRunRow)
                .where(
                    ScheduledTaskRunRow.id == event.occurrence_id,
                    ScheduledTaskRunRow.project_id == event.project_id,
                    ScheduledTaskRunRow.owner_user_id == event.owner_user_id,
                    ScheduledTaskRunRow.run_id == event.run_id,
                    ScheduledTaskRunRow.job_id == event.predecessor_job_id,
                    ScheduledTaskRunRow.status == "running",
                )
                .with_for_update(of=ScheduledTaskRunRow)
                .execution_options(populate_existing=True)
            )
            if occurrence is None:
                return False
        elif event.occurrence_id is not None:
            return False
        membership_version = await session.scalar(
            sa.select(ProjectMembershipRow.version).where(
                ProjectMembershipRow.project_id == event.project_id,
                ProjectMembershipRow.user_id == event.owner_user_id,
                ProjectMembershipRow.status == "active",
            )
        )
        if not isinstance(membership_version, int) or membership_version < 1:
            return False
        return await self._has_durable_terminal_proof(
            session,
            scope=PrivateResourceScope(
                project_id=str(event.project_id),
                owner_user_id=event.owner_user_id,
                membership_version=membership_version,
            ),
            thread_id=run.thread_id,
            run_id=event.run_id,
        )

    async def _has_durable_terminal_proof(
        self,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
    ) -> bool:
        """Single seam for stream terminals and future internal candidates."""

        if self._event_store is None:
            return False
        candidate = await self._event_store.get_stream_terminal_candidate(
            session,
            scope=scope,
            thread_id=thread_id,
            run_id=run_id,
        )
        if candidate is not None:
            return True
        terminal = await self._event_store.get_stream_terminal(
            session,
            scope=scope,
            thread_id=thread_id,
            run_id=run_id,
        )
        return terminal is not None

    async def rebind_durable_terminal_successor(
        self,
        session: AsyncSession,
        event: DurableTerminalSuccessorRebindRequest,
    ) -> bool:
        """Bind a terminal-only successor without erasing dead lineage."""

        if type(event) is not DurableTerminalSuccessorRebindRequest:
            raise TypeError("durable terminal successor rebind request is required")
        origin_trace_id = normalize_trace_id(event.origin_trace_id)
        if origin_trace_id is None:
            return False
        run = await session.scalar(
            sa.select(RunRow)
            .where(
                RunRow.project_id == event.project_id,
                RunRow.owner_user_id == event.owner_user_id,
                RunRow.run_id == event.run_id,
                RunRow.job_id == event.predecessor_job_id,
                RunRow.origin_trace_id == origin_trace_id,
                RunRow.status == "running",
                RunRow.authorization_cancel_requested_at.is_(None),
            )
            .with_for_update(of=RunRow)
            .execution_options(populate_existing=True)
        )
        if run is None:
            return False
        occurrence = None
        if event.job_type == "automation_run":
            if event.occurrence_id is None:
                return False
            occurrence = await session.scalar(
                sa.select(ScheduledTaskRunRow)
                .where(
                    ScheduledTaskRunRow.id == event.occurrence_id,
                    ScheduledTaskRunRow.project_id == event.project_id,
                    ScheduledTaskRunRow.owner_user_id == event.owner_user_id,
                    ScheduledTaskRunRow.run_id == event.run_id,
                    ScheduledTaskRunRow.job_id == event.predecessor_job_id,
                    ScheduledTaskRunRow.status == "running",
                )
                .with_for_update(of=ScheduledTaskRunRow)
                .execution_options(populate_existing=True)
            )
            if occurrence is None:
                return False
        elif event.occurrence_id is not None:
            return False
        run.job_id = event.successor_job_id
        run.execution_lease_token_hash = None
        run.execution_lease_expires_at = None
        run.execution_heartbeat_at = None
        run.updated_at = event.occurred_at
        if occurrence is not None:
            occurrence.job_id = event.successor_job_id
            occurrence.updated_at = event.occurred_at
        await session.flush()
        return True

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
        terminal_run = await session.scalar(
            sa.select(RunRow.run_id)
            .where(
                RunRow.project_id == event.project_id,
                RunRow.owner_user_id == event.owner_user_id,
                RunRow.run_id == event.run_id,
                RunRow.job_id == event.job_id,
            )
            .with_for_update(of=RunRow)
        )
        if terminal_run is None:
            raise RuntimeError("private terminal Run authority is missing")
        # Job settlement owns Job first; take Run before approval so every
        # host-execution mutation follows the same dependency order.
        await converge_execution_approvals_for_terminal_job(
            session,
            project_id=event.project_id,
            owner_user_id=event.owner_user_id,
            run_id=event.run_id,
            job_id=event.job_id,
            terminal_job_status=event.status,
            now=event.occurred_at,
            request_id=origin_trace_id,
            audit=self._execution_approval_audit,
        )
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
