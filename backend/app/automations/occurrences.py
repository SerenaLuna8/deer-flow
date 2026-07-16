from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.automations.errors import (
    AutomationActiveRun,
    AutomationConcurrencyLimit,
    AutomationConflict,
    AutomationError,
    AutomationForbidden,
    AutomationInvalid,
    AutomationNotFound,
    AutomationUnavailable,
)
from app.automations.models import AutomationRunView
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.errors import (
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.projects.capabilities import Capability
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.scheduled_task_runs import (
    ACTIVE_OCCURRENCE_STATUSES,
    TERMINAL_OCCURRENCE_STATUSES,
    ScheduledTaskRunCreate,
    ScheduledTaskRunRecord,
    ScheduledTaskRunRepository,
    ScheduledTaskRunRow,
)
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskRepository,
    ScheduledTaskRow,
)
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.scheduler.schedules import next_scheduled_occurrence

_AUTOMATION_ADMISSION_LOCK = 0x0DEE_12F1_0A55_0005
FRESH_THREAD_NAMESPACE = uuid.UUID("8bc2f65e-f186-5fb2-a480-7f23125f8005")
RUN_NAMESPACE = uuid.UUID("a58150d1-9869-55b1-8cbe-cd30e6edba05")


@dataclass(frozen=True, slots=True)
class ManualReservation:
    occurrence: ScheduledTaskRunRecord
    created: bool


@dataclass(frozen=True, slots=True)
class ManualDispatchClaim:
    occurrence: ScheduledTaskRunRecord
    claimed: bool


def scheduled_occurrence_key(task_id: str, scheduled_for: datetime) -> str:
    if not isinstance(task_id, str) or not task_id or not isinstance(scheduled_for, datetime) or scheduled_for.tzinfo is None:
        raise ValueError("task_id and timezone-aware scheduled_for are required")
    canonical = f"scheduled:{task_id}:{scheduled_for.astimezone(UTC).isoformat()}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def hash_manual_idempotency(value: uuid.UUID) -> str:
    if type(value) is not uuid.UUID:
        raise ValueError("UUID idempotency key is required")
    return hashlib.sha256(str(value).encode("ascii")).hexdigest()


def manual_occurrence_key(task_id: str, idempotency_hash: str) -> str:
    if not isinstance(task_id, str) or not task_id or not isinstance(idempotency_hash, str) or len(idempotency_hash) != 64:
        raise ValueError("task_id and SHA-256 idempotency hash are required")
    return hashlib.sha256(f"manual:{task_id}:{idempotency_hash}".encode()).hexdigest()


def deterministic_thread_id(occurrence_id: str) -> str:
    return str(uuid.uuid5(FRESH_THREAD_NAMESPACE, occurrence_id))


def deterministic_run_id(occurrence_id: str) -> str:
    return str(uuid.uuid5(RUN_NAMESPACE, occurrence_id))


class AutomationOccurrenceService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_concurrent_runs: int,
    ) -> None:
        if type(max_concurrent_runs) is not int or max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be positive")
        self._session_factory = session_factory
        self._max_concurrent_runs = max_concurrent_runs
        self._revalidator = PrivateWorkRevalidator()

    async def reserve_due(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[ScheduledTaskRunRecord, ...]:
        now = self._validated_now(now)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        try:
            async with self._session_factory() as session, session.begin():
                await self._acquire_admission_lock(session)
                active_count = await self._active_count(session)
                budget = max(0, self._max_concurrent_runs - active_count)
                candidates = (
                    await session.execute(
                        sa.select(ScheduledTaskRow, ProjectMembershipRow.version)
                        .join(
                            ProjectMembershipRow,
                            sa.and_(
                                ProjectMembershipRow.project_id == ScheduledTaskRow.project_id,
                                ProjectMembershipRow.user_id == ScheduledTaskRow.owner_user_id,
                            ),
                        )
                        .join(ProjectRow, ProjectRow.id == ScheduledTaskRow.project_id)
                        .where(
                            ScheduledTaskRow.status == "enabled",
                            ScheduledTaskRow.frozen_at.is_(None),
                            ScheduledTaskRow.deleted_at.is_(None),
                            ScheduledTaskRow.next_run_at.is_not(None),
                            ScheduledTaskRow.next_run_at <= now,
                            ProjectMembershipRow.status == "active",
                            ProjectMembershipRow.role.in_(("admin", "editor", "runner")),
                            ProjectRow.status == "active",
                            ProjectRow.is_suspended.is_(False),
                        )
                        .order_by(ScheduledTaskRow.next_run_at, ScheduledTaskRow.id)
                        .limit(limit)
                        .with_for_update(of=ScheduledTaskRow, skip_locked=True)
                    )
                ).all()

                reserved: list[ScheduledTaskRunRecord] = []
                for task_row, membership_version in candidates:
                    due_at = task_row.next_run_at
                    if due_at is None:
                        continue
                    scope = self._scope(task_row, membership_version)
                    occurrences = ScheduledTaskRunRepository(session)
                    overlapping = await occurrences.has_active(scope, task_row.id)
                    if not overlapping and budget == 0:
                        continue

                    next_run_at = self._next_run(task_row, now)
                    if overlapping:
                        request = ScheduledTaskRunCreate(
                            occurrence_id=str(uuid.uuid4()),
                            task_id=task_row.id,
                            task_version=task_row.version,
                            occurrence_key=scheduled_occurrence_key(task_row.id, due_at),
                            manual_idempotency_hash=None,
                            scheduled_for=due_at,
                            trigger="scheduled",
                            status="skipped",
                            error_code="AUTOMATION_OVERLAP_SKIPPED",
                            finished_at=now,
                            created_at=now,
                        )
                    else:
                        request = ScheduledTaskRunCreate(
                            occurrence_id=str(uuid.uuid4()),
                            task_id=task_row.id,
                            task_version=task_row.version,
                            occurrence_key=scheduled_occurrence_key(task_row.id, due_at),
                            manual_idempotency_hash=None,
                            scheduled_for=due_at,
                            trigger="scheduled",
                            status="queued",
                            created_at=now,
                        )
                        budget -= 1

                    occurrence = await occurrences.create(scope, request)
                    advanced = await ScheduledTaskRepository(session).advance_after_reservation(
                        scope,
                        task_row.id,
                        expected_next_run_at=due_at,
                        next_run_at=next_run_at,
                        updated_at=now,
                    )
                    if advanced is None:
                        raise AutomationUnavailable("scheduler")
                    reserved.append(occurrence)
                return tuple(reserved)
        except Exception as error:
            self._raise_mapped(error, "scheduler")

    async def reserve_manual(
        self,
        context: PrivateWorkContext,
        task_id: str,
        idempotency_key: uuid.UUID,
        *,
        now: datetime,
    ) -> ManualReservation:
        context = self._issued_context(context)
        now = self._validated_now(now, context.request_id)
        if not isinstance(task_id, str) or not task_id or len(task_id) > 64 or type(idempotency_key) is not uuid.UUID:
            raise AutomationInvalid(context.request_id)
        idempotency_hash = hash_manual_idempotency(idempotency_key)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.AUTOMATION_MANAGE_OWN,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                await self._acquire_admission_lock(session)
                occurrences = ScheduledTaskRunRepository(session)
                existing = await occurrences.get_by_manual_idempotency(
                    context.resource_scope,
                    task_id,
                    idempotency_hash,
                )
                if existing is not None:
                    return ManualReservation(existing, False)

                task = await ScheduledTaskRepository(session).lock_active(
                    context.resource_scope,
                    task_id,
                )
                if task is None or task.status not in {"enabled", "paused"}:
                    raise AutomationNotFound(context.request_id)

                if await occurrences.has_active(context.resource_scope, task.id):
                    raise AutomationActiveRun(context.request_id)
                if await self._active_count(session) >= self._max_concurrent_runs:
                    raise AutomationConcurrencyLimit(context.request_id)

                occurrence = await occurrences.create(
                    context.resource_scope,
                    ScheduledTaskRunCreate(
                        occurrence_id=str(uuid.uuid4()),
                        task_id=task.id,
                        task_version=task.version,
                        occurrence_key=manual_occurrence_key(task.id, idempotency_hash),
                        manual_idempotency_hash=idempotency_hash,
                        scheduled_for=now,
                        trigger="manual",
                        status="queued",
                        created_at=now,
                    ),
                )
                return ManualReservation(occurrence, True)
        except Exception as error:
            self._raise_mapped(error, context.request_id)

    async def claim_next(
        self,
        *,
        now: datetime,
        lease_owner: str,
        lease_seconds: int,
    ) -> ScheduledTaskRunRecord | None:
        now = self._validated_now(now)
        if not isinstance(lease_owner, str) or not lease_owner or len(lease_owner) > 128:
            raise ValueError("lease_owner must be between 1 and 128 characters")
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        try:
            async with self._session_factory() as session, session.begin():
                candidate_query = (
                    sa.select(
                        ScheduledTaskRunRow.id,
                        ScheduledTaskRunRow.scheduled_for,
                        ScheduledTaskRow,
                        ProjectMembershipRow.version,
                    )
                    .join(
                        ScheduledTaskRow,
                        sa.and_(
                            ScheduledTaskRow.project_id == ScheduledTaskRunRow.project_id,
                            ScheduledTaskRow.owner_user_id == ScheduledTaskRunRow.owner_user_id,
                            ScheduledTaskRow.id == ScheduledTaskRunRow.task_id,
                        ),
                    )
                    .join(
                        ProjectMembershipRow,
                        sa.and_(
                            ProjectMembershipRow.project_id == ScheduledTaskRow.project_id,
                            ProjectMembershipRow.user_id == ScheduledTaskRow.owner_user_id,
                        ),
                    )
                    .join(ProjectRow, ProjectRow.id == ScheduledTaskRow.project_id)
                    .where(
                        ScheduledTaskRunRow.status == "queued",
                        sa.or_(
                            ScheduledTaskRunRow.next_attempt_at.is_(None),
                            ScheduledTaskRunRow.next_attempt_at <= now,
                        ),
                        ScheduledTaskRow.frozen_at.is_(None),
                        ScheduledTaskRow.deleted_at.is_(None),
                        sa.or_(
                            ScheduledTaskRow.status == "enabled",
                            sa.and_(
                                ScheduledTaskRunRow.trigger == "manual",
                                ScheduledTaskRow.status == "paused",
                            ),
                        ),
                        ProjectMembershipRow.status == "active",
                        ProjectMembershipRow.role.in_(("admin", "editor", "runner")),
                        ProjectRow.status == "active",
                        ProjectRow.is_suspended.is_(False),
                    )
                )
                cursor: tuple[datetime, str] | None = None
                occurrences = ScheduledTaskRunRepository(session)
                while True:
                    query = candidate_query
                    if cursor is not None:
                        scheduled_for, occurrence_id = cursor
                        query = query.where(
                            sa.or_(
                                ScheduledTaskRunRow.scheduled_for > scheduled_for,
                                sa.and_(
                                    ScheduledTaskRunRow.scheduled_for == scheduled_for,
                                    ScheduledTaskRunRow.id > occurrence_id,
                                ),
                            )
                        )
                    candidate = (
                        await session.execute(
                            query.order_by(
                                ScheduledTaskRunRow.scheduled_for,
                                ScheduledTaskRunRow.id,
                            )
                            .limit(1)
                            .with_for_update(of=ScheduledTaskRow, skip_locked=True)
                        )
                    ).one_or_none()
                    if candidate is None:
                        return None
                    occurrence_id, scheduled_for, task_row, membership_version = candidate
                    cursor = (scheduled_for, occurrence_id)
                    scope = self._scope(task_row, membership_version)
                    # Claiming intentionally leaves thread_id/run_id unchanged. Their
                    # immediate final-schema FKs require Task 6 to create the real
                    # parent rows before atomically backfilling these pointers.
                    claimed = await occurrences.claim(
                        scope,
                        occurrence_id,
                        now=now,
                        lease_owner=lease_owner,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                    )
                    if claimed is not None:
                        return claimed
        except Exception as error:
            self._raise_mapped(error, "scheduler")

    async def claim_manual_occurrence(
        self,
        context: PrivateWorkContext,
        occurrence_id: str,
        *,
        now: datetime,
        lease_owner: str,
        lease_seconds: int,
    ) -> ManualDispatchClaim:
        """Claim exactly one scoped idempotent manual reservation.

        The unlocked first read discovers the task coordinate without taking
        an occurrence lock. The authoritative lock sequence remains project ->
        membership -> task -> occurrence. Existing launching/running/terminal
        rows are returned without dispatch ownership so a replay cannot launch
        the same reservation twice.
        """

        context = self._issued_context(context)
        now = self._validated_now(now, context.request_id)
        if not isinstance(occurrence_id, str) or not occurrence_id or len(occurrence_id) > 64:
            raise AutomationInvalid(context.request_id)
        if not isinstance(lease_owner, str) or not lease_owner or len(lease_owner) > 128:
            raise AutomationInvalid(context.request_id)
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise AutomationInvalid(context.request_id)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.AUTOMATION_MANAGE_OWN,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                occurrences = ScheduledTaskRunRepository(session)
                discovered = await occurrences.get(
                    context.resource_scope,
                    occurrence_id,
                )
                if discovered is None or discovered.trigger != "manual":
                    raise AutomationNotFound(context.request_id)
                if discovered.status in TERMINAL_OCCURRENCE_STATUSES:
                    return ManualDispatchClaim(discovered, False)

                task = await ScheduledTaskRepository(session).lock_active(
                    context.resource_scope,
                    discovered.task_id,
                )
                if task is None:
                    raise AutomationNotFound(context.request_id)
                occurrence = await occurrences.get(
                    context.resource_scope,
                    occurrence_id,
                    lock=True,
                )
                if occurrence is None or occurrence.task_id != task.id or occurrence.trigger != "manual":
                    raise AutomationNotFound(context.request_id)
                if occurrence.status in TERMINAL_OCCURRENCE_STATUSES or occurrence.status in {"launching", "running"}:
                    return ManualDispatchClaim(occurrence, False)
                if occurrence.status != "queued":
                    raise AutomationConflict(context.request_id)
                if task.status not in {"enabled", "paused"}:
                    raise AutomationNotFound(context.request_id)
                claimed = await occurrences.claim(
                    context.resource_scope,
                    occurrence.id,
                    now=now,
                    lease_owner=lease_owner,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                )
                if claimed is None:
                    current = await occurrences.get(
                        context.resource_scope,
                        occurrence.id,
                        lock=True,
                    )
                    if current is not None and (current.status in TERMINAL_OCCURRENCE_STATUSES or current.status in {"launching", "running"}):
                        return ManualDispatchClaim(current, False)
                    raise AutomationConflict(context.request_id)
                return ManualDispatchClaim(claimed, True)
        except Exception as error:
            self._raise_mapped(error, context.request_id)

    async def get(
        self,
        context: PrivateWorkContext,
        occurrence_id: str,
    ) -> AutomationRunView:
        context = self._issued_context(context)
        if not isinstance(occurrence_id, str) or not occurrence_id or len(occurrence_id) > 64:
            raise AutomationNotFound(context.request_id)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                record = await ScheduledTaskRunRepository(session).get(
                    context.resource_scope,
                    occurrence_id,
                )
                if record is None:
                    raise AutomationNotFound(context.request_id)
                return self._run_view(record)
        except Exception as error:
            self._raise_mapped(error, context.request_id)

    async def list(
        self,
        context: PrivateWorkContext,
        task_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[AutomationRunView, ...]:
        context = self._issued_context(context)
        if not isinstance(task_id, str) or not task_id or len(task_id) > 64:
            raise AutomationNotFound(context.request_id)
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or offset < 0:
            raise AutomationInvalid(context.request_id)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                task = await ScheduledTaskRepository(session).get(
                    context.resource_scope,
                    task_id,
                )
                if task is None:
                    raise AutomationNotFound(context.request_id)
                records = await ScheduledTaskRunRepository(session).list_by_task(
                    context.resource_scope,
                    task_id,
                    limit=limit,
                    offset=offset,
                )
                return tuple(self._run_view(record) for record in records)
        except Exception as error:
            self._raise_mapped(error, context.request_id)

    @staticmethod
    async def _acquire_admission_lock(session: AsyncSession) -> None:
        await session.execute(
            sa.text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _AUTOMATION_ADMISSION_LOCK},
        )

    @staticmethod
    async def _active_count(session: AsyncSession) -> int:
        value = await session.scalar(sa.select(sa.func.count()).select_from(ScheduledTaskRunRow).where(ScheduledTaskRunRow.status.in_(ACTIVE_OCCURRENCE_STATUSES)))
        return int(value or 0)

    @staticmethod
    def _scope(task_row: ScheduledTaskRow, membership_version: int) -> PrivateResourceScope:
        return PrivateResourceScope(
            project_id=str(task_row.project_id),
            owner_user_id=task_row.owner_user_id,
            membership_version=membership_version,
        )

    @staticmethod
    def _next_run(task_row: ScheduledTaskRow, now: datetime) -> datetime | None:
        if task_row.schedule_type == "once":
            return None
        return next_scheduled_occurrence(
            task_row.schedule_type,
            task_row.schedule_spec,
            task_row.timezone,
            now=now,
            coalesce=True,
        )

    @staticmethod
    def _validated_now(now: datetime, request_id: str = "scheduler") -> datetime:
        if not isinstance(now, datetime) or now.tzinfo is None:
            if request_id == "scheduler":
                raise ValueError("now must be timezone-aware")
            raise AutomationInvalid(request_id)
        return now.astimezone(UTC)

    @staticmethod
    def _run_view(record: ScheduledTaskRunRecord) -> AutomationRunView:
        return AutomationRunView(
            id=record.id,
            automation_id=record.task_id,
            automation_version=record.task_version,
            scheduled_for=record.scheduled_for,
            trigger=record.trigger,  # type: ignore[arg-type]
            status=record.status,  # type: ignore[arg-type]
            thread_id=record.thread_id,
            run_id=record.run_id,
            error_code=record.error_code,
            started_at=record.started_at,
            finished_at=record.finished_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _issued_context(context: PrivateWorkContext) -> PrivateWorkContext:
        try:
            return require_issued_private_work_context(context)
        except PrivateWorkNotFound as error:
            raise AutomationNotFound(error.request_id) from None

    @staticmethod
    def _raise_mapped(error: Exception, request_id: str):
        if isinstance(error, AutomationError):
            raise error
        if isinstance(error, PrivateWorkNotFound):
            raise AutomationNotFound(request_id) from None
        if isinstance(error, PrivateWorkForbidden):
            raise AutomationForbidden(request_id) from None
        if isinstance(error, PrivateWorkUnavailable):
            raise AutomationUnavailable(request_id) from None
        if isinstance(error, (DBAPIError, SATimeoutError)):
            raise AutomationUnavailable(request_id) from None
        if isinstance(error, (TypeError, ValueError)):
            if request_id == "scheduler":
                raise AutomationUnavailable(request_id) from None
            raise AutomationInvalid(request_id) from None
        raise error


__all__ = [
    "AutomationOccurrenceService",
    "FRESH_THREAD_NAMESPACE",
    "ManualReservation",
    "ManualDispatchClaim",
    "RUN_NAMESPACE",
    "deterministic_run_id",
    "deterministic_thread_id",
    "hash_manual_idempotency",
    "manual_occurrence_key",
    "scheduled_occurrence_key",
]
