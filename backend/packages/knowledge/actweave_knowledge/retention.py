"""Transaction-bound retention capabilities for Knowledge-private data."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from .persistence.models import KnowledgeQueryRow


async def purge_knowledge_query_history(
    session: AsyncSession,
    *,
    project_id: UUID,
    owner_user_id: str | None,
) -> None:
    """Delete query text for one exact retention scope without committing.

    ``owner_user_id=None`` is the Project-retention scope.  An owner value is
    the former-owner/account scope and deliberately leaves every Project-shared
    Knowledge Base, Document, Segment, Task, and stored object untouched.
    """

    statement = delete(KnowledgeQueryRow).where(
        KnowledgeQueryRow.project_id == UUID(str(project_id)),
    )
    if owner_user_id is not None:
        statement = statement.where(
            KnowledgeQueryRow.owner_user_id == str(UUID(str(owner_user_id))),
        )
    await session.execute(statement)


__all__ = ["purge_knowledge_query_history"]
