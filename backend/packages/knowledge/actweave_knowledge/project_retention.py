"""Project-retention capability independent from the optional feature runtime."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .contracts import KnowledgeError, KnowledgeSettings
from .persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeQueryRow,
    KnowledgeTaskRow,
)
from .storage import MinioObjectStore
from .tasks import purge_project_knowledge

logger = logging.getLogger(__name__)


def create_knowledge_project_purger(
    *,
    settings: KnowledgeSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> KnowledgeProjectPurger:
    """Build cleanup authority without enabling HTTP, tools, or task workers."""

    return KnowledgeProjectPurger(
        settings=settings,
        session_factory=session_factory,
    )


class KnowledgeProjectPurger:
    """Delete one Project's Knowledge rows and discoverable stored objects."""

    def __init__(
        self,
        *,
        settings: KnowledgeSettings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory
        self._object_store = MinioObjectStore(settings.minio) if settings.minio is not None else None

    async def purge_project(self, project_id: UUID) -> bool:
        """Return true only after every discoverable Project resource is gone.

        A disabled deployment may omit MinIO configuration even though rows
        from an earlier enabled deployment remain. Document rows or unfinished
        exact-key object cleanup prove that bytes may remain, so cleanup fails
        closed until the original storage configuration is restored. A
        succeeded exact-key cleanup is durable proof for that key and does not
        block metadata-only cleanup.
        """

        try:
            if self._object_store is not None:
                return await purge_project_knowledge(
                    self._session_factory,
                    self._object_store,
                    project_id=project_id,
                )

            async with self._session_factory() as session, session.begin():
                remaining_documents = await session.scalar(select(func.count()).select_from(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id))
                object_cleanup_tasks = await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeTaskRow)
                    .where(
                        KnowledgeTaskRow.project_id == project_id,
                        KnowledgeTaskRow.kind == "delete_document_object",
                        KnowledgeTaskRow.status != "succeeded",
                    )
                )
                if int(remaining_documents or 0) > 0 or int(object_cleanup_tasks or 0) > 0:
                    return False
                await session.execute(delete(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id))
                await session.execute(delete(KnowledgeTaskRow).where(KnowledgeTaskRow.project_id == project_id))
                await session.execute(delete(KnowledgeQueryRow).where(KnowledgeQueryRow.project_id == project_id))
                return True
        except (KnowledgeError, SQLAlchemyError):
            logger.warning(
                "knowledge purge for project %s did not complete",
                project_id,
            )
            return False


__all__ = ["KnowledgeProjectPurger", "create_knowledge_project_purger"]
