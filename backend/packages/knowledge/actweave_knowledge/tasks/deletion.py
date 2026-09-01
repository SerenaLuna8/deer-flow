"""Delete-task handlers and the project purge helper.

Deletion first withdraws user-visible Segment relationships and publication
pointers while retaining byte-authority rows. Registered objects are then
deleted and confirmed absent before quota release and authority-row removal, so
uncertainty leaves a durable retry fact. All handlers are idempotent — a
resource that is already gone settles the exact claim as a successful no-op.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..contracts import KNOWLEDGE_STORAGE_UNAVAILABLE, KNOWLEDGE_TASK_FAILED, KnowledgeError
from ..persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeExtractionRow,
    KnowledgeQueryRow,
    KnowledgeSegmentAttachmentRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from ..persistence.tasks import TASK_OPEN_STATUSES, recover_expired_tasks
from ..storage import MinioObjectStore, is_document_storage_key
from ..storage.quota import KnowledgeStorageQuotaPort
from .extraction_deletion import ParentCleanupGuard, delete_registered_extraction
from .worker import KnowledgeProjectInactive, KnowledgeTaskClaim, ProjectActiveCheck

if TYPE_CHECKING:
    from ..project_retention import ProjectCleanupCheck

logger = logging.getLogger(__name__)

_DELETE_BATCH_SIZE = 50
# Project deletion is admitted only after 30 days in the host, while the MinIO
# SDK's normal request window is measured in minutes. One day is deliberately
# much larger than expected transfer/retry settlement and much smaller than
# retention. PostgreSQL time below is authoritative, so host-clock drift cannot
# classify a live upload as stale.
_UPLOAD_SETTLEMENT_GRACE = timedelta(days=1)
DocumentCleanupGuard = Callable[[AsyncSession, UUID, UUID], Awaitable[None]]


def _storage_unavailable() -> KnowledgeError:
    """Public storage error; logs the database exception being mapped, if any."""

    if sys.exc_info()[0] is not None:
        logger.warning("knowledge database operation failed", exc_info=True)
    return KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用")


async def _lock_delete_claim(
    session: AsyncSession,
    *,
    project_active_check: ProjectActiveCheck,
    claim: KnowledgeTaskClaim,
    kind: str,
) -> KnowledgeTaskRow:
    """Lock Project then the exact live deletion claim."""

    expects_storage_key = kind == "delete_document_object"
    if claim.kind != kind or claim.target_version is not None or claim.reparse_settings is not None or (expects_storage_key and claim.storage_key is None) or (not expects_storage_key and claim.storage_key is not None):
        raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 删除任务身份已失效")
    if not await project_active_check(session, claim.project_id):
        raise KnowledgeProjectInactive()
    task = await session.scalar(
        select(KnowledgeTaskRow)
        .where(
            KnowledgeTaskRow.id == claim.id,
            KnowledgeTaskRow.project_id == claim.project_id,
            KnowledgeTaskRow.resource_id == claim.resource_id,
            KnowledgeTaskRow.kind == kind,
            KnowledgeTaskRow.target_version.is_(None),
            (KnowledgeTaskRow.storage_key == claim.storage_key if expects_storage_key else KnowledgeTaskRow.storage_key.is_(None)),
            KnowledgeTaskRow.status == "running",
            KnowledgeTaskRow.claim_token == claim.claim_token,
            KnowledgeTaskRow.attempt_count == claim.attempt_count,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = await session.scalar(select(func.clock_timestamp()))
    if task is None or task.max_attempts != claim.max_attempts or task.lease_until is None or task.lease_until <= now:
        raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 删除任务租约已失效")
    return task


async def _lock_document_scope(
    session: AsyncSession,
    *,
    project_active_check: ProjectActiveCheck,
    project_id: UUID,
    document_id: UUID,
    claim: KnowledgeTaskClaim | None,
    parent_guard: DocumentCleanupGuard | None,
) -> tuple[KnowledgeBaseRow, KnowledgeDocumentRow] | None:
    """Lock Project -> Task -> Base -> Document for one deleting Document."""

    if claim is not None and parent_guard is not None:
        raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 删除父级身份冲突")
    if claim is not None:
        await _lock_delete_claim(
            session,
            project_active_check=project_active_check,
            claim=claim,
            kind="delete_document",
        )
    elif parent_guard is not None:
        await parent_guard(session, project_id, document_id)
    elif not await project_active_check(session, project_id):
        raise KnowledgeProjectInactive()
    discovered_base_id = await session.scalar(
        select(KnowledgeDocumentRow.knowledge_base_id).where(
            KnowledgeDocumentRow.project_id == project_id,
            KnowledgeDocumentRow.id == document_id,
        )
    )
    if discovered_base_id is None:
        return None
    base = await session.scalar(
        select(KnowledgeBaseRow)
        .where(
            KnowledgeBaseRow.project_id == project_id,
            KnowledgeBaseRow.id == discovered_base_id,
        )
        .with_for_update()
    )
    document = await session.scalar(
        select(KnowledgeDocumentRow)
        .where(
            KnowledgeDocumentRow.project_id == project_id,
            KnowledgeDocumentRow.knowledge_base_id == discovered_base_id,
            KnowledgeDocumentRow.id == document_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if base is None or document is None:
        raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 删除作用域已变更")
    if document.status != "deleting":
        return None
    return base, document


async def _withdraw_document_publication(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_active_check: ProjectActiveCheck,
    project_id: UUID,
    document_id: UUID,
    claim: KnowledgeTaskClaim | None,
    parent_guard: DocumentCleanupGuard | None,
) -> bool:
    """Quiesce a Document and withdraw every published relationship."""

    async with session_factory() as session, session.begin():
        if claim is not None and parent_guard is not None:
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 删除父级身份冲突")
        if claim is not None:
            await _lock_delete_claim(
                session,
                project_active_check=project_active_check,
                claim=claim,
                kind="delete_document",
            )
        elif parent_guard is not None:
            await parent_guard(session, project_id, document_id)
        elif not await project_active_check(session, project_id):
            raise KnowledgeProjectInactive()
        base_id = await session.scalar(
            select(KnowledgeDocumentRow.knowledge_base_id).where(
                KnowledgeDocumentRow.project_id == project_id,
                KnowledgeDocumentRow.id == document_id,
            )
        )
        if base_id is None:
            return False
        extraction_ids = select(KnowledgeExtractionRow.id).where(
            KnowledgeExtractionRow.project_id == project_id,
            KnowledgeExtractionRow.knowledge_document_id == document_id,
        )
        open_tasks = (
            await session.scalars(
                select(KnowledgeTaskRow)
                .where(
                    KnowledgeTaskRow.project_id == project_id,
                    KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES),
                    or_(
                        KnowledgeTaskRow.resource_id == document_id,
                        KnowledgeTaskRow.resource_id.in_(extraction_ids),
                        KnowledgeTaskRow.extraction_id.in_(extraction_ids),
                    ),
                    KnowledgeTaskRow.id != (claim.id if claim is not None else UUID(int=0)),
                )
                .order_by(KnowledgeTaskRow.id)
                .with_for_update()
            )
        ).all()
        if any(task.status == "running" for task in open_tasks):
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Document 仍有任务正在执行")
        if open_tasks:
            await session.execute(delete(KnowledgeTaskRow).where(KnowledgeTaskRow.id.in_([task.id for task in open_tasks])))
        base = await session.scalar(
            select(KnowledgeBaseRow)
            .where(
                KnowledgeBaseRow.project_id == project_id,
                KnowledgeBaseRow.id == base_id,
            )
            .with_for_update()
        )
        document = await session.scalar(
            select(KnowledgeDocumentRow)
            .where(
                KnowledgeDocumentRow.project_id == project_id,
                KnowledgeDocumentRow.knowledge_base_id == base_id,
                KnowledgeDocumentRow.id == document_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if base is None or document is None:
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 删除作用域已变更")
        if document.status != "deleting":
            return False
        if document.upload_state == "pending":
            raise KnowledgeError(
                KNOWLEDGE_TASK_FAILED,
                "Document 上传仍在结算，请稍后重试删除",
            )
        if not is_document_storage_key(
            document.storage_key,
            project_id=project_id,
            document_id=document_id,
        ):
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Document 存储标识不合法")
        await session.execute(
            delete(KnowledgeSegmentAttachmentRow).where(
                KnowledgeSegmentAttachmentRow.project_id == project_id,
                KnowledgeSegmentAttachmentRow.knowledge_document_id == document_id,
            )
        )
        await session.execute(
            delete(KnowledgeSegmentRow).where(
                KnowledgeSegmentRow.project_id == project_id,
                KnowledgeSegmentRow.knowledge_document_id == document_id,
            )
        )
        document.published_extraction_id = None
    return True


async def _drain_document_extractions(
    session_factory: async_sessionmaker[AsyncSession],
    object_store: MinioObjectStore,
    quota: KnowledgeStorageQuotaPort,
    *,
    project_active_check: ProjectActiveCheck,
    project_id: UUID,
    document_id: UUID,
    claim: KnowledgeTaskClaim | None,
    parent_guard: DocumentCleanupGuard | None,
) -> None:
    """Delete each registered Extraction after publication withdrawal."""

    while True:
        async with session_factory() as session, session.begin():
            scope = await _lock_document_scope(
                session,
                project_active_check=project_active_check,
                project_id=project_id,
                document_id=document_id,
                claim=claim,
                parent_guard=parent_guard,
            )
            if scope is None:
                return
            extraction = await session.scalar(
                select(KnowledgeExtractionRow)
                .where(
                    KnowledgeExtractionRow.project_id == project_id,
                    KnowledgeExtractionRow.knowledge_document_id == document_id,
                )
                .order_by(KnowledgeExtractionRow.id)
                .limit(1)
                .with_for_update()
            )
            if extraction is None:
                return
            extraction.state = "deleting"
            extraction_id = extraction.id
        extraction_parent_guard: ParentCleanupGuard | None = None
        if claim is not None:

            async def extraction_parent_guard(
                session: AsyncSession,
                guard_project_id: UUID,
                guard_extraction_id: UUID,
            ) -> None:
                del guard_extraction_id
                if guard_project_id != project_id:
                    raise KnowledgeError(
                        KNOWLEDGE_TASK_FAILED,
                        "Document 删除父级作用域已变更",
                    )
                await _lock_delete_claim(
                    session,
                    project_active_check=project_active_check,
                    claim=claim,
                    kind="delete_document",
                )

        elif parent_guard is not None:

            async def extraction_parent_guard(
                session: AsyncSession,
                guard_project_id: UUID,
                guard_extraction_id: UUID,
            ) -> None:
                del guard_extraction_id
                await parent_guard(session, guard_project_id, document_id)

        await delete_registered_extraction(
            session_factory=session_factory,
            object_store=object_store,
            quota=quota,
            project_active_check=project_active_check,
            project_id=project_id,
            extraction_id=extraction_id,
            allow_published=True,
            parent_guard=extraction_parent_guard,
        )


async def _delete_registered_document(
    session_factory: async_sessionmaker[AsyncSession],
    object_store: MinioObjectStore,
    quota: KnowledgeStorageQuotaPort,
    *,
    project_active_check: ProjectActiveCheck,
    project_id: UUID,
    document_id: UUID,
    claim: KnowledgeTaskClaim | None,
    parent_guard: DocumentCleanupGuard | None = None,
) -> bool:
    """Withdraw, drain derived bytes, then delete the exact source object."""

    if not await _withdraw_document_publication(
        session_factory,
        project_active_check=project_active_check,
        project_id=project_id,
        document_id=document_id,
        claim=claim,
        parent_guard=parent_guard,
    ):
        return False
    await _drain_document_extractions(
        session_factory,
        object_store,
        quota,
        project_active_check=project_active_check,
        project_id=project_id,
        document_id=document_id,
        claim=claim,
        parent_guard=parent_guard,
    )
    async with session_factory() as session, session.begin():
        scope = await _lock_document_scope(
            session,
            project_active_check=project_active_check,
            project_id=project_id,
            document_id=document_id,
            claim=claim,
            parent_guard=parent_guard,
        )
        if scope is None:
            return False
        document = scope[1]
        if await session.scalar(select(KnowledgeExtractionRow.id).where(KnowledgeExtractionRow.knowledge_document_id == document_id).limit(1)) is not None:
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Document 提取结果尚未清理完成")
        storage_key = document.storage_key
        if not is_document_storage_key(
            storage_key,
            project_id=project_id,
            document_id=document_id,
        ):
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Document 存储标识不合法")
        document.upload_state = "delete_pending"
    await object_store.delete(storage_key)
    await object_store.require_absent(storage_key)
    async with session_factory() as session, session.begin():
        scope = await _lock_document_scope(
            session,
            project_active_check=project_active_check,
            project_id=project_id,
            document_id=document_id,
            claim=claim,
            parent_guard=parent_guard,
        )
        if scope is None:
            return False
        document = scope[1]
        if document.storage_key != storage_key:
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Document 存储作用域已变更")
        document.upload_state = "deleted"
        await quota.release(session, object_id=document.id)
        await session.delete(document)
    return True


class KnowledgeDocumentDeletionHandler:
    """Process one ``delete_document`` claim: object first, then the row."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        object_store: MinioObjectStore,
        quota: KnowledgeStorageQuotaPort,
        project_active_check: ProjectActiveCheck,
    ) -> None:
        self._session_factory = session_factory
        self._object_store = object_store
        self._quota = quota
        self._project_active_check = project_active_check

    async def __call__(self, claim: KnowledgeTaskClaim) -> None:
        try:
            await _delete_registered_document(
                self._session_factory,
                self._object_store,
                self._quota,
                project_active_check=self._project_active_check,
                project_id=claim.project_id,
                document_id=claim.resource_id,
                claim=claim,
            )
        except SQLAlchemyError:
            raise _storage_unavailable() from None


class KnowledgeDocumentObjectDeletionHandler:
    """Delete one exact late-upload object, then its optional tombstone row."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        object_store: MinioObjectStore,
        quota: KnowledgeStorageQuotaPort,
        project_active_check: ProjectActiveCheck,
    ) -> None:
        self._session_factory = session_factory
        self._object_store = object_store
        self._quota = quota
        self._project_active_check = project_active_check

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
        try:
            async with self._session_factory() as session, session.begin():
                await _lock_delete_claim(
                    session,
                    project_active_check=self._project_active_check,
                    claim=claim,
                    kind="delete_document_object",
                )
                tombstone = await session.scalar(
                    select(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.project_id == claim.project_id,
                        KnowledgeDocumentRow.id == claim.resource_id,
                    )
                    .with_for_update()
                )
                if tombstone is not None:
                    if tombstone.storage_key != storage_key or tombstone.status != "deleting":
                        raise KnowledgeError(
                            KNOWLEDGE_TASK_FAILED,
                            "对象清理任务的 Document 作用域已变更",
                        )
                    tombstone.upload_state = "delete_pending"
            await self._object_store.delete(storage_key)
            await self._object_store.require_absent(storage_key)
            async with self._session_factory() as session, session.begin():
                await _lock_delete_claim(
                    session,
                    project_active_check=self._project_active_check,
                    claim=claim,
                    kind="delete_document_object",
                )
                tombstone = await session.scalar(
                    select(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.project_id == claim.project_id,
                        KnowledgeDocumentRow.id == claim.resource_id,
                    )
                    .with_for_update()
                )
                if tombstone is not None:
                    if tombstone.storage_key != storage_key or tombstone.status != "deleting":
                        raise KnowledgeError(
                            KNOWLEDGE_TASK_FAILED,
                            "对象清理任务的 Document 作用域已变更",
                        )
                    tombstone.upload_state = "deleted"
                    await self._quota.release(session, object_id=tombstone.id)
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
        quota: KnowledgeStorageQuotaPort,
        project_active_check: ProjectActiveCheck,
    ) -> None:
        self._session_factory = session_factory
        self._object_store = object_store
        self._quota = quota
        self._project_active_check = project_active_check

    async def __call__(self, claim: KnowledgeTaskClaim) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await _lock_delete_claim(
                    session,
                    project_active_check=self._project_active_check,
                    claim=claim,
                    kind="delete_knowledge_base",
                )
                document_ids = select(KnowledgeDocumentRow.id).where(
                    KnowledgeDocumentRow.project_id == claim.project_id,
                    KnowledgeDocumentRow.knowledge_base_id == claim.resource_id,
                )
                extraction_ids = select(KnowledgeExtractionRow.id).where(
                    KnowledgeExtractionRow.project_id == claim.project_id,
                    KnowledgeExtractionRow.knowledge_base_id == claim.resource_id,
                )
                other_tasks = (
                    await session.scalars(
                        select(KnowledgeTaskRow)
                        .where(
                            KnowledgeTaskRow.project_id == claim.project_id,
                            KnowledgeTaskRow.id != claim.id,
                            KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES),
                            or_(
                                KnowledgeTaskRow.resource_id.in_(document_ids),
                                KnowledgeTaskRow.resource_id.in_(extraction_ids),
                                KnowledgeTaskRow.extraction_id.in_(extraction_ids),
                            ),
                        )
                        .order_by(KnowledgeTaskRow.id)
                        .with_for_update()
                    )
                ).all()
                if any(task.status == "running" for task in other_tasks):
                    raise KnowledgeError(
                        KNOWLEDGE_TASK_FAILED,
                        "Knowledge Base 仍有任务正在执行",
                    )
                if other_tasks:
                    await session.execute(delete(KnowledgeTaskRow).where(KnowledgeTaskRow.id.in_([task.id for task in other_tasks])))
                base = await session.scalar(
                    select(KnowledgeBaseRow)
                    .where(
                        KnowledgeBaseRow.project_id == claim.project_id,
                        KnowledgeBaseRow.id == claim.resource_id,
                    )
                    .with_for_update()
                )
                if base is None or base.status != "deleting":
                    return
                documents = (
                    await session.scalars(
                        select(KnowledgeDocumentRow)
                        .where(
                            KnowledgeDocumentRow.project_id == claim.project_id,
                            KnowledgeDocumentRow.knowledge_base_id == claim.resource_id,
                        )
                        .order_by(KnowledgeDocumentRow.id)
                        .with_for_update()
                    )
                ).all()
                for document in documents:
                    if document.status != "deleting":
                        document.status = "deleting"
                        document.version += 1
                    document.error_message = None
                pending_document_ids = [document.id for document in documents]
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        await self._object_store.require_unversioned_bucket()

        async def base_parent_guard(
            session: AsyncSession,
            guard_project_id: UUID,
            guard_document_id: UUID,
        ) -> None:
            if guard_project_id != claim.project_id:
                raise KnowledgeError(
                    KNOWLEDGE_TASK_FAILED,
                    "Knowledge Base 删除父级作用域已变更",
                )
            await _lock_delete_claim(
                session,
                project_active_check=self._project_active_check,
                claim=claim,
                kind="delete_knowledge_base",
            )
            base_id = await session.scalar(
                select(KnowledgeDocumentRow.knowledge_base_id).where(
                    KnowledgeDocumentRow.project_id == guard_project_id,
                    KnowledgeDocumentRow.id == guard_document_id,
                )
            )
            if base_id != claim.resource_id:
                raise KnowledgeError(
                    KNOWLEDGE_TASK_FAILED,
                    "Knowledge Base 删除 Document 作用域已变更",
                )

        for document_id in pending_document_ids:
            await _delete_registered_document(
                self._session_factory,
                self._object_store,
                self._quota,
                project_active_check=self._project_active_check,
                project_id=claim.project_id,
                document_id=document_id,
                claim=None,
                parent_guard=base_parent_guard,
            )
        try:
            async with self._session_factory() as session, session.begin():
                await _lock_delete_claim(
                    session,
                    project_active_check=self._project_active_check,
                    claim=claim,
                    kind="delete_knowledge_base",
                )
                base = await session.scalar(
                    select(KnowledgeBaseRow)
                    .where(
                        KnowledgeBaseRow.project_id == claim.project_id,
                        KnowledgeBaseRow.id == claim.resource_id,
                        KnowledgeBaseRow.status == "deleting",
                    )
                    .with_for_update()
                )
                if base is not None:
                    remaining_document = await session.scalar(
                        select(KnowledgeDocumentRow.id)
                        .where(
                            KnowledgeDocumentRow.project_id == claim.project_id,
                            KnowledgeDocumentRow.knowledge_base_id == claim.resource_id,
                        )
                        .limit(1)
                    )
                    if remaining_document is not None:
                        raise KnowledgeError(
                            KNOWLEDGE_TASK_FAILED,
                            "Knowledge Base 文档尚未清理完成",
                        )
                    await session.delete(base)
        except SQLAlchemyError:
            raise _storage_unavailable() from None


async def purge_project_knowledge(
    session_factory: async_sessionmaker[AsyncSession],
    object_store: MinioObjectStore,
    *,
    quota: KnowledgeStorageQuotaPort,
    project_cleanup_check: ProjectCleanupCheck,
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

    if type(batch_size) is not int or not 1 <= batch_size <= _DELETE_BATCH_SIZE:
        raise ValueError("Knowledge deletion batch size must be between 1 and 50")

    if await _has_recent_pending_uploads(
        session_factory,
        project_cleanup_check=project_cleanup_check,
        project_id=project_id,
    ):
        return False

    if not await _prepare_project_task_quiescence(
        session_factory,
        project_cleanup_check=project_cleanup_check,
        project_id=project_id,
    ):
        return False

    if await _defer_or_recover_uploads(
        session_factory,
        project_cleanup_check=project_cleanup_check,
        project_id=project_id,
    ):
        # This attempt performs no object or row deletion. Even stale rows are
        # only converted to deleting + exact cleanup work; the next retention
        # attempt owns the object-first purge.
        return False

    await object_store.require_unversioned_bucket()
    while True:
        try:
            async with session_factory() as session, session.begin():
                if not await project_cleanup_check(session, project_id):
                    return False
                (await session.scalars(select(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id).order_by(KnowledgeBaseRow.id).with_for_update())).all()
                documents = (await session.scalars(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id).order_by(KnowledgeDocumentRow.id).limit(batch_size).with_for_update())).all()
                if not documents:
                    break
                for document in documents:
                    if document.status != "deleting":
                        document.status = "deleting"
                        document.version += 1
                    document.error_message = None
                document_ids = [document.id for document in documents]
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        for document_id in document_ids:
            await _delete_registered_document(
                session_factory,
                object_store,
                quota,
                project_active_check=project_cleanup_check,
                project_id=project_id,
                document_id=document_id,
                claim=None,
            )
    # Rows are the normal object authority, but a put/delete race can leave a
    # late object after its row disappeared. The database-issued Project prefix
    # is the final retention boundary for those object-only remnants.
    await object_store.delete_project_objects(project_id)
    try:
        async with session_factory() as session, session.begin():
            if not await project_cleanup_check(session, project_id):
                return False
            remaining_document = await session.scalar(select(KnowledgeDocumentRow.id).where(KnowledgeDocumentRow.project_id == project_id).limit(1))
            remaining_extraction = await session.scalar(select(KnowledgeExtractionRow.id).where(KnowledgeExtractionRow.project_id == project_id).limit(1))
            if remaining_document is not None or remaining_extraction is not None:
                return False
            await session.execute(delete(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id))
            await session.execute(delete(KnowledgeTaskRow).where(KnowledgeTaskRow.project_id == project_id))
            await session.execute(delete(KnowledgeQueryRow).where(KnowledgeQueryRow.project_id == project_id))
    except SQLAlchemyError:
        raise _storage_unavailable() from None
    return True


async def _has_recent_pending_uploads(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_cleanup_check: ProjectCleanupCheck,
    project_id: UUID,
) -> bool:
    """Preflight unsettled source PUTs before touching Tasks or other rows."""

    try:
        async with session_factory() as session, session.begin():
            if not await project_cleanup_check(session, project_id):
                return True
            database_now = await session.scalar(select(func.now()))
            if database_now is None:  # pragma: no cover - PostgreSQL invariant
                raise _storage_unavailable()
            recent = await session.scalar(
                select(KnowledgeDocumentRow.id)
                .where(
                    KnowledgeDocumentRow.project_id == project_id,
                    KnowledgeDocumentRow.upload_state == "pending",
                    KnowledgeDocumentRow.updated_at > database_now - _UPLOAD_SETTLEMENT_GRACE,
                )
                .order_by(KnowledgeDocumentRow.id)
                .limit(1)
                .with_for_update()
            )
            return recent is not None
    except KnowledgeError:
        raise
    except SQLAlchemyError:
        raise _storage_unavailable() from None


async def _prepare_project_task_quiescence(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_cleanup_check: ProjectCleanupCheck,
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
            if not await project_cleanup_check(session, project_id):
                return False
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
    project_cleanup_check: ProjectCleanupCheck,
    project_id: UUID,
) -> bool:
    """Defer live uploads and durably convert crash-stale uploads.

    Returns true whenever an uploading row existed, including after converting
    stale rows. The caller must therefore delay all object/row deletion until a
    later Project-retention attempt.
    """

    try:
        async with session_factory() as session, session.begin():
            if not await project_cleanup_check(session, project_id):
                return True
            database_now = await session.scalar(select(func.now()))
            if database_now is None:  # pragma: no cover - PostgreSQL invariant
                raise _storage_unavailable()
            rows = (
                await session.scalars(
                    select(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.project_id == project_id,
                        or_(
                            KnowledgeDocumentRow.status == "uploading",
                            KnowledgeDocumentRow.upload_state == "pending",
                        ),
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
                if row.upload_state == "pending":
                    row.upload_state = "delete_pending"
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
