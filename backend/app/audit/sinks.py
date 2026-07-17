from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditAuthorityRejected,
    AuditOutcome,
    AuditProcess,
    AuditTarget,
    AuditTargetKind,
    SystemAuditContext,
)
from app.audit.service import AuditService
from app.private_work.context import (
    PrivateWorkContext,
    is_issued_private_work_context,
)
from app.private_work.run_repository import PrivateRunRecord
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.quotas.models import ProjectQuotaPolicy
from app.reliability.jobs import AdmittedJobRecord
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import (
    DeadJobRequeuedEvent,
    JobTerminalEvent,
)
from deerflow.runtime.private_scope import PrivateResourceScope


def _uuid(value: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise TypeError("audit sink authority is invalid") from None


class OperationalAuditSink:
    """Typed transactional audit ports used by project reliability domains."""

    def __init__(
        self,
        service: AuditService,
        *,
        process: AuditProcess,
    ) -> None:
        if type(service) is not AuditService or process not in {
            AuditProcess.GATEWAY,
            AuditProcess.WORKER,
            AuditProcess.SCHEDULER,
        }:
            raise TypeError("process-bound AuditService is required")
        self._service = service
        self._process = process

    def _require_process(self, *allowed: AuditProcess) -> None:
        if self._process not in allowed:
            raise AuditAuthorityRejected()

    async def run_admitted(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: PrivateRunRecord,
        job: AdmittedJobRecord,
    ) -> None:
        self._require_process(AuditProcess.GATEWAY)
        await self._service.append(
            session,
            AuditActor.user(_uuid(context.user_id)),
            AuditAction.RUN_ADMITTED,
            AuditTarget(
                AuditTargetKind.RUN,
                _uuid(run.run_id),
                _uuid(context.project_id),
            ),
            AuditOutcome.SUCCESS,
            {
                "job_type": job.job_type,
                "non_interactive": job.job_type == "automation_run",
            },
            request_id=context.request_id,
            job_id=_uuid(job.job_id),
        )

    async def run_cancel_requested(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        run_id: str,
        job_id: uuid.UUID,
    ) -> None:
        self._require_process(AuditProcess.GATEWAY)
        await self._service.append(
            session,
            AuditActor.user(_uuid(context.user_id)),
            AuditAction.RUN_CANCEL_REQUESTED,
            AuditTarget(
                AuditTargetKind.RUN,
                _uuid(run_id),
                _uuid(context.project_id),
            ),
            AuditOutcome.SUCCESS,
            {},
            request_id=context.request_id,
            job_id=_uuid(job_id),
        )

    async def member_role_changed(
        self,
        session: AsyncSession,
        context: ProjectContext,
        membership_id: uuid.UUID,
        previous_role: ProjectRole,
        role: ProjectRole,
    ) -> None:
        self._require_process(AuditProcess.GATEWAY)
        await self._service.append(
            session,
            AuditActor.user(_uuid(context.user_id)),
            AuditAction.MEMBER_ROLE_CHANGED,
            AuditTarget(
                AuditTargetKind.MEMBERSHIP,
                _uuid(membership_id),
                _uuid(context.project_id),
            ),
            AuditOutcome.SUCCESS,
            {
                "previous_role": previous_role.value,
                "role": role.value,
            },
            request_id=context.request_id,
        )

    async def member_ended(
        self,
        session: AsyncSession,
        context: ProjectContext,
        membership_id: uuid.UUID,
        status: str,
    ) -> None:
        self._require_process(AuditProcess.GATEWAY)
        action = {
            "removed": AuditAction.MEMBER_REMOVED,
            "left": AuditAction.MEMBER_LEFT,
        }.get(status)
        if action is None:
            raise TypeError("membership audit status is invalid")
        await self._service.append(
            session,
            AuditActor.user(_uuid(context.user_id)),
            action,
            AuditTarget(
                AuditTargetKind.MEMBERSHIP,
                _uuid(membership_id),
                _uuid(context.project_id),
            ),
            AuditOutcome.SUCCESS,
            {},
            request_id=context.request_id,
        )

    async def quota_policy_updated(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        policy: ProjectQuotaPolicy,
    ) -> None:
        self._require_process(AuditProcess.GATEWAY)
        if not is_issued_private_work_context(context) or type(policy) is not ProjectQuotaPolicy:
            raise AuditAuthorityRejected()
        configured = policy.configured
        await self._service.append(
            session,
            AuditActor.user(_uuid(context.user_id)),
            AuditAction.QUOTA_POLICY_UPDATED,
            AuditTarget(
                AuditTargetKind.QUOTA,
                _uuid(context.project_id),
                _uuid(context.project_id),
            ),
            AuditOutcome.SUCCESS,
            {
                "member_limit": configured.member_limit,
                "storage_bytes_limit": configured.storage_bytes_limit,
                "concurrent_run_limit": configured.concurrent_run_limit,
                "mcp_calls_daily_limit": configured.mcp_calls_daily_limit,
                "version": policy.version,
            },
            request_id=context.request_id,
        )

    async def automation_admitted(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        task_id: str,
        trigger: str,
        run: PrivateRunRecord,
        job: AdmittedJobRecord,
    ) -> None:
        if trigger == "manual":
            self._require_process(AuditProcess.GATEWAY)
            actor = AuditActor.user(_uuid(context.user_id))
        elif trigger == "scheduled":
            self._require_process(AuditProcess.SCHEDULER)
            actor = AuditActor.trusted_process(self._process)
        else:
            raise TypeError("automation audit trigger is invalid")
        await self._service.append(
            session,
            actor,
            AuditAction.AUTOMATION_TRIGGERED,
            AuditTarget(
                AuditTargetKind.AUTOMATION,
                _uuid(task_id),
                _uuid(context.project_id),
            ),
            AuditOutcome.SUCCESS,
            {"trigger_kind": trigger},
            request_id=context.request_id,
            job_id=_uuid(job.job_id),
        )
        await self._service.append(
            session,
            actor,
            AuditAction.RUN_ADMITTED,
            AuditTarget(
                AuditTargetKind.RUN,
                _uuid(run.run_id),
                _uuid(context.project_id),
            ),
            AuditOutcome.SUCCESS,
            {"job_type": "automation_run", "non_interactive": True},
            request_id=context.request_id,
            job_id=_uuid(job.job_id),
        )

    async def run_terminal(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        job_type: str,
        status: str,
        public_error_code: str | None,
        request_id: str,
    ) -> None:
        terminal_status = {
            "success": "completed",
            "error": "failed",
            "timeout": "failed",
            "interrupted": "cancelled",
        }.get(status)
        if terminal_status is None or job_type not in {
            "private_run",
            "automation_run",
        }:
            raise TypeError("run terminal audit event is invalid")
        if self._process is AuditProcess.GATEWAY:
            if terminal_status != "cancelled":
                raise AuditAuthorityRejected()
        else:
            self._require_process(AuditProcess.WORKER)
        await self._service.append(
            session,
            AuditActor.trusted_process(self._process),
            AuditAction.RUN_TERMINAL,
            AuditTarget(
                AuditTargetKind.RUN,
                _uuid(run_id),
                _uuid(scope.project_id),
            ),
            AuditOutcome.SUCCESS,
            {
                "job_type": job_type,
                "status": terminal_status,
                "public_error_code": public_error_code,
            },
            public_error_code=public_error_code,
            request_id=request_id,
            job_id=_uuid(job_id),
        )

    async def job_terminalized(
        self,
        session: AsyncSession,
        event: JobTerminalEvent,
    ) -> None:
        self._require_process(AuditProcess.WORKER)
        actor = AuditActor.trusted_process(self._process)
        if event.status == "dead":
            await self._service.append(
                session,
                actor,
                AuditAction.JOB_DEAD,
                AuditTarget(
                    AuditTargetKind.JOB,
                    _uuid(event.job_id),
                    _uuid(event.project_id),
                ),
                AuditOutcome.SUCCESS,
                {
                    "job_type": event.job_type,
                    "public_error_code": event.public_error_code,
                    "attempt_count": event.attempt_count,
                    "retry_safety": event.retry_safety,
                },
                public_error_code=event.public_error_code,
                request_id="worker-job-terminal",
                job_id=_uuid(event.job_id),
                occurred_at=event.occurred_at,
            )


class SystemJobAuditSink:
    """System-admin-bound safe requeue audit port."""

    def __init__(
        self,
        service: AuditService,
        context: SystemAuditContext,
    ) -> None:
        self._service = service
        self._actor = AuditActor.system_admin(context)
        self._request_id = context.request_id

    async def dead_job_requeued(
        self,
        session: AsyncSession,
        event: DeadJobRequeuedEvent,
    ) -> None:
        if type(event) is not DeadJobRequeuedEvent or event.request_id != self._request_id:
            raise AuditAuthorityRejected()
        row = await session.get(JobRow, event.successor_job_id)
        if row is None or row.project_id != event.project_id or row.predecessor_dead_job_id != event.predecessor_job_id or row.status != "queued" or row.retry_safety != "safe":
            raise AuditAuthorityRejected()
        await self._service.append(
            session,
            self._actor,
            AuditAction.JOB_REQUEUED,
            AuditTarget(
                AuditTargetKind.JOB,
                event.successor_job_id,
                event.project_id,
            ),
            AuditOutcome.SUCCESS,
            {
                "job_type": row.job_type,
                "attempt_count": row.attempt_count,
                "retry_safety": row.retry_safety,
            },
            request_id=self._request_id,
            job_id=event.successor_job_id,
        )


class TrustedOperationAuditSink:
    """Process-bound contracts for forward-owned backup and recovery callers."""

    def __init__(
        self,
        service: AuditService,
        *,
        process: AuditProcess,
    ) -> None:
        if type(service) is not AuditService or process not in {
            AuditProcess.OPERATOR,
            AuditProcess.RECOVERY,
            AuditProcess.WORKER,
        }:
            raise TypeError("trusted-operation AuditService is required")
        self._service = service
        self._process = process

    def _require_process(self, *allowed: AuditProcess) -> None:
        if self._process not in allowed:
            raise AuditAuthorityRejected()

    async def backup_created(
        self,
        session: AsyncSession,
        *,
        backup_id: uuid.UUID,
        table_count: int,
        tombstone_high_watermark: int,
        request_id: str,
    ) -> None:
        self._require_process(AuditProcess.OPERATOR)
        await self._service.append(
            session,
            AuditActor.trusted_process(self._process),
            AuditAction.BACKUP_CREATED,
            AuditTarget(
                AuditTargetKind.BACKUP,
                _uuid(backup_id),
                None,
            ),
            AuditOutcome.SUCCESS,
            {
                "table_count": table_count,
                "tombstone_high_watermark": tombstone_high_watermark,
            },
            request_id=request_id,
        )

    async def restore_started(
        self,
        session: AsyncSession,
        *,
        restore_id: uuid.UUID,
        table_count: int,
        tombstones_replayed: int,
        request_id: str,
    ) -> None:
        await self._restore_event(
            session,
            action=AuditAction.RESTORE_STARTED,
            restore_id=restore_id,
            table_count=table_count,
            tombstones_replayed=tombstones_replayed,
            request_id=request_id,
        )

    async def restore_completed(
        self,
        session: AsyncSession,
        *,
        restore_id: uuid.UUID,
        table_count: int,
        tombstones_replayed: int,
        request_id: str,
    ) -> None:
        await self._restore_event(
            session,
            action=AuditAction.RESTORE_COMPLETED,
            restore_id=restore_id,
            table_count=table_count,
            tombstones_replayed=tombstones_replayed,
            request_id=request_id,
        )

    async def recovery_drill_completed(
        self,
        session: AsyncSession,
        *,
        restore_id: uuid.UUID,
        table_count: int,
        tombstones_replayed: int,
        request_id: str,
    ) -> None:
        await self._restore_event(
            session,
            action=AuditAction.RECOVERY_DRILL_COMPLETED,
            restore_id=restore_id,
            table_count=table_count,
            tombstones_replayed=tombstones_replayed,
            request_id=request_id,
        )

    async def _restore_event(
        self,
        session: AsyncSession,
        *,
        action: AuditAction,
        restore_id: uuid.UUID,
        table_count: int,
        tombstones_replayed: int,
        request_id: str,
    ) -> None:
        self._require_process(AuditProcess.OPERATOR, AuditProcess.RECOVERY)
        await self._service.append(
            session,
            AuditActor.trusted_process(self._process),
            action,
            AuditTarget(
                AuditTargetKind.RESTORE,
                _uuid(restore_id),
                None,
            ),
            AuditOutcome.SUCCESS,
            {
                "table_count": table_count,
                "tombstones_replayed": tombstones_replayed,
            },
            request_id=request_id,
        )

    async def purge_completed(
        self,
        session: AsyncSession,
        *,
        purge_id: uuid.UUID,
        project_id: uuid.UUID | None,
        resource_kind: str,
        purged_count: int,
        request_id: str,
    ) -> None:
        if resource_kind == "account":
            self._require_process(AuditProcess.RECOVERY)
        elif resource_kind in {"project", "file"}:
            self._require_process(AuditProcess.WORKER, AuditProcess.RECOVERY)
        else:
            raise TypeError("purge audit resource kind is invalid")
        await self._service.append(
            session,
            AuditActor.trusted_process(self._process),
            AuditAction.PURGE_COMPLETED,
            AuditTarget(
                AuditTargetKind.PURGE,
                _uuid(purge_id),
                None if project_id is None else _uuid(project_id),
            ),
            AuditOutcome.SUCCESS,
            {
                "resource_kind": resource_kind,
                "purged_count": purged_count,
            },
            request_id=request_id,
        )


__all__ = [
    "OperationalAuditSink",
    "SystemJobAuditSink",
    "TrustedOperationAuditSink",
]
