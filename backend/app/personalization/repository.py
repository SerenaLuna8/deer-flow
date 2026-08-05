"""PostgreSQL authority for account Memory preferences and reset."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.private_work.memory_v2_management import (
    MemoryV2ManagementRepository,
)
from deerflow.persistence.private_work.model import (
    UserProjectMemoryFactRow,
    UserProjectMemoryRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow
from deerflow.persistence.user.model import UserRow


class AccountPersonalizationNotFound(LookupError):
    pass


class AccountPersonalizationConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AccountMemoryPreference:
    memory_enabled: bool
    version: int


@dataclass(frozen=True, slots=True)
class AccountMemoryResetCounts:
    version: int
    scopes_reset: int
    v1_memories: int
    source_batches: int
    candidates: int
    facts: int
    snapshots: int
    jobs_cancelled: int


class AccountPersonalizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def read_memory(
        self,
        user_id: uuid.UUID | str,
        *,
        for_update: bool = False,
    ) -> AccountMemoryPreference:
        try:
            owner_user_id = str(uuid.UUID(str(user_id)))
        except (TypeError, ValueError):
            raise AccountPersonalizationNotFound from None
        statement = select(UserRow).where(UserRow.id == owner_user_id)
        if for_update:
            statement = statement.with_for_update(of=UserRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AccountPersonalizationNotFound
        return AccountMemoryPreference(
            memory_enabled=bool(row.memory_enabled),
            version=int(row.preferences_version),
        )

    async def update_memory(
        self,
        user_id: uuid.UUID,
        *,
        memory_enabled: bool,
        expected_version: int,
    ) -> AccountMemoryPreference:
        current = await self.read_memory(user_id, for_update=True)
        if current.version != expected_version:
            raise AccountPersonalizationConflict
        row = await self.session.scalar(select(UserRow).where(UserRow.id == str(user_id)).with_for_update(of=UserRow))
        if row is None:
            raise AccountPersonalizationNotFound
        row.memory_enabled = memory_enabled
        row.preferences_version = current.version + 1
        await self.session.flush()
        return AccountMemoryPreference(
            memory_enabled=memory_enabled,
            version=int(row.preferences_version),
        )

    async def reset_memory(
        self,
        user_id: uuid.UUID,
        *,
        expected_version: int,
        now: datetime,
    ) -> AccountMemoryResetCounts:
        current = await self.read_memory(user_id, for_update=True)
        if current.version != expected_version:
            raise AccountPersonalizationConflict
        owner_user_id = str(user_id)
        project_ids = tuple((await self.session.execute(select(ProjectMembershipRow.project_id).where(ProjectMembershipRow.user_id == owner_user_id).distinct().order_by(ProjectMembershipRow.project_id))).scalars())
        v1_memories = 0
        source_batches = 0
        candidates = 0
        facts = 0
        snapshots = 0
        jobs_cancelled = 0
        memory_v2 = MemoryV2ManagementRepository(self.session)
        for project_id in project_ids:
            v1_memories += int(
                await self.session.scalar(
                    select(func.count())
                    .select_from(UserProjectMemoryRow)
                    .where(
                        UserProjectMemoryRow.project_id == project_id,
                        UserProjectMemoryRow.owner_user_id == owner_user_id,
                    )
                )
                or 0
            )
            reset = await memory_v2.reset_scope(
                project_id=project_id,
                owner_user_id=owner_user_id,
                now=now,
            )
            source_batches += reset.source_batches
            candidates += reset.candidates
            facts += reset.facts
            snapshots += reset.snapshots
            jobs_cancelled += reset.jobs_cancelled
            await self.session.execute(
                delete(UserProjectMemoryFactRow).where(
                    UserProjectMemoryFactRow.project_id == project_id,
                    UserProjectMemoryFactRow.owner_user_id == owner_user_id,
                )
            )
            await self.session.execute(
                delete(UserProjectMemoryRow).where(
                    UserProjectMemoryRow.project_id == project_id,
                    UserProjectMemoryRow.owner_user_id == owner_user_id,
                )
            )
        row = await self.session.scalar(select(UserRow).where(UserRow.id == owner_user_id).with_for_update(of=UserRow))
        if row is None:
            raise AccountPersonalizationNotFound
        row.preferences_version = current.version + 1
        await self.session.flush()
        return AccountMemoryResetCounts(
            version=int(row.preferences_version),
            scopes_reset=len(project_ids),
            v1_memories=v1_memories,
            source_batches=source_batches,
            candidates=candidates,
            facts=facts,
            snapshots=snapshots,
            jobs_cancelled=jobs_cancelled,
        )


__all__ = [
    "AccountMemoryPreference",
    "AccountMemoryResetCounts",
    "AccountPersonalizationConflict",
    "AccountPersonalizationNotFound",
    "AccountPersonalizationRepository",
]
