from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditAuthorityRejected,
    AuditOutcome,
    AuditProcess,
    AuditProcessContext,
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
from deerflow.persistence.jobs.sql import (
    DeadJobRequeuedEvent,
    JobTerminalEvent,
    consume_issued_dead_job_requeued_event,
)
from deerflow.runtime.private_scope import PrivateResourceScope


def _uuid(value: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise TypeError("audit sink authority is invalid") from None


def _automation_uuid(value: object) -> uuid.UUID:
    if type(value) is str and value.startswith("task-"):
        try:
            return uuid.UUID(hex=value.removeprefix("task-"))
        except ValueError:
            raise TypeError("audit sink authority is invalid") from None
    return _uuid(value)


class OperationalAuditSink:
    """Typed transactional audit ports used by project reliability domains."""

    def __init__(
        self,
        service: AuditService,
        *,
        process_context: AuditProcessContext,
    ) -> None:
        if type(service) is not AuditService:
            raise TypeError("process-bound AuditService is required")
        context = service.require_process_context(process_context)
        if context.process not in {
            AuditProcess.GATEWAY,
            AuditProcess.WORKER,
            AuditProcess.SCHEDULER,
        }:
            raise TypeError("process-bound AuditService is required")
        self._service = service
        self._process_context = context
        self._process = context.process

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
            request_id=run.origin_trace_id,
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

    async def project_created(
        self,
        session: AsyncSession,
        context: ProjectContext,
    ) -> None:
        await self._project_event(
            session,
            context.user_id,
            context.project_id,
            context.request_id,
            AuditAction.PROJECT_CREATED,
        )

    async def project_updated(
        self,
        session: AsyncSession,
        context: ProjectContext,
    ) -> None:
        await self._project_event(
            session,
            context.user_id,
            context.project_id,
            context.request_id,
            AuditAction.PROJECT_UPDATED,
        )

    async def project_deletion_requested(
        self,
        session: AsyncSession,
        context: ProjectContext,
    ) -> None:
        await self._project_event(
            session,
            context.user_id,
            context.project_id,
            context.request_id,
            AuditAction.PROJECT_DELETION_REQUESTED,
        )

    async def project_recovered(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        request_id: str,
    ) -> None:
        await self._project_event(
            session,
            user_id,
            project_id,
            request_id,
            AuditAction.PROJECT_RECOVERED,
        )

    async def project_suspended(
        self,
        session: AsyncSession,
        context: ProjectContext,
    ) -> None:
        await self._project_event(
            session,
            context.user_id,
            context.project_id,
            context.request_id,
            AuditAction.PROJECT_SUSPENDED,
        )

    async def project_resumed(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        request_id: str,
    ) -> None:
        await self._project_event(
            session,
            user_id,
            project_id,
            request_id,
            AuditAction.PROJECT_RESUMED,
        )

    async def _project_event(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        request_id: str,
        action: AuditAction,
    ) -> None:
        self._require_process(AuditProcess.GATEWAY)
        await self._service.append(
            session,
            AuditActor.user(_uuid(user_id)),
            action,
            AuditTarget(
                AuditTargetKind.PROJECT,
                _uuid(project_id),
                _uuid(project_id),
            ),
            AuditOutcome.SUCCESS,
            {},
            request_id=request_id,
        )

    async def invitation_created(
        self,
        session: AsyncSession,
        context: ProjectContext,
        invitation_id: uuid.UUID,
        role: ProjectRole,
    ) -> None:
        await self._invitation_event(
            session,
            context,
            invitation_id,
            role,
            AuditAction.INVITATION_CREATED,
        )

    async def invitation_revoked(
        self,
        session: AsyncSession,
        context: ProjectContext,
        invitation_id: uuid.UUID,
    ) -> None:
        await self._invitation_event(
            session,
            context,
            invitation_id,
            None,
            AuditAction.INVITATION_REVOKED,
        )

    async def _invitation_event(
        self,
        session: AsyncSession,
        context: ProjectContext,
        invitation_id: uuid.UUID,
        role: ProjectRole | None,
        action: AuditAction,
    ) -> None:
        self._require_process(AuditProcess.GATEWAY)
        await self._service.append(
            session,
            AuditActor.user(_uuid(context.user_id)),
            action,
            AuditTarget(
                AuditTargetKind.INVITATION,
                _uuid(invitation_id),
                _uuid(context.project_id),
            ),
            AuditOutcome.SUCCESS,
            {} if role is None else {"role": role.value},
            request_id=context.request_id,
        )

    async def invitation_redeemed_and_member_joined(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        invitation_id: uuid.UUID,
        membership_id: uuid.UUID,
        role: ProjectRole,
        request_id: str,
    ) -> None:
        self._require_process(AuditProcess.GATEWAY)
        actor = AuditActor.user(_uuid(user_id))
        project_uuid = _uuid(project_id)
        metadata = {"role": role.value}
        await self._service.append(
            session,
            actor,
            AuditAction.INVITATION_REDEEMED,
            AuditTarget(
                AuditTargetKind.INVITATION,
                _uuid(invitation_id),
                project_uuid,
            ),
            AuditOutcome.SUCCESS,
            metadata,
            request_id=request_id,
        )
        await self._service.append(
            session,
            actor,
            AuditAction.MEMBER_JOINED,
            AuditTarget(
                AuditTargetKind.MEMBERSHIP,
                _uuid(membership_id),
                project_uuid,
            ),
            AuditOutcome.SUCCESS,
            metadata,
            request_id=request_id,
        )

    async def automation_created(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        task_id: str,
    ) -> None:
        await self._automation_definition_event(
            session,
            context,
            task_id,
            AuditAction.AUTOMATION_CREATED,
        )

    async def automation_updated(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        task_id: str,
    ) -> None:
        await self._automation_definition_event(
            session,
            context,
            task_id,
            AuditAction.AUTOMATION_UPDATED,
        )

    async def automation_deleted(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        task_id: str,
    ) -> None:
        await self._automation_definition_event(
            session,
            context,
            task_id,
            AuditAction.AUTOMATION_DELETED,
        )

    async def _automation_definition_event(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        task_id: str,
        action: AuditAction,
    ) -> None:
        self._require_process(AuditProcess.GATEWAY)
        if not is_issued_private_work_context(context):
            raise AuditAuthorityRejected()
        await self._service.append(
            session,
            AuditActor.user(_uuid(context.user_id)),
            action,
            AuditTarget(
                AuditTargetKind.AUTOMATION,
                _automation_uuid(task_id),
                _uuid(context.project_id),
            ),
            AuditOutcome.SUCCESS,
            {},
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
            actor = AuditActor.trusted_process(self._process_context)
        else:
            raise TypeError("automation audit trigger is invalid")
        await self._service.append(
            session,
            actor,
            AuditAction.AUTOMATION_TRIGGERED,
            AuditTarget(
                AuditTargetKind.AUTOMATION,
                _automation_uuid(task_id),
                _uuid(context.project_id),
            ),
            AuditOutcome.SUCCESS,
            {"trigger_kind": trigger},
            request_id=run.origin_trace_id,
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
            request_id=run.origin_trace_id,
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
            AuditActor.trusted_process(self._process_context),
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

    async def run_files_finalized(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        request_id: str,
        created_count: int,
        modified_count: int,
        deleted_count: int,
        artifact_count: int,
        committed_bytes: int,
    ) -> None:
        self._require_process(AuditProcess.WORKER)
        await self._service.append(
            session,
            AuditActor.trusted_process(self._process_context),
            AuditAction.RUN_FILES_FINALIZED,
            AuditTarget(
                AuditTargetKind.RUN,
                _uuid(run_id),
                _uuid(scope.project_id),
            ),
            AuditOutcome.SUCCESS,
            {
                "created_count": created_count,
                "modified_count": modified_count,
                "deleted_count": deleted_count,
                "artifact_count": artifact_count,
                "committed_bytes": committed_bytes,
            },
            request_id=request_id,
            job_id=_uuid(job_id),
        )

    async def memory_remembered(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        request_id: str,
        kind: str,
    ) -> None:
        self._require_process(AuditProcess.WORKER)
        await self._service.append(
            session,
            AuditActor.trusted_process(self._process_context),
            AuditAction.MEMORY_REMEMBER,
            AuditTarget(
                AuditTargetKind.RUN,
                _uuid(run_id),
                _uuid(scope.project_id),
            ),
            AuditOutcome.SUCCESS,
            {"kind": kind},
            request_id=request_id,
            job_id=_uuid(job_id),
        )

    async def memory_recall_executed(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        request_id: str,
        result_bucket: str,
        matched_stage: str,
        tags_filtered: bool,
    ) -> None:
        self._require_process(AuditProcess.WORKER)
        await self._service.append(
            session,
            AuditActor.trusted_process(self._process_context),
            AuditAction.MEMORY_RECALL_EXECUTED,
            AuditTarget(
                AuditTargetKind.RUN,
                _uuid(run_id),
                _uuid(scope.project_id),
            ),
            AuditOutcome.SUCCESS,
            {
                "result_bucket": result_bucket,
                "matched_stage": matched_stage,
                "tags_filtered": tags_filtered,
            },
            request_id=request_id,
            job_id=_uuid(job_id),
        )

    async def memory_injection_skipped(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID | str,
        run_id: uuid.UUID | str,
        request_id: str,
    ) -> None:
        self._require_process(AuditProcess.GATEWAY, AuditProcess.SCHEDULER)
        await self._service.append(
            session,
            AuditActor.trusted_process(self._process_context),
            AuditAction.MEMORY_INJECTION_SKIPPED,
            AuditTarget(
                AuditTargetKind.RUN,
                _uuid(run_id),
                _uuid(project_id),
            ),
            AuditOutcome.SUCCESS,
            {"reason": "over_budget"},
            request_id=request_id,
        )

    async def memory_dream_review_flagged(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID | str,
        job_id: uuid.UUID,
        request_id: str,
        version: int,
        deletion_ratio_bucket: str,
    ) -> None:
        self._require_process(AuditProcess.WORKER)
        await self._service.append(
            session,
            AuditActor.trusted_process(self._process_context),
            AuditAction.MEMORY_DREAM_REVIEW_FLAGGED,
            AuditTarget(
                AuditTargetKind.JOB,
                _uuid(job_id),
                _uuid(project_id),
            ),
            AuditOutcome.SUCCESS,
            {
                "version": version,
                "deletion_ratio_bucket": deletion_ratio_bucket,
            },
            request_id=request_id,
            job_id=_uuid(job_id),
        )

    async def memory_seal_admitted(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID | str,
        job_id: uuid.UUID,
        request_id: str,
    ) -> None:
        self._require_process(AuditProcess.SCHEDULER)
        await self._service.append(
            session,
            AuditActor.trusted_process(self._process_context),
            AuditAction.MEMORY_SEAL_ADMITTED,
            AuditTarget(
                AuditTargetKind.JOB,
                _uuid(job_id),
                _uuid(project_id),
            ),
            AuditOutcome.SUCCESS,
            {},
            request_id=request_id,
            job_id=_uuid(job_id),
        )

    async def memory_seal_settled(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID | str,
        job_id: uuid.UUID,
        request_id: str,
        disposition: str,
    ) -> None:
        self._require_process(AuditProcess.WORKER)
        await self._service.append(
            session,
            AuditActor.trusted_process(self._process_context),
            AuditAction.MEMORY_SEAL_SETTLED,
            AuditTarget(
                AuditTargetKind.JOB,
                _uuid(job_id),
                _uuid(project_id),
            ),
            AuditOutcome.SUCCESS,
            {"disposition": disposition},
            request_id=request_id,
            job_id=_uuid(job_id),
        )

    async def job_terminalized(
        self,
        session: AsyncSession,
        event: JobTerminalEvent,
    ) -> None:
        self._require_process(AuditProcess.WORKER)
        actor = AuditActor.trusted_process(self._process_context)
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
                request_id=event.origin_trace_id or "worker-job-terminal",
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
        if not consume_issued_dead_job_requeued_event(event) or event.request_id != self._request_id:
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
                "job_type": event.job_type,
                "attempt_count": event.attempt_count,
                "retry_safety": event.retry_safety,
            },
            request_id=self._request_id,
            job_id=event.successor_job_id,
        )


class SystemProjectLifecycleAuditSink:
    """System-admin-bound project pause/resume audit port."""

    def __init__(
        self,
        service: AuditService,
        context: SystemAuditContext,
    ) -> None:
        self._service = service
        self._actor = AuditActor.system_admin(context)
        self._request_id = context.request_id

    async def project_suspended(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
    ) -> None:
        await self._project_event(
            session,
            project_id=project_id,
            action=AuditAction.PROJECT_SUSPENDED,
        )

    async def project_resumed(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
    ) -> None:
        await self._project_event(
            session,
            project_id=project_id,
            action=AuditAction.PROJECT_RESUMED,
        )

    async def _project_event(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        action: AuditAction,
    ) -> None:
        await self._service.append(
            session,
            self._actor,
            action,
            AuditTarget(
                AuditTargetKind.PROJECT,
                _uuid(project_id),
                _uuid(project_id),
            ),
            AuditOutcome.SUCCESS,
            {},
            request_id=self._request_id,
        )


class TrustedOperationAuditSink:
    """Process-bound contract for Worker-owned retention purge."""

    def __init__(
        self,
        service: AuditService,
        *,
        process_context: AuditProcessContext,
    ) -> None:
        if type(service) is not AuditService:
            raise TypeError("trusted-operation AuditService is required")
        context = service.require_process_context(process_context)
        if context.process is not AuditProcess.WORKER:
            raise TypeError("trusted-operation AuditService is required")
        self._service = service
        self._process_context = context
        self._process = context.process

    def _require_process(self, *allowed: AuditProcess) -> None:
        if self._process not in allowed:
            raise AuditAuthorityRejected()

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
        if resource_kind not in {
            "account",
            "project",
            "file",
            "former_owner",
        }:
            raise TypeError("purge audit resource kind is invalid")
        self._require_process(AuditProcess.WORKER)
        await self._service.append(
            session,
            AuditActor.trusted_process(self._process_context),
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
    "SystemProjectLifecycleAuditSink",
    "SystemJobAuditSink",
    "TrustedOperationAuditSink",
]
