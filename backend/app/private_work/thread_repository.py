from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.errors import PrivateWorkConflict
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.private_scope import PrivateResourceScope


@dataclass(frozen=True, slots=True)
class ThreadAgentRef:
    asset_id: uuid.UUID
    scope: str


@dataclass(frozen=True, slots=True)
class PrivateThreadRecord:
    thread_id: str
    project_id: uuid.UUID
    owner_user_id: str
    agent_asset_id: uuid.UUID
    agent_scope: str
    display_name: str | None
    thread_kind: str
    status: str
    metadata: dict[str, Any]
    frozen_at: datetime | None
    deleted_at: datetime | None
    checkpoint_delete_status: str
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CheckpointDeleteCandidate:
    """Content-free coordinates for idempotent raw checkpoint cleanup."""

    thread_id: str
    project_id: uuid.UUID
    owner_user_id: str
    thread_kind: Literal["chat", "skill_builder"]
    created_at: datetime
    deleted_at: datetime


_UNSET = object()
_AUTOMATIC_TITLE_PLACEHOLDERS = (
    "",
    "New conversation",
    "Untitled",
    "新对话",
)


class PrivateThreadRepository:
    """Project-thread persistence with scope embedded in every statement."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _coordinates(scope: PrivateResourceScope) -> tuple[uuid.UUID, str]:
        if type(scope) is not PrivateResourceScope:
            raise PrivateWorkConflict("unknown")
        try:
            return uuid.UUID(scope.project_id), str(uuid.UUID(scope.owner_user_id))
        except (TypeError, ValueError):
            raise PrivateWorkConflict("unknown") from None

    @classmethod
    def _active_scope(
        cls,
        scope: PrivateResourceScope,
        thread_id: str,
        *,
        thread_kind: str = "chat",
    ):
        project_id, owner_user_id = cls._coordinates(scope)
        if thread_kind not in {"chat", "skill_builder"}:
            raise PrivateWorkConflict("unknown")
        return (
            ThreadMetaRow.thread_id == thread_id,
            ThreadMetaRow.project_id == project_id,
            ThreadMetaRow.owner_user_id == owner_user_id,
            ThreadMetaRow.thread_kind == thread_kind,
            ThreadMetaRow.deleted_at.is_(None),
            ThreadMetaRow.frozen_at.is_(None),
        )

    @staticmethod
    def _record(row: ThreadMetaRow) -> PrivateThreadRecord:
        return PrivateThreadRecord(
            thread_id=row.thread_id,
            project_id=row.project_id,
            owner_user_id=row.owner_user_id,
            agent_asset_id=row.agent_asset_id,
            agent_scope=row.agent_scope,
            display_name=row.display_name,
            thread_kind=row.thread_kind,
            status=row.status,
            metadata=dict(row.metadata_json or {}),
            frozen_at=row.frozen_at,
            deleted_at=row.deleted_at,
            checkpoint_delete_status=row.checkpoint_delete_status,
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def create(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        agent: ThreadAgentRef,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        thread_kind: str = "chat",
    ) -> PrivateThreadRecord:
        project_id, owner_user_id = self._coordinates(scope)
        if thread_kind not in {"chat", "skill_builder"}:
            raise PrivateWorkConflict("unknown")
        now = datetime.now(UTC)
        row = ThreadMetaRow(
            thread_id=thread_id,
            owner_user_id=owner_user_id,
            project_id=project_id,
            agent_asset_id=agent.asset_id,
            agent_scope=agent.scope,
            display_name=display_name,
            thread_kind=thread_kind,
            status="idle",
            metadata_json=dict(metadata or {}),
            checkpoint_delete_status="not_requested",
            version=1,
            created_at=now,
            updated_at=now,
        )
        try:
            self.session.add(row)
            await self.session.flush()
        except IntegrityError:
            raise PrivateWorkConflict("unknown") from None
        return self._record(row)

    async def get(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        lock: bool = False,
        thread_kind: str = "chat",
    ) -> PrivateThreadRecord | None:
        statement = select(ThreadMetaRow).where(
            *self._active_scope(
                scope,
                thread_id,
                thread_kind=thread_kind,
            )
        )
        if lock:
            statement = statement.with_for_update(of=ThreadMetaRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else self._record(row)

    async def get_deleted(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        lock: bool = False,
        thread_kind: str = "chat",
    ) -> PrivateThreadRecord | None:
        """Return an exact scoped tombstone without exposing it to normal reads."""

        project_id, owner_user_id = self._coordinates(scope)
        if thread_kind not in {"chat", "skill_builder"}:
            raise PrivateWorkConflict("unknown")
        statement = select(ThreadMetaRow).where(
            ThreadMetaRow.thread_id == thread_id,
            ThreadMetaRow.project_id == project_id,
            ThreadMetaRow.owner_user_id == owner_user_id,
            ThreadMetaRow.thread_kind == thread_kind,
            ThreadMetaRow.deleted_at.is_not(None),
        )
        if lock:
            statement = statement.with_for_update(of=ThreadMetaRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else self._record(row)

    async def check_access(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
    ) -> bool:
        statement = select(ThreadMetaRow.thread_id).where(*self._active_scope(scope, thread_id))
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def search(
        self,
        *,
        scope: PrivateResourceScope,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[PrivateThreadRecord, ...]:
        project_id, owner_user_id = self._coordinates(scope)
        statement = (
            select(ThreadMetaRow)
            .where(
                ThreadMetaRow.project_id == project_id,
                ThreadMetaRow.owner_user_id == owner_user_id,
                ThreadMetaRow.thread_kind == "chat",
                ThreadMetaRow.deleted_at.is_(None),
                ThreadMetaRow.frozen_at.is_(None),
            )
            .order_by(ThreadMetaRow.updated_at.desc(), ThreadMetaRow.thread_id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self.session.execute(statement)).scalars()
        return tuple(self._record(row) for row in rows)

    async def touch_activity(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        occurred_at: datetime,
        thread_kind: str = "chat",
    ) -> None:
        """Advance activity ordering without invalidating metadata CAS versions."""

        if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
            raise PrivateWorkConflict("unknown")
        await self.session.execute(
            update(ThreadMetaRow)
            .where(
                *self._active_scope(
                    scope,
                    thread_id,
                    thread_kind=thread_kind,
                ),
                ThreadMetaRow.updated_at < func.now(),
            )
            .values(updated_at=occurred_at.astimezone(UTC))
        )

    async def patch(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        expected_version: int,
        display_name: str | None | object = _UNSET,
        metadata: dict[str, Any] | object = _UNSET,
        status: str | object = _UNSET,
    ) -> PrivateThreadRecord:
        values: dict[str, object] = {
            "updated_at": datetime.now(UTC),
            "version": ThreadMetaRow.version + 1,
        }
        if display_name is not _UNSET:
            values["display_name"] = display_name
        if metadata is not _UNSET:
            values["metadata_json"] = dict(metadata)  # type: ignore[arg-type]
        if status is not _UNSET:
            values["status"] = status
        statement = (
            update(ThreadMetaRow)
            .where(
                *self._active_scope(scope, thread_id),
                ThreadMetaRow.version == expected_version,
            )
            .values(**values)
            .returning(ThreadMetaRow)
        )
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise PrivateWorkConflict("unknown")
        return self._record(row)

    async def set_automatic_display_name(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        display_name: str,
    ) -> bool:
        """Persist the one-time backend title without replacing a manual name."""

        normalized = display_name.strip()
        if not normalized:
            return False
        statement = (
            update(ThreadMetaRow)
            .where(
                *self._active_scope(scope, thread_id),
                ThreadMetaRow.version == 1,
                or_(
                    ThreadMetaRow.display_name.is_(None),
                    ThreadMetaRow.display_name.in_(
                        _AUTOMATIC_TITLE_PLACEHOLDERS,
                    ),
                ),
            )
            .values(
                display_name=normalized,
                updated_at=datetime.now(UTC),
            )
            .returning(ThreadMetaRow.thread_id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def mark_deleted(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        expected_version: int,
        thread_kind: str = "chat",
    ) -> PrivateThreadRecord:
        now = datetime.now(UTC)
        statement = (
            update(ThreadMetaRow)
            .where(
                *self._active_scope(
                    scope,
                    thread_id,
                    thread_kind=thread_kind,
                ),
                ThreadMetaRow.version == expected_version,
            )
            .values(
                deleted_at=now,
                checkpoint_delete_status="not_requested",
                updated_at=now,
                version=ThreadMetaRow.version + 1,
            )
            .returning(ThreadMetaRow)
        )
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise PrivateWorkConflict("unknown")
        return self._record(row)

    async def request_checkpoint_delete_for_compensation(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        expected_created_at: datetime,
        expected_deleted_at: datetime,
        thread_kind: str = "chat",
    ) -> PrivateThreadRecord:
        """Queue raw cleanup only for a trusted failed-create compensation."""

        project_id, owner_user_id = self._coordinates(scope)
        if not isinstance(expected_created_at, datetime) or expected_created_at.tzinfo is None or not isinstance(expected_deleted_at, datetime) or expected_deleted_at.tzinfo is None:
            raise PrivateWorkConflict("unknown")
        if thread_kind not in {"chat", "skill_builder"}:
            raise PrivateWorkConflict("unknown")
        row = (
            await self.session.execute(
                update(ThreadMetaRow)
                .where(
                    ThreadMetaRow.thread_id == thread_id,
                    ThreadMetaRow.project_id == project_id,
                    ThreadMetaRow.owner_user_id == owner_user_id,
                    ThreadMetaRow.thread_kind == thread_kind,
                    ThreadMetaRow.created_at == expected_created_at,
                    ThreadMetaRow.deleted_at == expected_deleted_at,
                    ThreadMetaRow.checkpoint_delete_status == "not_requested",
                )
                .values(
                    checkpoint_delete_status="pending",
                    updated_at=func.clock_timestamp(),
                )
                .returning(ThreadMetaRow)
            )
        ).scalar_one_or_none()
        if row is None:
            raise PrivateWorkConflict("unknown")
        return self._record(row)

    async def set_checkpoint_delete_status(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        status: str,
        thread_kind: str | None = None,
    ) -> bool:
        if status not in {"complete", "retry_required"}:
            raise PrivateWorkConflict("unknown")
        if thread_kind is not None and thread_kind not in {
            "chat",
            "skill_builder",
        }:
            raise PrivateWorkConflict("unknown")
        project_id, owner_user_id = self._coordinates(scope)
        target_statuses = ("pending", "retry_required")
        statement = (
            update(ThreadMetaRow)
            .where(
                ThreadMetaRow.thread_id == thread_id,
                ThreadMetaRow.project_id == project_id,
                ThreadMetaRow.owner_user_id == owner_user_id,
                ThreadMetaRow.deleted_at.is_not(None),
                ThreadMetaRow.checkpoint_delete_status.in_(target_statuses),
            )
            .values(
                checkpoint_delete_status=status,
                updated_at=func.clock_timestamp(),
            )
            .returning(ThreadMetaRow.thread_id)
        )
        if thread_kind is not None:
            statement = statement.where(ThreadMetaRow.thread_kind == thread_kind)
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def list_checkpoint_delete_candidates(
        self,
        *,
        limit: int,
    ) -> tuple[CheckpointDeleteCandidate, ...]:
        """Select durable cleanup work without holding locks across raw I/O."""

        if not 1 <= limit <= 1000:
            raise PrivateWorkConflict("unknown")
        rows = (
            (
                await self.session.execute(
                    select(ThreadMetaRow)
                    .where(
                        ThreadMetaRow.deleted_at.is_not(None),
                        ThreadMetaRow.checkpoint_delete_status.in_(
                            ("pending", "retry_required"),
                        ),
                    )
                    .order_by(
                        ThreadMetaRow.updated_at,
                        ThreadMetaRow.thread_id,
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            CheckpointDeleteCandidate(
                thread_id=row.thread_id,
                project_id=row.project_id,
                owner_user_id=row.owner_user_id,
                thread_kind=row.thread_kind,
                created_at=row.created_at,
                deleted_at=row.deleted_at,
            )
            for row in rows
            if row.deleted_at is not None
        )

    async def lock_checkpoint_delete_candidate(
        self,
        candidate: CheckpointDeleteCandidate,
    ) -> bool:
        """Fence raw cleanup to the exact still-pending tombstone generation."""

        row = (
            await self.session.execute(
                select(ThreadMetaRow.thread_id)
                .where(
                    ThreadMetaRow.thread_id == candidate.thread_id,
                    ThreadMetaRow.project_id == candidate.project_id,
                    ThreadMetaRow.owner_user_id == candidate.owner_user_id,
                    ThreadMetaRow.thread_kind == candidate.thread_kind,
                    ThreadMetaRow.created_at == candidate.created_at,
                    ThreadMetaRow.deleted_at == candidate.deleted_at,
                    ThreadMetaRow.checkpoint_delete_status.in_(
                        ("pending", "retry_required"),
                    ),
                )
                .with_for_update(of=ThreadMetaRow)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return row is not None

    async def purge_compensated_create(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        expected_created_at: datetime,
        expected_deleted_at: datetime,
    ) -> None:
        project_id, owner_user_id = self._coordinates(scope)
        if not isinstance(expected_created_at, datetime) or expected_created_at.tzinfo is None or not isinstance(expected_deleted_at, datetime) or expected_deleted_at.tzinfo is None:
            raise PrivateWorkConflict("unknown")
        deleted_thread_id = (
            await self.session.execute(
                delete(ThreadMetaRow)
                .where(
                    ThreadMetaRow.thread_id == thread_id,
                    ThreadMetaRow.project_id == project_id,
                    ThreadMetaRow.owner_user_id == owner_user_id,
                    ThreadMetaRow.created_at == expected_created_at,
                    ThreadMetaRow.deleted_at == expected_deleted_at,
                    ThreadMetaRow.checkpoint_delete_status == "complete",
                )
                .returning(ThreadMetaRow.thread_id)
            )
        ).scalar_one_or_none()
        if deleted_thread_id is None:
            raise PrivateWorkConflict("unknown")
