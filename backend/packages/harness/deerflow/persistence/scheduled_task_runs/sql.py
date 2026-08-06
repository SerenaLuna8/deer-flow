from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
from deerflow.runtime.private_scope import PrivateResourceScope

TERMINAL_OCCURRENCE_STATUSES = frozenset({"success", "failed", "skipped", "interrupted", "cancelled", "rejected"})
ACTIVE_OCCURRENCE_STATUSES = frozenset({"queued", "launching", "running"})


@dataclass(frozen=True, slots=True)
class ScheduledTaskRunCreate:
    occurrence_id: str
    task_id: str
    task_version: int
    occurrence_key: str
    manual_idempotency_hash: str | None
    scheduled_for: datetime
    trigger: str
    status: str
    error_code: str | None = None
    error_message: str | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ScheduledTaskRunRecord:
    id: str
    project_id: uuid.UUID
    owner_user_id: str
    task_id: str
    task_version: int
    occurrence_key: str
    scheduled_for: datetime
    trigger: str
    status: str
    thread_id: str | None
    run_id: str | None
    job_id: uuid.UUID | None
    resolved_membership_id: uuid.UUID | None
    resolved_membership_version: int | None
    launch_attempt_count: int
    next_attempt_at: datetime | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ScheduledTaskRunRepository:
    """Session-bound occurrence repository with mandatory private scope."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def coordinates(scope: PrivateResourceScope) -> tuple[uuid.UUID, str]:
        if type(scope) is not PrivateResourceScope:
            raise TypeError("PrivateResourceScope is required")
        try:
            return uuid.UUID(scope.project_id), str(uuid.UUID(scope.owner_user_id))
        except (TypeError, ValueError):
            raise TypeError("PrivateResourceScope is invalid") from None

    @classmethod
    def predicates(cls, scope: PrivateResourceScope):
        project_id, owner_user_id = cls.coordinates(scope)
        return (
            ScheduledTaskRunRow.project_id == project_id,
            ScheduledTaskRunRow.owner_user_id == owner_user_id,
        )

    @staticmethod
    def record(row: ScheduledTaskRunRow) -> ScheduledTaskRunRecord:
        return ScheduledTaskRunRecord(
            id=row.id,
            project_id=row.project_id,
            owner_user_id=row.owner_user_id,
            task_id=row.task_id,
            task_version=row.task_version,
            occurrence_key=row.occurrence_key,
            scheduled_for=row.scheduled_for,
            trigger=row.trigger,
            status=row.status,
            thread_id=row.thread_id,
            run_id=row.run_id,
            job_id=row.job_id,
            resolved_membership_id=row.resolved_membership_id,
            resolved_membership_version=row.resolved_membership_version,
            launch_attempt_count=row.launch_attempt_count,
            next_attempt_at=row.next_attempt_at,
            error_code=row.error_code,
            error_message=row.error_message,
            started_at=row.started_at,
            finished_at=row.finished_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _validate_page(limit: int, offset: int) -> None:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must be non-negative")

    async def create(
        self,
        scope: PrivateResourceScope,
        request: ScheduledTaskRunCreate,
    ) -> ScheduledTaskRunRecord:
        project_id, owner_user_id = self.coordinates(scope)
        now = request.created_at or datetime.now(UTC)
        row = ScheduledTaskRunRow(
            id=request.occurrence_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            task_id=request.task_id,
            task_version=request.task_version,
            occurrence_key=request.occurrence_key,
            manual_idempotency_hash=request.manual_idempotency_hash,
            scheduled_for=request.scheduled_for,
            trigger=request.trigger,
            status=request.status,
            resolved_membership_version=None,
            launch_attempt_count=0,
            error_code=request.error_code,
            error_message=request.error_message,
            finished_at=request.finished_at,
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        return self.record(row)

    async def get(
        self,
        scope: PrivateResourceScope,
        occurrence_id: str,
        *,
        lock: bool = False,
    ) -> ScheduledTaskRunRecord | None:
        statement = sa.select(ScheduledTaskRunRow).where(
            ScheduledTaskRunRow.id == occurrence_id,
            *self.predicates(scope),
        )
        if lock:
            statement = statement.with_for_update(of=ScheduledTaskRunRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def get_by_agent_run_id(
        self,
        scope: PrivateResourceScope,
        run_id: str,
        *,
        lock: bool = False,
    ) -> ScheduledTaskRunRecord | None:
        statement = sa.select(ScheduledTaskRunRow).where(
            ScheduledTaskRunRow.run_id == run_id,
            *self.predicates(scope),
        )
        if lock:
            statement = statement.with_for_update(of=ScheduledTaskRunRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def get_by_occurrence_key(
        self,
        scope: PrivateResourceScope,
        task_id: str,
        occurrence_key: str,
        *,
        lock: bool = False,
    ) -> ScheduledTaskRunRecord | None:
        statement = sa.select(ScheduledTaskRunRow).where(
            ScheduledTaskRunRow.task_id == task_id,
            ScheduledTaskRunRow.occurrence_key == occurrence_key,
            *self.predicates(scope),
        )
        if lock:
            statement = statement.with_for_update(of=ScheduledTaskRunRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def get_by_manual_idempotency(
        self,
        scope: PrivateResourceScope,
        task_id: str,
        manual_idempotency_hash: str,
    ) -> ScheduledTaskRunRecord | None:
        row = (
            await self.session.execute(
                sa.select(ScheduledTaskRunRow).where(
                    ScheduledTaskRunRow.task_id == task_id,
                    ScheduledTaskRunRow.trigger == "manual",
                    ScheduledTaskRunRow.manual_idempotency_hash == manual_idempotency_hash,
                    *self.predicates(scope),
                )
            )
        ).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def list_by_task(
        self,
        scope: PrivateResourceScope,
        task_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[ScheduledTaskRunRecord, ...]:
        self._validate_page(limit, offset)
        rows = (
            await self.session.execute(
                sa.select(ScheduledTaskRunRow)
                .where(
                    ScheduledTaskRunRow.task_id == task_id,
                    *self.predicates(scope),
                )
                .order_by(
                    ScheduledTaskRunRow.created_at.desc(),
                    ScheduledTaskRunRow.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
        return tuple(self.record(row) for row in rows)

    async def has_active(
        self,
        scope: PrivateResourceScope,
        task_id: str,
    ) -> bool:
        row = (
            await self.session.execute(
                sa.select(ScheduledTaskRunRow.id)
                .where(
                    ScheduledTaskRunRow.task_id == task_id,
                    ScheduledTaskRunRow.status.in_(ACTIVE_OCCURRENCE_STATUSES),
                    *self.predicates(scope),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return row is not None

    async def lock_active_by_task(
        self,
        scope: PrivateResourceScope,
        task_id: str,
    ) -> tuple[ScheduledTaskRunRecord, ...]:
        """Lock active occurrences before a definition mutation.

        Locking queued rows as well as launching/running rows serializes the
        definition mutation with a concurrent occurrence claim.
        """

        rows = (
            await self.session.execute(
                sa.select(ScheduledTaskRunRow)
                .where(
                    ScheduledTaskRunRow.task_id == task_id,
                    ScheduledTaskRunRow.status.in_(ACTIVE_OCCURRENCE_STATUSES),
                    *self.predicates(scope),
                )
                .order_by(ScheduledTaskRunRow.id)
                .with_for_update(of=ScheduledTaskRunRow)
            )
        ).scalars()
        return tuple(self.record(row) for row in rows)

    async def claim(
        self,
        scope: PrivateResourceScope,
        occurrence_id: str,
        *,
        now: datetime,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> ScheduledTaskRunRecord | None:
        claimable = (
            ScheduledTaskRunRow.id == occurrence_id,
            ScheduledTaskRunRow.status == "queued",
            sa.or_(
                ScheduledTaskRunRow.next_attempt_at.is_(None),
                ScheduledTaskRunRow.next_attempt_at <= now,
            ),
            *self.predicates(scope),
        )
        locked_id = await self.session.scalar(sa.select(ScheduledTaskRunRow.id).where(*claimable).with_for_update(of=ScheduledTaskRunRow, skip_locked=True))
        if locked_id is None:
            return None
        row = (
            await self.session.execute(
                sa.update(ScheduledTaskRunRow)
                .where(*claimable)
                .values(
                    status="launching",
                    lease_owner=lease_owner,
                    lease_expires_at=lease_expires_at,
                    launch_attempt_count=ScheduledTaskRunRow.launch_attempt_count + 1,
                    started_at=sa.func.coalesce(ScheduledTaskRunRow.started_at, now),
                    updated_at=now,
                )
                .returning(ScheduledTaskRunRow)
            )
        ).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def record_resolution(
        self,
        scope: PrivateResourceScope,
        occurrence_id: str,
        *,
        membership_id: uuid.UUID,
        membership_version: int,
        updated_at: datetime,
    ) -> ScheduledTaskRunRecord | None:
        row = (
            await self.session.execute(
                sa.update(ScheduledTaskRunRow)
                .where(
                    ScheduledTaskRunRow.id == occurrence_id,
                    ScheduledTaskRunRow.status == "launching",
                    ScheduledTaskRunRow.thread_id.is_(None),
                    ScheduledTaskRunRow.run_id.is_(None),
                    *self.predicates(scope),
                )
                .values(
                    resolved_membership_id=membership_id,
                    resolved_membership_version=membership_version,
                    updated_at=updated_at,
                )
                .returning(ScheduledTaskRunRow)
            )
        ).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def mark_running(
        self,
        scope: PrivateResourceScope,
        occurrence_id: str,
        *,
        thread_id: str,
        run_id: str,
        started_at: datetime,
        updated_at: datetime,
    ) -> ScheduledTaskRunRecord | None:
        row = (
            await self.session.execute(
                sa.update(ScheduledTaskRunRow)
                .where(
                    ScheduledTaskRunRow.id == occurrence_id,
                    ScheduledTaskRunRow.status == "launching",
                    ScheduledTaskRunRow.thread_id.is_(None),
                    ScheduledTaskRunRow.run_id.is_(None),
                    *self.predicates(scope),
                )
                .values(
                    status="running",
                    thread_id=thread_id,
                    run_id=run_id,
                    started_at=started_at,
                    next_attempt_at=None,
                    error_code=None,
                    error_message=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=updated_at,
                )
                .returning(ScheduledTaskRunRow)
            )
        ).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def mark_admitted(
        self,
        scope: PrivateResourceScope,
        occurrence_id: str,
        *,
        thread_id: str,
        run_id: str,
        job_id: uuid.UUID,
        membership_id: uuid.UUID,
        membership_version: int,
        admitted_at: datetime,
    ) -> ScheduledTaskRunRecord | None:
        """Attach the atomically-created private Run and durable job once."""

        row = (
            await self.session.execute(
                sa.update(ScheduledTaskRunRow)
                .where(
                    ScheduledTaskRunRow.id == occurrence_id,
                    ScheduledTaskRunRow.status == "launching",
                    ScheduledTaskRunRow.thread_id.is_(None),
                    ScheduledTaskRunRow.run_id.is_(None),
                    ScheduledTaskRunRow.job_id.is_(None),
                    *self.predicates(scope),
                )
                .values(
                    status="running",
                    thread_id=thread_id,
                    run_id=run_id,
                    job_id=job_id,
                    resolved_membership_id=membership_id,
                    resolved_membership_version=membership_version,
                    launch_attempt_count=1,
                    started_at=admitted_at,
                    next_attempt_at=None,
                    error_code=None,
                    error_message=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=admitted_at,
                )
                .returning(ScheduledTaskRunRow)
            )
        ).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def requeue_launch(
        self,
        scope: PrivateResourceScope,
        occurrence_id: str,
        *,
        next_attempt_at: datetime,
        error_code: str,
        updated_at: datetime,
    ) -> ScheduledTaskRunRecord | None:
        row = (
            await self.session.execute(
                sa.update(ScheduledTaskRunRow)
                .where(
                    ScheduledTaskRunRow.id == occurrence_id,
                    ScheduledTaskRunRow.status == "launching",
                    ScheduledTaskRunRow.thread_id.is_(None),
                    ScheduledTaskRunRow.run_id.is_(None),
                    *self.predicates(scope),
                )
                .values(
                    status="queued",
                    next_attempt_at=next_attempt_at,
                    error_code=error_code,
                    error_message=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=updated_at,
                )
                .returning(ScheduledTaskRunRow)
            )
        ).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def reject_launch(
        self,
        scope: PrivateResourceScope,
        occurrence_id: str,
        *,
        error_code: str,
        finished_at: datetime,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> ScheduledTaskRunRecord | None:
        if (thread_id is None) != (run_id is None):
            raise ValueError("thread_id and run_id must be supplied together")
        row = (
            await self.session.execute(
                sa.update(ScheduledTaskRunRow)
                .where(
                    ScheduledTaskRunRow.id == occurrence_id,
                    ScheduledTaskRunRow.status == "launching",
                    ScheduledTaskRunRow.thread_id.is_(None),
                    ScheduledTaskRunRow.run_id.is_(None),
                    *self.predicates(scope),
                )
                .values(
                    status="rejected",
                    thread_id=thread_id,
                    run_id=run_id,
                    next_attempt_at=None,
                    error_code=error_code,
                    error_message=None,
                    finished_at=finished_at,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=finished_at,
                )
                .returning(ScheduledTaskRunRow)
            )
        ).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def finish(
        self,
        scope: PrivateResourceScope,
        occurrence_id: str,
        *,
        status: str,
        error_code: str | None,
        error_message: str | None,
        finished_at: datetime,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> bool:
        if status not in TERMINAL_OCCURRENCE_STATUSES:
            raise ValueError("status must be terminal")
        if (thread_id is None) != (run_id is None):
            raise ValueError("thread_id and run_id must be supplied together")
        compatible_links = ()
        values: dict[str, object] = {}
        if thread_id is not None and run_id is not None:
            compatible_links = (
                sa.or_(
                    ScheduledTaskRunRow.thread_id.is_(None),
                    ScheduledTaskRunRow.thread_id == thread_id,
                ),
                sa.or_(
                    ScheduledTaskRunRow.run_id.is_(None),
                    ScheduledTaskRunRow.run_id == run_id,
                ),
            )
            values.update(
                thread_id=sa.func.coalesce(ScheduledTaskRunRow.thread_id, thread_id),
                run_id=sa.func.coalesce(ScheduledTaskRunRow.run_id, run_id),
            )
        result = await self.session.execute(
            sa.update(ScheduledTaskRunRow)
            .where(
                ScheduledTaskRunRow.id == occurrence_id,
                ScheduledTaskRunRow.status.not_in(TERMINAL_OCCURRENCE_STATUSES),
                *compatible_links,
                *self.predicates(scope),
            )
            .values(
                **values,
                status=status,
                error_code=error_code,
                error_message=error_message,
                finished_at=finished_at,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=finished_at,
            )
        )
        return result.rowcount == 1

    async def cancel_queued(
        self,
        scope: PrivateResourceScope,
        task_id: str,
        *,
        now: datetime,
        error_code: str,
    ) -> int:
        result = await self.session.execute(
            sa.update(ScheduledTaskRunRow)
            .where(
                ScheduledTaskRunRow.task_id == task_id,
                ScheduledTaskRunRow.status == "queued",
                *self.predicates(scope),
            )
            .values(
                status="cancelled",
                error_code=error_code,
                finished_at=now,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        return result.rowcount


__all__ = [
    "ACTIVE_OCCURRENCE_STATUSES",
    "ScheduledTaskRunCreate",
    "ScheduledTaskRunRecord",
    "ScheduledTaskRunRepository",
    "TERMINAL_OCCURRENCE_STATUSES",
]
