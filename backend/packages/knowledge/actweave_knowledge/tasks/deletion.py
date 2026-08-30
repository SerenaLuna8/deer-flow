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
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..contracts import KNOWLEDGE_STORAGE_UNAVAILABLE, KNOWLEDGE_TASK_FAILED, KnowledgeError
from ..persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeQueryRow,
    KnowledgeTaskRow,
)
from ..persistence.tasks import TASK_OPEN_STATUSES, recover_expired_tasks
from ..storage import MinioObjectStore, is_document_storage_key
from .worker import KnowledgeTaskClaim

logger = logging.getLogger(__name__)

_DELETE_BATCH_SIZE = 50
# Project deletion is admitted only after 30 days in the host, while the MinIO
# SDK's normal request window is measured in minutes. One day is deliberately
# much larger than expected transfer/retry settlement and much smaller than
# retention. PostgreSQL time below is authoritative, so host-clock drift cannot
# classify a live upload as stale.
_UPLOAD_SETTLEMENT_GRACE = timedelta(days=1)


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
        await object_store.delete_many([storage_key for _, storage_key in rows])
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
                row = (
                    await session.execute(
                        select(
                            KnowledgeDocumentRow.storage_key,
                            KnowledgeDocumentRow.status,
                        ).where(
                            KnowledgeDocumentRow.project_id == claim.project_id,
                            KnowledgeDocumentRow.id == claim.resource_id,
                        )
                    )
                ).one_or_none()
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        if row is None:
            return
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


class KnowledgeDocumentObjectDeletionHandler:
    """Delete one exact late-upload object, then its optional tombstone row."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        object_store: MinioObjectStore,
    ) -> None:
        self._session_factory = session_factory
        self._object_store = object_store

    async def __call__(self, claim: KnowledgeTaskClaim) -> None:
        storage_key = claim.storage_key
        if storage_key is None:  # schema-constrained; defensive for forged tests/callers
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "对象清理任务缺少存储标识")
        if not is_document_storage_key(
            storage_key,
            project_id=claim.project_id,
            document_id=claim.resource_id,
        ):
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "对象清理任务的存储标识不合法")
        await self._object_store.delete(storage_key)
        try:
            async with self._session_factory() as session, session.begin():
                tombstone = await session.scalar(
                    select(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.project_id == claim.project_id,
                        KnowledgeDocumentRow.id == claim.resource_id,
                        KnowledgeDocumentRow.storage_key == storage_key,
                        KnowledgeDocumentRow.status == "deleting",
                    )
                    .with_for_update()
                )
                if tombstone is not None:
                    await session.delete(tombstone)
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
        await self._object_store.require_unversioned_bucket()
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
) -> bool:
    """Delete every Knowledge resource of a project; idempotent.

    Live task handlers defer the whole attempt after paused queue work is
    removed. Recent uploads likewise defer; crash-stale uploads first become
    deleting exact-key work and delay one attempt. Otherwise objects and
    document rows go first (reusing the Base-deletion drain), then bases and
    task rows. Returns whether cleanup completed; raises
    ``KNOWLEDGE_STORAGE_UNAVAILABLE`` when either store fails, leaving the
    remainder for the caller's retry.
    """

    if not await _prepare_project_task_quiescence(
        session_factory,
        project_id=project_id,
    ):
        return False

    if await _defer_or_recover_uploads(
        session_factory,
        project_id=project_id,
    ):
        # This attempt performs no object or row deletion. Even stale rows are
        # only converted to deleting + exact cleanup work; the next retention
        # attempt owns the object-first purge.
        return False

    await object_store.require_unversioned_bucket()
    await _drain_documents(
        session_factory,
        object_store,
        KnowledgeDocumentRow.project_id == project_id,
        batch_size=batch_size,
    )
    # Rows are the normal object authority, but a put/delete race can leave a
    # late object after its row disappeared. The database-issued Project prefix
    # is the final retention boundary for those object-only remnants.
    await object_store.delete_project_objects(project_id)
    try:
        async with session_factory() as session, session.begin():
            await session.execute(delete(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id))
            await session.execute(delete(KnowledgeTaskRow).where(KnowledgeTaskRow.project_id == project_id))
            await session.execute(delete(KnowledgeQueryRow).where(KnowledgeQueryRow.project_id == project_id))
    except SQLAlchemyError:
        raise _storage_unavailable() from None
    return True


async def _prepare_project_task_quiescence(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
) -> bool:
    """Fence Project purge behind live handlers and discard paused work.

    Project deletion admission makes every later claim return to retry_wait in
    the same transaction. Locking all open rows here therefore closes the race:
    an already-running handler defers this purge attempt, while queued/retry
    work can be removed before object and relationship deletion begins.
    """

    try:
        async with session_factory() as session, session.begin():
            database_now = await session.scalar(select(func.clock_timestamp()))
            if database_now is None:  # pragma: no cover - PostgreSQL invariant
                raise _storage_unavailable()
            await recover_expired_tasks(
                session,
                project_id=project_id,
                now=database_now,
            )
            open_tasks = (
                await session.scalars(
                    select(KnowledgeTaskRow)
                    .where(
                        KnowledgeTaskRow.project_id == project_id,
                        KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES),
                    )
                    .order_by(KnowledgeTaskRow.id)
                    .with_for_update()
                )
            ).all()
            has_running = any(task.status == "running" for task in open_tasks)
            paused_ids = [task.id for task in open_tasks if task.status != "running"]
            if paused_ids:
                await session.execute(
                    delete(KnowledgeTaskRow).where(
                        KnowledgeTaskRow.id.in_(paused_ids),
                    )
                )
            return not has_running
    except SQLAlchemyError:
        raise _storage_unavailable() from None


async def _defer_or_recover_uploads(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
) -> bool:
    """Defer live uploads and durably convert crash-stale uploads.

    Returns true whenever an uploading row existed, including after converting
    stale rows. The caller must therefore delay all object/row deletion until a
    later Project-retention attempt.
    """

    try:
        async with session_factory() as session, session.begin():
            database_now = await session.scalar(select(func.now()))
            if database_now is None:  # pragma: no cover - PostgreSQL invariant
                raise _storage_unavailable()
            rows = (
                await session.scalars(
                    select(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.project_id == project_id,
                        KnowledgeDocumentRow.status == "uploading",
                    )
                    .order_by(KnowledgeDocumentRow.id)
                    .with_for_update()
                )
            ).all()
            if not rows:
                return False
            stale_before = database_now - _UPLOAD_SETTLEMENT_GRACE
            for row in rows:
                if row.updated_at > stale_before:
                    continue
                row.status = "deleting"
                row.version = row.version + 1
                row.updated_at = database_now
                open_cleanup = await session.scalar(
                    select(KnowledgeTaskRow.id).where(
                        KnowledgeTaskRow.kind == "delete_document_object",
                        KnowledgeTaskRow.storage_key == row.storage_key,
                        KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES),
                    )
                )
                if open_cleanup is None:
                    session.add(
                        KnowledgeTaskRow(
                            id=uuid4(),
                            project_id=project_id,
                            resource_id=row.id,
                            kind="delete_document_object",
                            target_version=None,
                            storage_key=row.storage_key,
                            status="queued",
                        )
                    )
            return True
    except KnowledgeError:
        raise
    except SQLAlchemyError:
        raise _storage_unavailable() from None
