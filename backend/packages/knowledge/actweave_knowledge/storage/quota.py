"""Host-owned storage accounting, inside the caller's object-fact transaction."""

from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


class KnowledgeStorageQuotaPort(Protocol):
    async def reserve(self, session: AsyncSession, *, project_id: UUID, object_id: UUID, size_bytes: int) -> None: ...

    async def commit(self, session: AsyncSession, *, object_id: UUID) -> None: ...

    async def release(self, session: AsyncSession, *, object_id: UUID) -> None: ...
