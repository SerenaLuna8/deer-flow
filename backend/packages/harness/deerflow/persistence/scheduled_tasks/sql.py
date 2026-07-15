from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
from deerflow.runtime.private_scope import PrivateResourceScope


@dataclass(frozen=True, slots=True)
class ScheduledTaskCreate:
    task_id: str
    thread_id: str | None
    context_mode: str
    agent_asset_id: uuid.UUID
    agent_scope: str
    title: str
    prompt: str
    schedule_type: str
    schedule_spec: dict[str, object]
    timezone: str
    next_run_at: datetime | None


@dataclass(frozen=True, slots=True)
class ScheduledTaskPatch:
    title: str | None = None
    prompt: str | None = None
    schedule_spec: dict[str, object] | None = None
    timezone: str | None = None
    next_run_at: datetime | None = None
    status: str | None = None


_TASK_PATCH_FIELDS = frozenset(field.name for field in fields(ScheduledTaskPatch))


@dataclass(frozen=True, slots=True)
class ScheduledTaskRecord:
    id: str
    project_id: uuid.UUID
    owner_user_id: str
    thread_id: str | None
    context_mode: str
    agent_asset_id: uuid.UUID
    agent_scope: str
    title: str
    prompt: str
    schedule_type: str
    schedule_spec: dict[str, object]
    timezone: str
    status: str
    overlap_policy: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_outcome: str | None
    last_error_code: str | None
    run_count: int
    version: int
    frozen_at: datetime | None
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ScheduledTaskRepository:
    """Session-bound definition repository with mandatory private scope."""

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
            ScheduledTaskRow.project_id == project_id,
            ScheduledTaskRow.owner_user_id == owner_user_id,
        )

    @staticmethod
    def record(row: ScheduledTaskRow) -> ScheduledTaskRecord:
        return ScheduledTaskRecord(
            id=row.id,
            project_id=row.project_id,
            owner_user_id=row.owner_user_id,
            thread_id=row.thread_id,
            context_mode=row.context_mode,
            agent_asset_id=row.agent_asset_id,
            agent_scope=row.agent_scope,
            title=row.title,
            prompt=row.prompt,
            schedule_type=row.schedule_type,
            schedule_spec=dict(row.schedule_spec or {}),
            timezone=row.timezone,
            status=row.status,
            overlap_policy=row.overlap_policy,
            next_run_at=row.next_run_at,
            last_run_at=row.last_run_at,
            last_outcome=row.last_outcome,
            last_error_code=row.last_error_code,
            run_count=row.run_count,
            version=row.version,
            frozen_at=row.frozen_at,
            deleted_at=row.deleted_at,
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
        request: ScheduledTaskCreate,
    ) -> ScheduledTaskRecord:
        project_id, owner_user_id = self.coordinates(scope)
        now = datetime.now(UTC)
        row = ScheduledTaskRow(
            id=request.task_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            thread_id=request.thread_id,
            context_mode=request.context_mode,
            agent_asset_id=request.agent_asset_id,
            agent_scope=request.agent_scope,
            title=request.title,
            prompt=request.prompt,
            schedule_type=request.schedule_type,
            schedule_spec=dict(request.schedule_spec),
            timezone=request.timezone,
            status="enabled",
            overlap_policy="skip",
            next_run_at=request.next_run_at,
            run_count=0,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        return self.record(row)

    async def get(
        self,
        scope: PrivateResourceScope,
        task_id: str,
    ) -> ScheduledTaskRecord | None:
        row = (
            await self.session.execute(
                sa.select(ScheduledTaskRow).where(
                    ScheduledTaskRow.id == task_id,
                    ScheduledTaskRow.deleted_at.is_(None),
                    *self.predicates(scope),
                )
            )
        ).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def lock_active(
        self,
        scope: PrivateResourceScope,
        task_id: str,
    ) -> ScheduledTaskRecord | None:
        row = (
            await self.session.execute(
                sa.select(ScheduledTaskRow)
                .where(
                    ScheduledTaskRow.id == task_id,
                    ScheduledTaskRow.deleted_at.is_(None),
                    ScheduledTaskRow.frozen_at.is_(None),
                    *self.predicates(scope),
                )
                .with_for_update(of=ScheduledTaskRow)
            )
        ).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def list(
        self,
        scope: PrivateResourceScope,
        *,
        limit: int,
        offset: int,
    ) -> tuple[ScheduledTaskRecord, ...]:
        self._validate_page(limit, offset)
        rows = (
            await self.session.execute(
                sa.select(ScheduledTaskRow)
                .where(
                    ScheduledTaskRow.deleted_at.is_(None),
                    *self.predicates(scope),
                )
                .order_by(
                    ScheduledTaskRow.created_at.desc(),
                    ScheduledTaskRow.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
        return tuple(self.record(row) for row in rows)

    async def list_by_thread(
        self,
        scope: PrivateResourceScope,
        thread_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[ScheduledTaskRecord, ...]:
        self._validate_page(limit, offset)
        rows = (
            await self.session.execute(
                sa.select(ScheduledTaskRow)
                .where(
                    ScheduledTaskRow.thread_id == thread_id,
                    ScheduledTaskRow.deleted_at.is_(None),
                    *self.predicates(scope),
                )
                .order_by(
                    ScheduledTaskRow.created_at.desc(),
                    ScheduledTaskRow.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
        return tuple(self.record(row) for row in rows)

    async def update(
        self,
        scope: PrivateResourceScope,
        task_id: str,
        *,
        expected_version: int,
        values: Mapping[str, object],
    ) -> ScheduledTaskRecord | None:
        if set(values) - _TASK_PATCH_FIELDS:
            raise ValueError("values contain non-patchable scheduled-task fields")
        statement = (
            sa.update(ScheduledTaskRow)
            .where(
                ScheduledTaskRow.id == task_id,
                ScheduledTaskRow.version == expected_version,
                ScheduledTaskRow.deleted_at.is_(None),
                *self.predicates(scope),
            )
            .values(
                **dict(values),
                version=ScheduledTaskRow.version + 1,
                updated_at=datetime.now(UTC),
            )
            .returning(ScheduledTaskRow)
        )
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def soft_delete(
        self,
        scope: PrivateResourceScope,
        task_id: str,
        *,
        expected_version: int,
        deleted_at: datetime,
    ) -> bool:
        result = await self.session.execute(
            sa.update(ScheduledTaskRow)
            .where(
                ScheduledTaskRow.id == task_id,
                ScheduledTaskRow.version == expected_version,
                ScheduledTaskRow.deleted_at.is_(None),
                *self.predicates(scope),
            )
            .values(
                deleted_at=deleted_at,
                status="paused",
                next_run_at=None,
                version=ScheduledTaskRow.version + 1,
                updated_at=deleted_at,
            )
        )
        return result.rowcount == 1


__all__ = [
    "ScheduledTaskCreate",
    "ScheduledTaskPatch",
    "ScheduledTaskRecord",
    "ScheduledTaskRepository",
]
