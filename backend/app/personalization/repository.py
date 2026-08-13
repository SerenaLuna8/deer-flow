"""PostgreSQL authority for account Memory preferences and reset."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentConflict,
    MemoryDocumentRepository,
    MemoryResetSettledDream,
)
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
    history_entries: int
    documents: int
    versions: int
    dream_runs: int
    prepare_runs: int
    snapshots: int
    episodes: int
    jobs_cancelled: int
    affected_project_ids: tuple[uuid.UUID, ...]
    settled_dreams: tuple[MemoryResetSettledDream, ...]


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
        memory = MemoryDocumentRepository(self.session)
        authority_project_ids = await memory.lock_owner_projects(str(user_id))
        current = await self.read_memory(user_id, for_update=True)
        if current.version != expected_version:
            raise AccountPersonalizationConflict
        owner_user_id = str(user_id)
        try:
            reset = await memory.reset_owner(
                owner_user_id,
                now=now,
                authority_project_ids=authority_project_ids,
            )
        except MemoryDocumentConflict:
            raise AccountPersonalizationConflict from None
        row = await self.session.scalar(select(UserRow).where(UserRow.id == owner_user_id).with_for_update(of=UserRow))
        if row is None:
            raise AccountPersonalizationNotFound
        row.preferences_version = current.version + 1
        await self.session.flush()
        return AccountMemoryResetCounts(
            version=int(row.preferences_version),
            scopes_reset=reset.scopes_reset,
            history_entries=reset.history_entries,
            documents=reset.documents,
            versions=reset.versions,
            dream_runs=reset.dream_runs,
            prepare_runs=reset.prepare_runs,
            snapshots=reset.snapshots,
            episodes=reset.episodes,
            jobs_cancelled=reset.jobs_cancelled,
            affected_project_ids=reset.affected_project_ids,
            settled_dreams=reset.settled_dreams,
        )


__all__ = [
    "AccountMemoryPreference",
    "AccountMemoryResetCounts",
    "AccountPersonalizationConflict",
    "AccountPersonalizationNotFound",
    "AccountPersonalizationRepository",
]
