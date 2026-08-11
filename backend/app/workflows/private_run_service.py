"""Owner-private Workflow Run read/cancel/retry boundary.

No route is registered here.  G14 supplies the capability mapping; later Run
API gates call this service.  There is intentionally no Run-list operation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.errors import PrivateWorkNotFound
from app.workflows.authorization import (
    WorkflowAction,
    WorkflowAuthorizationService,
)
from app.workflows.errors import (
    WorkflowError,
    WorkflowNotFound,
    WorkflowRunConflict,
    WorkflowRunRetryForbidden,
    WorkflowUnavailable,
)
from app.workflows.repository import (
    WorkflowAuthorityMissing,
    WorkflowManualRetryForbidden,
    WorkflowPersistenceError,
    WorkflowRepository,
    WorkflowRetrySource,
    WorkflowRunEventAppend,
    WorkflowRunRecord,
    WorkflowRunScope,
    WorkflowRunStateConflict,
)
from deerflow.persistence.jobs.sql import JobRepository, JobScope


@dataclass(frozen=True, slots=True)
class WorkflowCancelResult:
    run: WorkflowRunRecord
    cancel_requested: bool
    settled: bool


class WorkflowPrivateRunService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: WorkflowAuthorizationService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization or WorkflowAuthorizationService()

    @staticmethod
    def _issued(context: PrivateWorkContext) -> PrivateWorkContext:
        try:
            return require_issued_private_work_context(context)
        except PrivateWorkNotFound:
            raise WorkflowNotFound("unknown") from None

    @staticmethod
    def _run_id(value: object, request_id: str) -> uuid.UUID:
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError):
            raise WorkflowNotFound(request_id) from None

    @staticmethod
    def _scope(context: PrivateWorkContext) -> WorkflowRunScope:
        return WorkflowRunScope(
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
        )

    @staticmethod
    def _raise_mapped(error: Exception, request_id: str) -> None:
        if isinstance(error, WorkflowError):
            raise error
        if isinstance(error, WorkflowAuthorityMissing):
            raise WorkflowNotFound(request_id) from None
        if isinstance(error, WorkflowManualRetryForbidden):
            raise WorkflowRunRetryForbidden(request_id) from None
        if isinstance(error, WorkflowRunStateConflict):
            raise WorkflowRunConflict(request_id) from None
        if isinstance(error, DBAPIError):
            raise WorkflowUnavailable(request_id) from None
        if isinstance(error, WorkflowPersistenceError):
            raise WorkflowRunConflict(request_id) from None
        raise error

    async def get(
        self,
        context: PrivateWorkContext,
        run_id: uuid.UUID,
    ) -> WorkflowRunRecord:
        context = self._issued(context)
        run_id = self._run_id(run_id, context.request_id)
        try:
            async with self._session_factory() as session, session.begin():
                await self._authorization.require(
                    session,
                    context,
                    WorkflowAction.RUN_READ_OWN,
                    lock=False,
                )
                record = await WorkflowRepository(session).get_run(
                    self._scope(context),
                    run_id,
                )
                if record is None:
                    raise WorkflowNotFound(context.request_id)
                return record
        except Exception as error:
            self._raise_mapped(error, context.request_id)
            raise AssertionError("unreachable")

    async def cancel(
        self,
        context: PrivateWorkContext,
        run_id: uuid.UUID,
    ) -> WorkflowCancelResult:
        context = self._issued(context)
        run_id = self._run_id(run_id, context.request_id)
        scope = self._scope(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._authorization.require(
                    session,
                    context,
                    WorkflowAction.RUN_CANCEL_OWN,
                    lock=True,
                )
                repository = WorkflowRepository(session)
                run = await repository.get_run(scope, run_id, lock=True)
                if run is None:
                    raise WorkflowNotFound(context.request_id)
                if run.status == "cancelled":
                    return WorkflowCancelResult(
                        run=run,
                        cancel_requested=True,
                        settled=True,
                    )
                if run.status not in {"queued", "running"} or run.current_job_id is None:
                    raise WorkflowRunConflict(context.request_id)
                jobs = JobRepository(session)
                job_scope = JobScope(scope.project_id, scope.owner_user_id)
                requested = await jobs.request_cancel(
                    job_scope,
                    run.current_job_id,
                    reason="WORKFLOW_CANCEL_REQUESTED",
                )
                if not requested:
                    raise WorkflowRunConflict(context.request_id)
                if run.status == "running":
                    return WorkflowCancelResult(
                        run=run,
                        cancel_requested=True,
                        settled=False,
                    )
                settled = await jobs.settle_requested_cancel(
                    job_scope,
                    run.current_job_id,
                )
                if not settled:
                    # A concurrent claim can make queued cancellation
                    # cooperative.  The Run remains active and exact-scoped.
                    current = await repository.get_run(scope, run_id)
                    if current is None:
                        raise WorkflowNotFound(context.request_id)
                    return WorkflowCancelResult(
                        run=current,
                        cancel_requested=True,
                        settled=False,
                    )
                cancelled = await repository.settle_queued_cancel(scope, run_id)
                await repository.append_control_event(
                    scope,
                    run_id,
                    WorkflowRunEventAppend(
                        event_type="workflow.run.cancelled",
                        payload={},
                    ),
                )
                return WorkflowCancelResult(
                    run=cancelled,
                    cancel_requested=True,
                    settled=True,
                )
        except Exception as error:
            self._raise_mapped(error, context.request_id)
            raise AssertionError("unreachable")

    async def prepare_retry(
        self,
        context: PrivateWorkContext,
        run_id: uuid.UUID,
    ) -> WorkflowRetrySource:
        """Return immutable source authority; G30 admits the new Run."""

        context = self._issued(context)
        run_id = self._run_id(run_id, context.request_id)
        try:
            async with self._session_factory() as session, session.begin():
                await self._authorization.require(
                    session,
                    context,
                    WorkflowAction.RETRY,
                    lock=True,
                )
                return await WorkflowRepository(session).prepare_manual_retry(
                    self._scope(context),
                    run_id,
                )
        except Exception as error:
            self._raise_mapped(error, context.request_id)
            raise AssertionError("unreachable")


__all__ = ["WorkflowCancelResult", "WorkflowPrivateRunService"]
