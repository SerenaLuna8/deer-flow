from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditOutcome,
    AuditProcess,
    AuditTarget,
    AuditTargetKind,
    SystemAuditContext,
)
from app.audit.service import AuditService
from app.private_work.context import PrivateWorkContext
from app.private_work.run_repository import PrivateRunRecord
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.jobs import AdmittedJobRecord
from deerflow.persistence.jobs.sql import JobTerminalEvent
from deerflow.runtime.private_scope import PrivateResourceScope


def _uuid(value: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise TypeError("audit sink authority is invalid") from None


class OperationalAuditSink:
    """Typed transactional audit ports used by project reliability domains."""

    def __init__(self, service: AuditService) -> None:
        if type(service) is not AuditService:
            raise TypeError("AuditService is required")
        self._service = service

    async def run_admitted(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: PrivateRunRecord,
        job: AdmittedJobRecord,
    ) -> None:
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
        actor = AuditActor.user(_uuid(context.user_id)) if trigger == "manual" else AuditActor.trusted_process(AuditProcess.SCHEDULER)
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
        await self._service.append(
            session,
            AuditActor.trusted_process(AuditProcess.WORKER),
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
        actor = AuditActor.trusted_process(AuditProcess.WORKER)
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

    async def dead_job_requeued(self, session, event) -> None:
        from deerflow.persistence.jobs.model import JobRow

        row = await session.get(JobRow, event.successor_job_id)
        if row is None:
            raise RuntimeError("requeued job audit authority is missing")
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


__all__ = ["OperationalAuditSink", "SystemJobAuditSink"]
