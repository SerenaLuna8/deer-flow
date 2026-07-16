from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.automations.errors import (
    AutomationConflict,
    AutomationError,
    AutomationForbidden,
    AutomationInvalid,
    AutomationNotFound,
    AutomationUnavailable,
    AutomationVersionConflict,
)
from app.automations.execution_authority import (
    automation_retry_denial,
    lock_automation_execution_authority,
)
from app.automations.occurrences import deterministic_run_id, deterministic_thread_id
from app.gateway.services import start_scheduled_private_run
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_repository import PrivateRunRecord, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRecord, ThreadAgentRef
from app.private_work.thread_service import PrivateThreadService
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext, resolve_project_context_in_transaction
from app.projects.errors import (
    ProjectDatabaseUnavailable,
    ProjectForbidden,
    ProjectNotFound,
)
from deerflow.persistence.scheduled_task_runs import (
    ScheduledTaskRunRecord,
    ScheduledTaskRunRepository,
    ScheduledTaskRunRow,
)
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskRecord,
    ScheduledTaskRepository,
    ScheduledTaskRow,
)
from deerflow.runtime import RunRecord
from deerflow.runtime.private_scope import PrivateResourceScope

_DISPATCH_REQUEST_ID = "automation-dispatch"
_RUN_ADOPTABLE_STATUSES = frozenset({"pending", "running", "success"})


@dataclass(frozen=True, slots=True)
class AutomationDispatchResult:
    occurrence_id: str
    thread_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class _DispatchCoordinates:
    occurrence_id: str
    project_id: uuid.UUID
    owner_user_id: str
    task_id: str
    trigger: str
    context_mode: str
    reuse_thread_id: str | None

    @property
    def scope(self) -> PrivateResourceScope:
        # Repository coordinates are database-derived here; the scheduler has
        # no client-issued scope. The current membership version is resolved
        # before any runtime side effect.
        return PrivateResourceScope(
            project_id=str(self.project_id),
            owner_user_id=self.owner_user_id,
            membership_version=1,
        )

    @property
    def expected_thread_id(self) -> str:
        if self.context_mode == "reuse_thread" and self.reuse_thread_id:
            return self.reuse_thread_id
        return deterministic_thread_id(self.occurrence_id)

    @property
    def expected_run_id(self) -> str:
        return deterministic_run_id(self.occurrence_id)

    @property
    def run_metadata(self) -> dict[str, object]:
        return {
            "scheduled_task_id": self.task_id,
            "scheduled_task_run_id": self.occurrence_id,
            "scheduled_trigger": self.trigger,
        }


@dataclass(frozen=True, slots=True)
class _PreparedDispatch:
    context: PrivateWorkContext
    task: ScheduledTaskRecord
    occurrence: ScheduledTaskRunRecord


async def ensure_automation_thread(
    context: PrivateWorkContext,
    task: ScheduledTaskRecord,
    occurrence: ScheduledTaskRunRecord,
    *,
    thread_service: PrivateThreadService,
) -> str:
    """Create/adopt a deterministic fresh Thread or revalidate a reuse Thread."""

    expected_agent = ThreadAgentRef(task.agent_asset_id, task.agent_scope)
    if task.context_mode == "reuse_thread":
        if not task.thread_id:
            raise AutomationConflict(context.request_id)
        existing = await thread_service.get(context, task.thread_id)
        if existing is None or not _thread_agent_matches(existing, expected_agent):
            raise AutomationConflict(context.request_id)
        return existing.thread_id

    if task.context_mode != "fresh_thread_per_run" or task.thread_id is not None:
        raise AutomationConflict(context.request_id)
    thread_id = deterministic_thread_id(occurrence.id)
    expected_metadata = {
        "scheduled_task_id": task.id,
        "scheduled_task_run_id": occurrence.id,
        "scheduled_trigger": occurrence.trigger,
    }
    existing = await thread_service.get(context, thread_id)
    if existing is None:
        try:
            existing = await thread_service.create(
                context,
                thread_id=thread_id,
                agent=expected_agent,
                display_name=task.title,
                metadata=expected_metadata,
            )
        except PrivateWorkConflict:
            # A retry or concurrent dispatcher may have committed the exact
            # deterministic Thread. Adopt only after a fresh scoped read.
            existing = await thread_service.get(context, thread_id)
    if existing is None or not _thread_agent_matches(existing, expected_agent) or existing.metadata != expected_metadata:
        raise AutomationConflict(context.request_id)
    return existing.thread_id


def _thread_agent_matches(
    thread: PrivateThreadRecord,
    expected: ThreadAgentRef,
) -> bool:
    return thread.agent_asset_id == expected.asset_id and thread.agent_scope == expected.scope


class AutomationDispatcher:
    """Materialize one claimed occurrence through the sole M4 private runtime."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        thread_service: PrivateThreadService,
        launch_private_run: Callable[..., Awaitable[RunRecord]] = start_scheduled_private_run,
        clock: Callable[[], datetime] | None = None,
        retry_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        if retry_delay <= timedelta(0):
            raise ValueError("retry_delay must be positive")
        self._session_factory = session_factory
        self._thread_service = thread_service
        self._launch_private_run = launch_private_run
        self._clock = clock or (lambda: datetime.now(UTC))
        self._retry_delay = retry_delay
        self._revalidator = PrivateWorkRevalidator()

    async def dispatch(
        self,
        occurrence_id: str,
        *,
        app: Any,
    ) -> AutomationDispatchResult:
        if not isinstance(occurrence_id, str) or not occurrence_id or len(occurrence_id) > 64:
            raise AutomationInvalid(_DISPATCH_REQUEST_ID)
        coordinates = await self._load_coordinates(occurrence_id)
        try:
            prepared = await self._prepare(coordinates)
            if prepared.occurrence.status == "running":
                return self._result_from_running(coordinates, prepared.occurrence)
            thread_id = await ensure_automation_thread(
                prepared.context,
                prepared.task,
                prepared.occurrence,
                thread_service=self._thread_service,
            )
            if thread_id != coordinates.expected_thread_id:
                raise AutomationConflict(prepared.context.request_id)

            # Thread creation has its own committed transaction. Re-resolve
            # current membership and definition state once more before M4 run
            # admission so a concurrent freeze/delete/version change cannot
            # cross that boundary on the earlier snapshot.
            prepared = await self._prepare(coordinates)

            existing = await self._get_private_run(
                prepared.context.resource_scope,
                coordinates.expected_run_id,
            )
            if existing is not None:
                self._require_matching_run(coordinates, existing)
                if existing.status not in _RUN_ADOPTABLE_STATUSES:
                    raise AutomationConflict(prepared.context.request_id)
                return await self._mark_running(
                    coordinates,
                    prepared,
                    existing,
                )

            try:
                record = await self._launch_private_run(
                    app=app,
                    context=prepared.context,
                    thread_id=thread_id,
                    run_id=coordinates.expected_run_id,
                    prompt=prepared.task.prompt,
                    metadata=coordinates.run_metadata,
                )
            except PrivateWorkConflict:
                # Another dispatcher may have crossed the same deterministic
                # precheck and won M4 admission. Adopt only that exact scoped
                # Run; unrelated admission conflicts continue to fail closed.
                raced = await self._get_private_run(
                    prepared.context.resource_scope,
                    coordinates.expected_run_id,
                )
                if raced is None:
                    raise
                self._require_matching_run(coordinates, raced)
                if raced.status not in _RUN_ADOPTABLE_STATUSES:
                    raise AutomationConflict(prepared.context.request_id) from None
                return await self._mark_running(coordinates, prepared, raced)
            if record.thread_id != thread_id or record.run_id != coordinates.expected_run_id:
                raise AutomationConflict(prepared.context.request_id)
            persisted = await self._get_private_run(
                prepared.context.resource_scope,
                coordinates.expected_run_id,
            )
            if persisted is None:
                raise AutomationUnavailable(prepared.context.request_id)
            self._require_matching_run(coordinates, persisted)
            return await self._mark_running(coordinates, prepared, persisted)
        except Exception as error:
            mapped = self._map_error(error)
            await self._settle_failure(coordinates, mapped)
            raise mapped from None

    async def _load_coordinates(
        self,
        occurrence_id: str,
    ) -> _DispatchCoordinates:
        try:
            async with self._session_factory() as session, session.begin():
                pair = (
                    await session.execute(
                        sa.select(ScheduledTaskRunRow, ScheduledTaskRow)
                        .join(
                            ScheduledTaskRow,
                            sa.and_(
                                ScheduledTaskRow.project_id == ScheduledTaskRunRow.project_id,
                                ScheduledTaskRow.owner_user_id == ScheduledTaskRunRow.owner_user_id,
                                ScheduledTaskRow.id == ScheduledTaskRunRow.task_id,
                            ),
                        )
                        .where(ScheduledTaskRunRow.id == occurrence_id)
                    )
                ).one_or_none()
        except (DBAPIError, SATimeoutError):
            raise AutomationUnavailable(_DISPATCH_REQUEST_ID) from None
        if pair is None:
            raise AutomationNotFound(_DISPATCH_REQUEST_ID)
        occurrence, task = pair
        return _DispatchCoordinates(
            occurrence_id=occurrence.id,
            project_id=occurrence.project_id,
            owner_user_id=occurrence.owner_user_id,
            task_id=task.id,
            trigger=occurrence.trigger,
            context_mode=task.context_mode,
            reuse_thread_id=task.thread_id,
        )

    async def _prepare(
        self,
        coordinates: _DispatchCoordinates,
    ) -> _PreparedDispatch:
        try:
            async with self._session_factory() as session, session.begin():
                project = await resolve_project_context_in_transaction(
                    session,
                    uuid.UUID(coordinates.owner_user_id),
                    coordinates.project_id,
                    _DISPATCH_REQUEST_ID,
                    lock=True,
                )
                if type(project) is not ProjectContext:
                    raise ProjectNotFound
                context = PrivateWorkContext.from_project(project)
                project.require(Capability.AUTOMATION_MANAGE_OWN)
                project.require(Capability.PRIVATE_WORK_CREATE)
                project.require(Capability.SHARED_ASSETS_EXECUTE)

                task = await ScheduledTaskRepository(session).lock_active(
                    context.resource_scope,
                    coordinates.task_id,
                )
                if task is None:
                    raise AutomationNotFound(context.request_id)
                occurrence_repository = ScheduledTaskRunRepository(session)
                occurrence = await occurrence_repository.get(
                    context.resource_scope,
                    coordinates.occurrence_id,
                    lock=True,
                )
                if occurrence is None or occurrence.task_id != task.id:
                    raise AutomationNotFound(context.request_id)
                if task.version != occurrence.task_version:
                    raise AutomationVersionConflict(context.request_id)
                allowed_status = task.status == "enabled" or (occurrence.trigger == "manual" and task.status == "paused")
                if not allowed_status:
                    raise AutomationConflict(context.request_id)
                if occurrence.status == "running":
                    self._result_from_running(coordinates, occurrence)
                    return _PreparedDispatch(context, task, occurrence)
                if occurrence.status != "launching" or occurrence.thread_id is not None or occurrence.run_id is not None:
                    raise AutomationConflict(context.request_id)
                resolved = await occurrence_repository.record_resolution(
                    context.resource_scope,
                    occurrence.id,
                    membership_id=project.membership_id,
                    membership_version=project.membership_version,
                    updated_at=self._now(),
                )
                if resolved is None:
                    raise AutomationConflict(context.request_id)
                return _PreparedDispatch(context, task, resolved)
        except Exception as error:
            raise self._map_error(error) from None

    async def _mark_running(
        self,
        coordinates: _DispatchCoordinates,
        prepared: _PreparedDispatch,
        run: PrivateRunRecord,
    ) -> AutomationDispatchResult:
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    prepared.context,
                    Capability.AUTOMATION_MANAGE_OWN,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                task = await ScheduledTaskRepository(session).lock_active(
                    prepared.context.resource_scope,
                    coordinates.task_id,
                )
                if task is None:
                    raise AutomationNotFound(prepared.context.request_id)
                occurrence_repository = ScheduledTaskRunRepository(session)
                occurrence = await occurrence_repository.get(
                    prepared.context.resource_scope,
                    coordinates.occurrence_id,
                    lock=True,
                )
                if occurrence is None:
                    raise AutomationNotFound(prepared.context.request_id)
                if task.version != occurrence.task_version:
                    raise AutomationVersionConflict(prepared.context.request_id)
                allowed_status = task.status == "enabled" or (occurrence.trigger == "manual" and task.status == "paused")
                if not allowed_status:
                    raise AutomationConflict(prepared.context.request_id)
                locked_run = await PrivateRunRepository(session).get(
                    scope=prepared.context.resource_scope,
                    run_id=run.run_id,
                    lock=True,
                )
                if locked_run is None:
                    raise AutomationUnavailable(prepared.context.request_id)
                self._require_matching_run(coordinates, locked_run)
                if locked_run.status not in _RUN_ADOPTABLE_STATUSES:
                    raise AutomationConflict(prepared.context.request_id)
                marked = await occurrence_repository.mark_running(
                    prepared.context.resource_scope,
                    occurrence.id,
                    thread_id=locked_run.thread_id,
                    run_id=locked_run.run_id,
                    started_at=locked_run.created_at,
                    updated_at=self._now(),
                )
                if marked is None:
                    if occurrence.status == "running":
                        return self._result_from_running(coordinates, occurrence)
                    raise AutomationConflict(prepared.context.request_id)
                return AutomationDispatchResult(
                    occurrence_id=marked.id,
                    thread_id=locked_run.thread_id,
                    run_id=locked_run.run_id,
                )
        except Exception as error:
            raise self._map_error(error) from None

    async def _get_private_run(
        self,
        scope: PrivateResourceScope,
        run_id: str,
    ) -> PrivateRunRecord | None:
        try:
            async with self._session_factory() as session, session.begin():
                return await PrivateRunRepository(session).get(
                    scope=scope,
                    run_id=run_id,
                )
        except (DBAPIError, SATimeoutError):
            raise AutomationUnavailable(_DISPATCH_REQUEST_ID) from None

    async def _settle_failure(
        self,
        coordinates: _DispatchCoordinates,
        error: AutomationError,
    ) -> None:
        now = self._now()
        try:
            async with self._session_factory() as session, session.begin():
                authority = await lock_automation_execution_authority(
                    session,
                    coordinates.scope,
                )
                tasks = ScheduledTaskRepository(session)
                task = await tasks.lock_for_automation_outcome(
                    coordinates.scope,
                    coordinates.task_id,
                )
                occurrences = ScheduledTaskRunRepository(session)
                occurrence = await occurrences.get(
                    coordinates.scope,
                    coordinates.occurrence_id,
                    lock=True,
                )
                if occurrence is None or occurrence.status != "launching":
                    return
                denial = automation_retry_denial(authority, task, occurrence)
                run = await PrivateRunRepository(session).get(
                    scope=coordinates.scope,
                    run_id=coordinates.expected_run_id,
                    lock=True,
                )
                attach_run = False
                if run is not None:
                    try:
                        self._require_matching_run(coordinates, run)
                    except AutomationConflict:
                        attach_run = False
                    else:
                        attach_run = True
                if run is None and isinstance(error, AutomationUnavailable):
                    if denial is not None:
                        await occurrences.finish(
                            coordinates.scope,
                            coordinates.occurrence_id,
                            status=denial.occurrence_status,
                            error_code=denial.error_code,
                            error_message=None,
                            finished_at=now,
                        )
                        return
                    await occurrences.requeue_launch(
                        coordinates.scope,
                        coordinates.occurrence_id,
                        next_attempt_at=now + self._retry_delay,
                        error_code=error.code,
                        updated_at=now,
                    )
                    return
                await occurrences.reject_launch(
                    coordinates.scope,
                    coordinates.occurrence_id,
                    error_code=error.code,
                    finished_at=now,
                    thread_id=run.thread_id if attach_run and run is not None else None,
                    run_id=run.run_id if attach_run and run is not None else None,
                )
        except (DBAPIError, SATimeoutError):
            raise AutomationUnavailable(error.request_id) from None

    @staticmethod
    def _require_matching_run(
        coordinates: _DispatchCoordinates,
        run: PrivateRunRecord,
    ) -> None:
        if run.thread_id != coordinates.expected_thread_id or run.run_id != coordinates.expected_run_id or run.metadata != coordinates.run_metadata:
            raise AutomationConflict(_DISPATCH_REQUEST_ID)

    @staticmethod
    def _result_from_running(
        coordinates: _DispatchCoordinates,
        occurrence: ScheduledTaskRunRecord,
    ) -> AutomationDispatchResult:
        if occurrence.thread_id != coordinates.expected_thread_id or occurrence.run_id != coordinates.expected_run_id:
            raise AutomationConflict(_DISPATCH_REQUEST_ID)
        return AutomationDispatchResult(
            occurrence_id=occurrence.id,
            thread_id=occurrence.thread_id,
            run_id=occurrence.run_id,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise AutomationUnavailable(_DISPATCH_REQUEST_ID)
        return value.astimezone(UTC)

    @staticmethod
    def _map_error(error: Exception) -> AutomationError:
        if isinstance(error, AutomationError):
            return error
        if isinstance(error, (ProjectNotFound, PrivateWorkNotFound)):
            return AutomationNotFound(_DISPATCH_REQUEST_ID)
        if isinstance(error, (ProjectForbidden, PrivateWorkForbidden)):
            return AutomationForbidden(_DISPATCH_REQUEST_ID)
        if isinstance(error, (PrivateWorkConflict, PrivateWorkAssetStale)):
            return AutomationConflict(_DISPATCH_REQUEST_ID)
        if isinstance(
            error,
            (
                ProjectDatabaseUnavailable,
                PrivateWorkUnavailable,
                DBAPIError,
                SATimeoutError,
            ),
        ):
            return AutomationUnavailable(_DISPATCH_REQUEST_ID)
        if isinstance(error, PrivateWorkError):
            return AutomationUnavailable(_DISPATCH_REQUEST_ID)
        raise error


__all__ = [
    "AutomationDispatchResult",
    "AutomationDispatcher",
    "ensure_automation_thread",
]
