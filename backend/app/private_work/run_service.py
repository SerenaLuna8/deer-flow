from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.automations.errors import AutomationError
from app.automations.reconciliation import AutomationReconciler
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.execution_approval_audit import (
    HostExecutionApprovalAuditPort,
    NoopHostExecutionApprovalAudit,
)
from app.private_work.execution_approval_lifecycle import (
    ApprovalRunDependency,
    ExecutionApprovalPrivateLifecycleConflict,
    LockedExecutionApprovalRows,
    cancel_locked_execution_approval_continuation,
    lock_execution_approval_private_rows,
    reconcile_locked_execution_approval,
    reject_sealed_staged_approval_terminalization,
)
from app.private_work.output_delivery_obligation import (
    OutputDeliveryObligationConflict,
    settle_continuation_output_delivery,
    transition_output_delivery_obligation_for_approval_terminal,
)
from app.private_work.retention_authority import (
    RetentionPurgeAuthority,
    RetentionPurgeAuthorityConflict,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_repository import (
    PrivateRunConflict,
    PrivateRunRecord,
    PrivateRunRepository,
)
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability
from app.shared_assets.skill_deletion import ArchivedSkillPurger
from deerflow.persistence.execution_approvals import (
    EXECUTION_APPROVAL_ACTIVE_STATUSES,
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)
from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.private_work.model import (
    PrivateArtifactRow,
    RunAssetVersionRow,
    RunMcpSecretSnapshotRow,
    RunSkillSecretSnapshotRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.shared_assets import SkillDesignOperationRow
from deerflow.runtime.private_scope import PrivateResourceScope

TERMINAL_PRIVATE_RUN_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})
_RUN_RETENTION_NAMESPACE = uuid.UUID("b4ab3129-e23d-4ee6-b7db-71f87754e65f")
logger = logging.getLogger(__name__)


class PrivateRunQuotaPort(Protocol):
    async def release_concurrent_run(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        request_id: str,
    ) -> None: ...


class PrivateRunAuditPort(Protocol):
    async def run_cancel_requested(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        run_id: str,
        job_id: uuid.UUID,
    ) -> None: ...

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
    ) -> None: ...


class _NoopPrivateRunQuota:
    async def release_concurrent_run(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        request_id: str,
    ) -> None:
        del session, scope, run_id, request_id


class _NoopPrivateRunAudit:
    async def run_cancel_requested(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        run_id: str,
        job_id: uuid.UUID,
    ) -> None:
        del session, context, run_id, job_id

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
        del session, scope, run_id, job_id, job_type, status, public_error_code, request_id


class PrivateRunService:
    """Scoped read/delete boundary for project-owned runs."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        quota: PrivateRunQuotaPort | None = None,
        audit: PrivateRunAuditPort | None = None,
        approval_audit: HostExecutionApprovalAuditPort | None = None,
        archived_skill_purger: ArchivedSkillPurger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._revalidator = PrivateWorkRevalidator()
        self._automation_reconciler = AutomationReconciler(session_factory)
        self._quota = quota or _NoopPrivateRunQuota()
        self._audit = audit or _NoopPrivateRunAudit()
        self._approval_audit = approval_audit or NoopHostExecutionApprovalAudit()
        self._archived_skill_purger = archived_skill_purger

    @staticmethod
    async def _require_thread(
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        lock: bool = False,
    ) -> None:
        thread = await PrivateThreadRepository(session).get(
            scope=context.resource_scope,
            thread_id=thread_id,
            lock=lock,
        )
        if thread is None:
            # Hidden Builder threads are never returned by the chat directory,
            # but internal Builder orchestration still consumes the same
            # durable Run/Event substrate. Browser routes must pass through
            # require_browser_chat_thread before calling this fallback.
            thread = await PrivateThreadRepository(session).get(
                scope=context.resource_scope,
                thread_id=thread_id,
                lock=lock,
                thread_kind="skill_builder",
            )
        if thread is None:
            raise PrivateWorkNotFound(context.request_id)

    async def require_browser_chat_thread(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> None:
        """Authorize generic browser Run access for a visible chat thread only.

        Hidden Skill Builder threads keep using the internal Run/Event substrate,
        but their browser projection is the Skill Design Activity API rather than
        the generic private-work Run feeds.
        """

        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                thread = await PrivateThreadRepository(session).get(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                )
                if thread is None:
                    raise PrivateWorkNotFound(context.request_id)
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except PrivateRunConflict:
            raise PrivateWorkConflict(context.request_id) from None

    async def list(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[PrivateRunRecord, ...]:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                await self._require_thread(session, context, thread_id)
                return await PrivateRunRepository(session).list_by_thread(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    limit=limit,
                    offset=offset,
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except PrivateRunConflict:
            raise PrivateWorkConflict(context.request_id) from None

    async def get(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        run_id: str,
    ) -> PrivateRunRecord:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                await self._require_thread(session, context, thread_id)
                record = await PrivateRunRepository(session).get(
                    scope=context.resource_scope,
                    run_id=run_id,
                )
                if record is None or record.thread_id != thread_id:
                    raise PrivateWorkNotFound(context.request_id)
                return record
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except PrivateRunConflict:
            raise PrivateWorkConflict(context.request_id) from None

    async def get_many(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        run_ids: set[str],
    ) -> dict[str, PrivateRunRecord]:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                await self._require_thread(session, context, thread_id)
                return await PrivateRunRepository(session).get_many_by_thread(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    run_ids=run_ids,
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except PrivateRunConflict:
            raise PrivateWorkConflict(context.request_id) from None

    async def _cancel_execution_approval(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        row: ExecutionApprovalRequestRow,
        *,
        now: datetime,
    ) -> None:
        if row.status not in EXECUTION_APPROVAL_ACTIVE_STATUSES:
            return
        try:
            await reject_sealed_staged_approval_terminalization(
                session,
                row,
            )
            await transition_output_delivery_obligation_for_approval_terminal(
                session,
                approval=row,
                approval_status="cancelled",
                now=now,
            )
        except (
            ExecutionApprovalPrivateLifecycleConflict,
            OutputDeliveryObligationConflict,
        ):
            raise PrivateRunConflict from None
        row.status = "cancelled"
        row.version += 1
        row.terminal_at = now
        row.updated_at = now
        await self._approval_audit.host_execution_approval_terminal(
            session,
            project_id=row.project_id,
            source_run_id=row.source_run_id,
            status="cancelled",
            request_id=context.request_id,
            occurred_at=now,
        )

    async def _cancel_linked_approval_run(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        locked: LockedExecutionApprovalRows,
        row: ExecutionApprovalRequestRow,
        *,
        now: datetime,
    ) -> tuple[bool, str | None]:
        """Stop a linked Run before erasing its replay authority.

        Returns ``(wait_required, synchronously_terminal_run_id)``. A leased
        continuation receives a durable cancellation request, but the approval
        link remains until the Worker has actually settled that Run.
        """

        if row.continuation_job_id is None or row.continuation_run_id is None:
            return False, None
        job = locked.jobs.get(row.continuation_job_id)
        if job is None:
            raise PrivateRunConflict
        try:
            cancel_result = cancel_locked_execution_approval_continuation(
                row,
                locked,
                now=now,
                reason="approval_run_deleted",
            )
        except ExecutionApprovalPrivateLifecycleConflict:
            raise PrivateRunConflict from None
        if cancel_result == "requested":
            await self._audit.run_cancel_requested(
                session,
                context,
                run_id=row.continuation_run_id,
                job_id=row.continuation_job_id,
            )
        if cancel_result in {"cancelled", "terminal"}:
            await self._quota.release_concurrent_run(
                session,
                context.resource_scope,
                run_id=row.continuation_run_id,
                request_id=context.request_id,
            )
        if cancel_result == "cancelled":
            await self._audit.run_terminal(
                session,
                context.resource_scope,
                run_id=row.continuation_run_id,
                job_id=row.continuation_job_id,
                job_type=job.job_type,
                status="interrupted",
                public_error_code=None,
                request_id=context.request_id,
            )
            return False, row.continuation_run_id
        return cancel_result == "requested", None

    async def delete(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        run_id: str,
    ) -> None:
        context = require_issued_private_work_context(context)
        conflict_after_commit = False
        run_deleted = False
        synchronously_cancelled_runs: set[str] = set()
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                    lock=True,
                )
                await self._require_thread(
                    session,
                    context,
                    thread_id,
                    lock=True,
                )
                repository = PrivateRunRepository(session)
                # Discover target coordinates without authority. The shared
                # helper then locks Job -> Run -> approval, matching Worker
                # claim/completion before this method mutates private data.
                record = await repository.get(
                    scope=context.resource_scope,
                    run_id=run_id,
                )
                if record is None or record.thread_id != thread_id:
                    raise PrivateWorkNotFound(context.request_id)
                if record.status not in TERMINAL_PRIVATE_RUN_STATUSES:
                    raise PrivateWorkConflict(context.request_id)

                try:
                    locked_approvals = await lock_execution_approval_private_rows(
                        session,
                        project_id=context.project_id,
                        owner_user_id=str(context.user_id),
                        thread_id=thread_id,
                        related_run_id=run_id,
                        extra_run_dependencies=(
                            ApprovalRunDependency(
                                owner_user_id=str(context.user_id),
                                thread_id=thread_id,
                                run_id=run_id,
                                job_id=record.job_id,
                            ),
                        ),
                    )
                except ExecutionApprovalPrivateLifecycleConflict:
                    raise PrivateRunConflict from None
                approval_rows = locked_approvals.rows
                approval_now = await session.scalar(
                    sa.select(sa.func.clock_timestamp()),
                )
                if not isinstance(approval_now, datetime) or approval_now.tzinfo is None:
                    raise PrivateRunConflict
                approval_now = approval_now.astimezone(UTC)
                for approval in approval_rows:
                    if approval.status in EXECUTION_APPROVAL_ACTIVE_STATUSES:
                        await reconcile_locked_execution_approval(
                            session,
                            approval,
                            now=approval_now,
                            audit=self._approval_audit,
                        )

                # A live claim may already have launched the host process. No
                # deletion can revoke that side effect, so retain its receipt
                # authority and let the caller retry after lease convergence.
                conflict_after_commit = any(
                    approval.status == "claimed"
                    or approval_now
                    < locked_approvals.claimed_absolute_deadlines.get(
                        approval.id,
                        approval_now,
                    )
                    for approval in approval_rows
                )
                if not conflict_after_commit:
                    for approval in approval_rows:
                        wait_required, cancelled_run_id = await self._cancel_linked_approval_run(
                            session,
                            context,
                            locked_approvals,
                            approval,
                            now=approval_now,
                        )
                        if cancelled_run_id is not None:
                            synchronously_cancelled_runs.add(cancelled_run_id)
                        if approval.status in {
                            "staged",
                            "pending",
                            "approved",
                        }:
                            await self._cancel_execution_approval(
                                session,
                                context,
                                approval,
                                now=approval_now,
                            )
                        conflict_after_commit = conflict_after_commit or wait_required

                if not conflict_after_commit:
                    locked_target = (
                        await session.execute(
                            sa.select(RunRow.thread_id, RunRow.status).where(
                                RunRow.project_id == context.project_id,
                                RunRow.owner_user_id == str(context.user_id),
                                RunRow.run_id == run_id,
                            )
                        )
                    ).one_or_none()
                    if locked_target is None or locked_target.thread_id != thread_id:
                        raise PrivateWorkNotFound(context.request_id)
                    if locked_target.status not in TERMINAL_PRIVATE_RUN_STATUSES:
                        raise PrivateRunConflict
                    try:
                        await RetentionPurgeAuthority.issue_single_run(
                            session,
                            purge_id=uuid.uuid5(
                                _RUN_RETENTION_NAMESPACE,
                                ":".join(
                                    (
                                        str(context.project_id),
                                        str(context.user_id),
                                        thread_id,
                                        run_id,
                                        context.request_id,
                                    )
                                ),
                            ),
                            project_id=context.project_id,
                            owner_user_id=str(context.user_id),
                            thread_id=thread_id,
                            run_id=run_id,
                            now=approval_now,
                        )
                    except RetentionPurgeAuthorityConflict:
                        raise PrivateRunConflict from None

                    approval_ids = tuple(row.id for row in approval_rows)
                    if approval_ids:
                        await session.flush()
                        await session.execute(
                            sa.delete(ExecutionApprovalResultReceiptRow).where(
                                ExecutionApprovalResultReceiptRow.project_id == context.project_id,
                                ExecutionApprovalResultReceiptRow.owner_user_id == str(context.user_id),
                                ExecutionApprovalResultReceiptRow.thread_id == thread_id,
                                ExecutionApprovalResultReceiptRow.approval_id.in_(
                                    approval_ids,
                                ),
                            )
                        )
                        await session.execute(
                            sa.delete(ExecutionApprovalRequestRow).where(
                                ExecutionApprovalRequestRow.project_id == context.project_id,
                                ExecutionApprovalRequestRow.owner_user_id == str(context.user_id),
                                ExecutionApprovalRequestRow.thread_id == thread_id,
                                ExecutionApprovalRequestRow.id.in_(approval_ids),
                                sa.or_(
                                    ExecutionApprovalRequestRow.source_run_id == run_id,
                                    ExecutionApprovalRequestRow.continuation_run_id == run_id,
                                ),
                            )
                        )

                    # Builder idempotency outcomes remain durable, while their
                    # optional private telemetry link is cleared.
                    await session.execute(
                        sa.update(SkillDesignOperationRow)
                        .where(
                            SkillDesignOperationRow.project_id == context.project_id,
                            SkillDesignOperationRow.owner_user_id == str(context.user_id),
                            SkillDesignOperationRow.run_id == run_id,
                        )
                        .values(run_id=None)
                    )
                    await session.execute(
                        sa.delete(RunEventRow).where(
                            RunEventRow.project_id == context.project_id,
                            RunEventRow.owner_user_id == str(context.user_id),
                            RunEventRow.thread_id == thread_id,
                            RunEventRow.run_id == run_id,
                        )
                    )
                    await session.execute(
                        sa.delete(FeedbackRow).where(
                            FeedbackRow.project_id == context.project_id,
                            FeedbackRow.owner_user_id == str(context.user_id),
                            FeedbackRow.thread_id == thread_id,
                            FeedbackRow.run_id == run_id,
                        )
                    )
                    await session.execute(
                        sa.delete(PrivateArtifactRow).where(
                            PrivateArtifactRow.project_id == context.project_id,
                            PrivateArtifactRow.owner_user_id == str(context.user_id),
                            PrivateArtifactRow.thread_id == thread_id,
                            PrivateArtifactRow.run_id == run_id,
                        )
                    )
                    for snapshot_type in (
                        RunSkillSecretSnapshotRow,
                        RunMcpSecretSnapshotRow,
                        RunAssetVersionRow,
                    ):
                        await session.execute(
                            sa.delete(snapshot_type).where(
                                snapshot_type.project_id == context.project_id,
                                snapshot_type.owner_user_id == str(context.user_id),
                                snapshot_type.thread_id == thread_id,
                                snapshot_type.run_id == run_id,
                            )
                        )
                    if not await repository.delete(
                        scope=context.resource_scope,
                        run_id=run_id,
                    ):
                        raise PrivateWorkNotFound(context.request_id)
                    run_deleted = True
            for cancelled_run_id in synchronously_cancelled_runs:
                await self._automation_reconciler.handle_run_completion(
                    SimpleNamespace(run_id=cancelled_run_id),
                )
            if conflict_after_commit:
                raise PrivateWorkConflict(context.request_id)
            if run_deleted and self._archived_skill_purger is not None:
                try:
                    await self._archived_skill_purger.purge_project(
                        context.project_id,
                        request_id=context.request_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Run deletion is already committed. The low-frequency
                    # reconciler owns retry and no private coordinates are logged.
                    logger.warning("Archived Skill purge deferred after Run deletion")
        except PrivateWorkError:
            raise
        except AutomationError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except PrivateRunConflict:
            raise PrivateWorkConflict(context.request_id) from None

    async def cancel(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        run_id: str,
        *,
        reason: str = "user_requested",
    ) -> None:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    lock=True,
                )
                await self._require_thread(
                    session,
                    context,
                    thread_id,
                    lock=True,
                )
                repository = PrivateRunRepository(session)
                record = await repository.get(
                    scope=context.resource_scope,
                    run_id=run_id,
                )
                if record is None or record.thread_id != thread_id:
                    raise PrivateWorkNotFound(context.request_id)
                if record.job_id is None:
                    raise PrivateWorkConflict(context.request_id)

                # Freeze every approval dependency behind the target Job/Run
                # before requesting cancellation.  This keeps public cancel on
                # the same Job -> Run -> approval -> obligation lock order as
                # Worker claim/completion, so an approved continuation cannot
                # launch between the Run cancellation and approval convergence.
                try:
                    locked_approvals = await lock_execution_approval_private_rows(
                        session,
                        project_id=context.project_id,
                        owner_user_id=str(context.user_id),
                        thread_id=thread_id,
                        related_run_id=run_id,
                        extra_run_dependencies=(
                            ApprovalRunDependency(
                                owner_user_id=str(context.user_id),
                                thread_id=thread_id,
                                run_id=run_id,
                                job_id=record.job_id,
                            ),
                        ),
                    )
                except ExecutionApprovalPrivateLifecycleConflict:
                    raise PrivateRunConflict from None
                cancelled_at = await session.scalar(
                    sa.select(sa.func.clock_timestamp()),
                )
                if not isinstance(cancelled_at, datetime) or cancelled_at.tzinfo is None:
                    raise PrivateRunConflict
                cancelled_at = cancelled_at.astimezone(UTC)
                cancel_result = await repository.request_cancel(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    run_id=run_id,
                    job_id=record.job_id,
                    reason=reason,
                    now=cancelled_at,
                )
                for approval in locked_approvals.rows:
                    if approval.status in {"staged", "pending", "approved"}:
                        await self._cancel_execution_approval(
                            session,
                            context,
                            approval,
                            now=cancelled_at,
                        )
                        continue
                    if approval.status in {"finished", "launch_failed"} and approval.continuation_run_id == run_id and approval.continuation_job_id == record.job_id:
                        try:
                            await settle_continuation_output_delivery(
                                session,
                                approval_id_value=str(approval.id),
                                project_id=context.project_id,
                                owner_user_id=str(context.user_id),
                                thread_id=thread_id,
                                continuation_run_id=run_id,
                                continuation_job_id=record.job_id,
                                settled_status="interrupted",
                                now=cancelled_at,
                            )
                        except OutputDeliveryObligationConflict:
                            raise PrivateRunConflict from None
                if cancel_result != "terminal":
                    await self._audit.run_cancel_requested(
                        session,
                        context,
                        run_id=run_id,
                        job_id=record.job_id,
                    )
                if cancel_result in {"cancelled", "terminal"}:
                    await self._quota.release_concurrent_run(
                        session,
                        context.resource_scope,
                        run_id=run_id,
                        request_id=context.request_id,
                    )
                if cancel_result == "cancelled":
                    job_type = await session.scalar(
                        sa.select(JobRow.job_type).where(
                            JobRow.id == record.job_id,
                            JobRow.project_id == context.project_id,
                            JobRow.owner_user_id == str(context.user_id),
                        )
                    )
                    if job_type not in {"private_run", "automation_run"}:
                        raise PrivateRunConflict
                    await self._audit.run_terminal(
                        session,
                        context.resource_scope,
                        run_id=run_id,
                        job_id=record.job_id,
                        job_type=job_type,
                        status="interrupted",
                        public_error_code=None,
                        request_id=context.request_id,
                    )
            if cancel_result in {"cancelled", "terminal"}:
                await self._automation_reconciler.handle_run_completion(
                    SimpleNamespace(run_id=run_id),
                )
        except PrivateWorkError:
            raise
        except AutomationError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except PrivateRunConflict:
            raise PrivateWorkConflict(context.request_id) from None


__all__ = ["PrivateRunService", "TERMINAL_PRIVATE_RUN_STATUSES"]
