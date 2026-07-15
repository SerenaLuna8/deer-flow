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
        now = datetime.now(UTC)
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
            resolved_membership_version=scope.membership_version,
            launch_attempt_count=0,
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

    async def finish(
        self,
        scope: PrivateResourceScope,
        occurrence_id: str,
        *,
        status: str,
        error_code: str | None,
        error_message: str | None,
        finished_at: datetime,
    ) -> bool:
        if status not in TERMINAL_OCCURRENCE_STATUSES:
            raise ValueError("status must be terminal")
        result = await self.session.execute(
            sa.update(ScheduledTaskRunRow)
            .where(
                ScheduledTaskRunRow.id == occurrence_id,
                ScheduledTaskRunRow.status.not_in(TERMINAL_OCCURRENCE_STATUSES),
                *self.predicates(scope),
            )
            .values(
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
