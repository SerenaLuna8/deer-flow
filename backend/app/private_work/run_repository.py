from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.errors import PrivateWorkConflict
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.private_scope import PrivateResourceScope


@dataclass(frozen=True, slots=True)
class PrivateRunCreate:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    assistant_id: str | None = None
    status: str = "pending"
    multitask_strategy: str = "reject"
    metadata: dict[str, Any] = field(default_factory=dict)
    kwargs: dict[str, Any] = field(default_factory=dict)
    model_name: str | None = None


@dataclass(frozen=True, slots=True)
class PrivateRunRecord:
    run_id: str
    thread_id: str
    project_id: uuid.UUID
    owner_user_id: str
    assistant_id: str | None
    status: str
    multitask_strategy: str
    metadata: dict[str, Any]
    kwargs: dict[str, Any]
    error: str | None
    model_name: str | None
    created_at: datetime
    updated_at: datetime


class PrivateRunRepository:
    """Session-bound run repository whose every statement carries private scope."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def coordinates(scope: PrivateResourceScope) -> tuple[uuid.UUID, str]:
        if type(scope) is not PrivateResourceScope:
            raise PrivateWorkConflict("unknown")
        try:
            return uuid.UUID(scope.project_id), str(uuid.UUID(scope.owner_user_id))
        except (TypeError, ValueError):
            raise PrivateWorkConflict("unknown") from None

    @classmethod
    def predicates(cls, scope: PrivateResourceScope):
        project_id, owner_user_id = cls.coordinates(scope)
        return (
            RunRow.project_id == project_id,
            RunRow.owner_user_id == owner_user_id,
        )

    @staticmethod
    def record(row: RunRow) -> PrivateRunRecord:
        return PrivateRunRecord(
            run_id=row.run_id,
            thread_id=row.thread_id,
            project_id=row.project_id,
            owner_user_id=row.owner_user_id,
            assistant_id=row.assistant_id,
            status=row.status,
            multitask_strategy=row.multitask_strategy,
            metadata=dict(row.metadata_json or {}),
            kwargs=dict(row.kwargs_json or {}),
            error=row.error,
            model_name=row.model_name,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def create(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        request: PrivateRunCreate,
    ) -> PrivateRunRecord:
        project_id, owner_user_id = self.coordinates(scope)
        thread_exists = (
            await self.session.execute(
                select(ThreadMetaRow.thread_id).where(
                    ThreadMetaRow.thread_id == thread_id,
                    ThreadMetaRow.project_id == project_id,
                    ThreadMetaRow.owner_user_id == owner_user_id,
                    ThreadMetaRow.deleted_at.is_(None),
                    ThreadMetaRow.frozen_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if thread_exists is None:
            raise PrivateWorkConflict("unknown")
        now = datetime.now(UTC)
        row = RunRow(
            run_id=request.run_id,
            thread_id=thread_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            assistant_id=request.assistant_id,
            status=request.status,
            multitask_strategy=request.multitask_strategy,
            metadata_json=dict(request.metadata),
            kwargs_json=dict(request.kwargs),
            model_name=request.model_name,
            created_at=now,
            updated_at=now,
        )
        try:
            self.session.add(row)
            await self.session.flush()
        except IntegrityError:
            raise PrivateWorkConflict("unknown") from None
        return self.record(row)

    async def get(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        lock: bool = False,
    ) -> PrivateRunRecord | None:
        statement = select(RunRow).where(
            RunRow.run_id == run_id,
            *self.predicates(scope),
        )
        if lock:
            statement = statement.with_for_update(of=RunRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else self.record(row)

    async def list_by_thread(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[PrivateRunRecord, ...]:
        statement = (
            select(RunRow)
            .where(
                RunRow.thread_id == thread_id,
                *self.predicates(scope),
            )
            .order_by(RunRow.created_at.desc(), RunRow.run_id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self.session.execute(statement)).scalars()
        return tuple(self.record(row) for row in rows)

    async def update_status(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        status: str,
        error: str | None = None,
    ) -> bool:
        values: dict[str, Any] = {
            "status": status,
            "updated_at": datetime.now(UTC),
        }
        if error is not None:
            values["error"] = error
        result = await self.session.execute(update(RunRow).where(RunRow.run_id == run_id, *self.predicates(scope)).values(**values))
        return result.rowcount != 0

    async def update_model_name(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        model_name: str | None,
    ) -> bool:
        result = await self.session.execute(update(RunRow).where(RunRow.run_id == run_id, *self.predicates(scope)).values(model_name=model_name, updated_at=datetime.now(UTC)))
        return result.rowcount != 0

    async def delete(
        self,
        *,
        scope: PrivateResourceScope,
        run_id: str,
    ) -> bool:
        result = await self.session.execute(delete(RunRow).where(RunRow.run_id == run_id, *self.predicates(scope)))
        return result.rowcount != 0
