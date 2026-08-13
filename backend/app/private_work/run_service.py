from __future__ import annotations

import uuid
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
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_repository import (
    PrivateRunConflict,
    PrivateRunRecord,
    PrivateRunRepository,
)
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability
from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.private_work.model import (
    PrivateArtifactRow,
    RunAssetVersionRow,
    RunMcpGrantSnapshotRow,
    RunSkillCredentialSnapshotRow,
)
from deerflow.persistence.shared_assets import SkillDesignOperationRow
from deerflow.runtime.private_scope import PrivateResourceScope

TERMINAL_PRIVATE_RUN_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})


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
    ) -> None:
        self._session_factory = session_factory
        self._revalidator = PrivateWorkRevalidator()
        self._automation_reconciler = AutomationReconciler(session_factory)
        self._quota = quota or _NoopPrivateRunQuota()
        self._audit = audit or _NoopPrivateRunAudit()

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
            # but their owner may consume the same durable Run/SSE substrate
            # through a server-issued exact thread/run link.
            thread = await PrivateThreadRepository(session).get(
                scope=context.resource_scope,
                thread_id=thread_id,
                lock=lock,
                thread_kind="skill_builder",
            )
        if thread is None:
            raise PrivateWorkNotFound(context.request_id)

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

    async def delete(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        run_id: str,
    ) -> None:
        context = require_issued_private_work_context(context)
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
                record = await repository.get(
                    scope=context.resource_scope,
                    run_id=run_id,
                    lock=True,
                )
                if record is None or record.thread_id != thread_id:
                    raise PrivateWorkNotFound(context.request_id)
                if record.status not in TERMINAL_PRIVATE_RUN_STATUSES:
                    raise PrivateWorkConflict(context.request_id)
                # Builder idempotency records outlive deletable Run telemetry.
                # Clear only the scoped optional link; the durable operation
                # outcome and result revision remain available for replay.
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
                    RunSkillCredentialSnapshotRow,
                    RunMcpGrantSnapshotRow,
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
        except PrivateWorkError:
            raise
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
                cancel_result = await repository.request_cancel(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    run_id=run_id,
                    job_id=record.job_id,
                    reason=reason,
                )
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
