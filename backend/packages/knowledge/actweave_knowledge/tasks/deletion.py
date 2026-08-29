"""Delete-task handlers and the project purge helper.

Deletion is object-store-first: MinIO objects are removed before the rows
that reference them, so a failure can only ever leave rows pointing at
already-deleted objects (retried later), never orphaned objects without rows.
All handlers are idempotent — a resource that is already gone settles the
claim as a successful no-op.
"""

from __future__ import annotations

import logging
import sys
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..contracts import KNOWLEDGE_STORAGE_UNAVAILABLE, KnowledgeError
from ..persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeQueryRow,
    KnowledgeTaskRow,
)
from ..storage import MinioObjectStore
from .worker import KnowledgeTaskClaim

logger = logging.getLogger(__name__)

_DELETE_BATCH_SIZE = 50


def _storage_unavailable() -> KnowledgeError:
    """Public storage error; logs the database exception being mapped, if any."""

    if sys.exc_info()[0] is not None:
        logger.warning("knowledge database operation failed", exc_info=True)
    return KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用")


async def _drain_documents(
    session_factory: async_sessionmaker[AsyncSession],
    object_store: MinioObjectStore,
    document_filter: Any,
    *,
    batch_size: int = _DELETE_BATCH_SIZE,
) -> None:
    """Delete objects then rows for every document matching ``document_filter``.

    Batched so one enormous Base cannot hold a transaction open for the whole
    deletion; each batch commits row deletions only after its objects are gone.
    """

    while True:
        try:
            async with session_factory() as session:
                rows = (await session.execute(select(KnowledgeDocumentRow.id, KnowledgeDocumentRow.storage_key).where(document_filter).order_by(KnowledgeDocumentRow.id).limit(batch_size))).all()
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        if not rows:
            return
        for _, storage_key in rows:
            await object_store.delete(storage_key)
        try:
            async with session_factory() as session, session.begin():
                await session.execute(delete(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id.in_([document_id for document_id, _ in rows])))
        except SQLAlchemyError:
            raise _storage_unavailable() from None


class KnowledgeDocumentDeletionHandler:
    """Process one ``delete_document`` claim: object first, then the row."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        object_store: MinioObjectStore,
    ) -> None:
        self._session_factory = session_factory
        self._object_store = object_store

    async def __call__(self, claim: KnowledgeTaskClaim) -> None:
        try:
            async with self._session_factory() as session:
                row = (await session.execute(select(KnowledgeDocumentRow.storage_key, KnowledgeDocumentRow.status).where(KnowledgeDocumentRow.id == claim.resource_id))).one_or_none()
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        if row is None:
            return  # already deleted — idempotent success
        storage_key, status = row
        if status != "deleting":
            return  # stale task for a document that was never marked
        await self._object_store.delete(storage_key)
        try:
            async with self._session_factory() as session, session.begin():
                document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == claim.resource_id, KnowledgeDocumentRow.status == "deleting").with_for_update())
                if document is not None:
                    await session.delete(document)  # segments cascade
        except SQLAlchemyError:
            raise _storage_unavailable() from None


class KnowledgeBaseDeletionHandler:
    """Process one ``delete_knowledge_base`` claim: drain documents, drop the base."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        object_store: MinioObjectStore,
    ) -> None:
        self._session_factory = session_factory
        self._object_store = object_store

    async def __call__(self, claim: KnowledgeTaskClaim) -> None:
        try:
            async with self._session_factory() as session:
                status = await session.scalar(select(KnowledgeBaseRow.status).where(KnowledgeBaseRow.id == claim.resource_id))
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        if status is None or status != "deleting":
            return  # already deleted or never marked — idempotent no-op
        await _drain_documents(
            self._session_factory,
            self._object_store,
            KnowledgeDocumentRow.knowledge_base_id == claim.resource_id,
        )
        try:
            async with self._session_factory() as session, session.begin():
                base = await session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.id == claim.resource_id, KnowledgeBaseRow.status == "deleting").with_for_update())
                if base is not None:
                    await session.delete(base)
        except SQLAlchemyError:
            raise _storage_unavailable() from None


async def purge_project_knowledge(
    session_factory: async_sessionmaker[AsyncSession],
    object_store: MinioObjectStore,
    *,
    project_id: UUID,
    batch_size: int = _DELETE_BATCH_SIZE,
) -> None:
    """Delete every Knowledge resource of a project; idempotent.

    Objects and document rows go first (reusing the Base-deletion drain), then
    bases and task rows. Raises ``KNOWLEDGE_STORAGE_UNAVAILABLE`` when either
    store fails, leaving the remainder for the caller's retry.
    """

    await _drain_documents(
        session_factory,
        object_store,
        KnowledgeDocumentRow.project_id == project_id,
        batch_size=batch_size,
    )
    try:
        async with session_factory() as session, session.begin():
            await session.execute(delete(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id))
            await session.execute(delete(KnowledgeTaskRow).where(KnowledgeTaskRow.project_id == project_id))
            await session.execute(delete(KnowledgeQueryRow).where(KnowledgeQueryRow.project_id == project_id))
    except SQLAlchemyError:
        raise _storage_unavailable() from None
