"""Durable, scoped deletion of one registered Extraction closure."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..contracts import KNOWLEDGE_CONFLICT, KNOWLEDGE_STORAGE_UNAVAILABLE, KNOWLEDGE_TASK_FAILED, KnowledgeError
from ..persistence.models import (
    KnowledgeAttachmentRow,
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeExtractionRow,
    KnowledgeSegmentAttachmentRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from ..persistence.tasks import TASK_OPEN_STATUSES
from ..storage.extraction_keys import is_extraction_storage_key
from ..storage.minio_store import MinioObjectStore
from ..storage.quota import KnowledgeStorageQuotaPort
from .worker import KnowledgeProjectInactive, KnowledgeTaskClaim, ProjectActiveCheck

logger = logging.getLogger(__name__)

_SAFE_DELETE_ERROR = "提取结果对象删除失败"
ParentCleanupGuard = Callable[[AsyncSession, UUID, UUID], Awaitable[None]]


def _conflict() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_CONFLICT, "提取结果清理作用域已变更")


def _storage_unavailable() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "提取结果存储清理失败，请稍后重试")


def _registered_manifest_fact(row: KnowledgeExtractionRow) -> bool:
    unregistered = row.manifest_storage_key is None and row.manifest_sha256 is None and row.manifest_size_bytes == 0 and row.manifest_upload_state == "pending" and row.manifest_quota_state == "unreserved"
    if unregistered:
        return False
    registered = (
        row.manifest_storage_key is not None
        and row.manifest_sha256 is not None
        and ((row.manifest_quota_state == "reserved" and row.manifest_upload_state in {"pending", "delete_pending"}) or (row.manifest_quota_state == "committed" and row.manifest_upload_state in {"stored", "delete_pending"}))
    )
    if not registered:
        raise _conflict()
    return True


@dataclass(frozen=True, slots=True)
class _DeletionScope:
    project_id: UUID
    base_id: UUID
    document_id: UUID
    extraction_id: UUID
    manifest_key: str | None


async def _lock_scope(
    session: AsyncSession,
    *,
    project_active_check: ProjectActiveCheck,
    project_id: UUID,
    extraction_id: UUID,
    claim: KnowledgeTaskClaim | None,
    allow_published: bool,
    parent_guard: ParentCleanupGuard | None,
) -> _DeletionScope | None:
    if not await project_active_check(session, project_id):
        raise KnowledgeProjectInactive()
    if claim is not None and parent_guard is not None:
        raise _conflict()
    if parent_guard is not None:
        await parent_guard(session, project_id, extraction_id)
    if claim is not None:
        if (
            claim.project_id,
            claim.resource_id,
            claim.kind,
            claim.target_version,
            claim.storage_key,
            claim.reparse_settings,
        ) != (project_id, extraction_id, "delete_extraction", None, None, None):
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 任务身份已失效")
        task = await session.scalar(
            select(KnowledgeTaskRow)
            .where(
                KnowledgeTaskRow.id == claim.id,
                KnowledgeTaskRow.project_id == project_id,
                KnowledgeTaskRow.resource_id == extraction_id,
                KnowledgeTaskRow.kind == "delete_extraction",
                KnowledgeTaskRow.target_version.is_(None),
                KnowledgeTaskRow.storage_key.is_(None),
                KnowledgeTaskRow.status == "running",
                KnowledgeTaskRow.claim_token == claim.claim_token,
                KnowledgeTaskRow.attempt_count == claim.attempt_count,
            )
            .with_for_update()
        )
        now = await session.scalar(select(func.clock_timestamp()))
        if task is None or task.max_attempts != claim.max_attempts or task.lease_until is None or task.lease_until <= now:
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 任务租约已失效")
    elif not allow_published:
        raise _conflict()

    discovered = await session.get(KnowledgeExtractionRow, extraction_id)
    if discovered is None:
        return None
    if discovered.project_id != project_id:
        raise _conflict()
    await session.scalar(
        select(KnowledgeBaseRow)
        .where(
            KnowledgeBaseRow.id == discovered.knowledge_base_id,
            KnowledgeBaseRow.project_id == project_id,
        )
        .with_for_update()
    )
    document = await session.scalar(
        select(KnowledgeDocumentRow)
        .where(
            KnowledgeDocumentRow.id == discovered.knowledge_document_id,
            KnowledgeDocumentRow.project_id == project_id,
            KnowledgeDocumentRow.knowledge_base_id == discovered.knowledge_base_id,
        )
        .with_for_update()
    )
    extraction = await session.scalar(
        select(KnowledgeExtractionRow)
        .where(
            KnowledgeExtractionRow.id == extraction_id,
            KnowledgeExtractionRow.project_id == project_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if document is None or extraction is None:
        raise _conflict()
    if document.published_extraction_id == extraction_id:
        raise _conflict()
    pin = await session.scalar(
        select(KnowledgeTaskRow.id)
        .where(
            KnowledgeTaskRow.extraction_id == extraction_id,
            KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES),
        )
        .limit(1)
    )
    if pin is not None:
        raise _conflict()
    if extraction.state != "deleting":
        raise _conflict()
    has_binding = await session.scalar(select(KnowledgeSegmentAttachmentRow.attachment_id).where(KnowledgeSegmentAttachmentRow.extraction_id == extraction_id).limit(1))
    has_segment = await session.scalar(select(KnowledgeSegmentRow.id).where(KnowledgeSegmentRow.extraction_id == extraction_id).limit(1))
    if has_binding is not None or has_segment is not None:
        raise _conflict()
    return _DeletionScope(
        project_id=project_id,
        base_id=extraction.knowledge_base_id,
        document_id=extraction.knowledge_document_id,
        extraction_id=extraction.id,
        manifest_key=extraction.manifest_storage_key,
    )


async def _record_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_active_check: ProjectActiveCheck,
    project_id: UUID,
    extraction_id: UUID,
    attachment_id: UUID | None,
) -> None:
    try:
        async with session_factory() as session, session.begin():
            await project_active_check(session, project_id)
            row = await session.scalar(
                select(KnowledgeExtractionRow)
                .where(
                    KnowledgeExtractionRow.id == extraction_id,
                    KnowledgeExtractionRow.project_id == project_id,
                )
                .with_for_update()
            )
            if row is not None:
                row.delete_error = _SAFE_DELETE_ERROR
            if attachment_id is not None:
                attachment = await session.scalar(
                    select(KnowledgeAttachmentRow)
                    .where(
                        KnowledgeAttachmentRow.id == attachment_id,
                        KnowledgeAttachmentRow.project_id == project_id,
                        KnowledgeAttachmentRow.extraction_id == extraction_id,
                    )
                    .with_for_update()
                )
                if attachment is not None:
                    attachment.delete_error = _SAFE_DELETE_ERROR
    except SQLAlchemyError:
        logger.warning("knowledge extraction delete failure could not be recorded")


async def delete_registered_extraction(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    object_store: MinioObjectStore,
    quota: KnowledgeStorageQuotaPort,
    project_active_check: ProjectActiveCheck,
    project_id: UUID,
    extraction_id: UUID,
    allow_published: bool = False,
    claim: KnowledgeTaskClaim | None = None,
    parent_guard: ParentCleanupGuard | None = None,
) -> bool:
    """Delete registered bytes before their authority row; missing is success."""

    current_attachment_id: UUID | None = None
    try:
        while True:
            async with session_factory() as session, session.begin():
                scope = await _lock_scope(
                    session,
                    project_active_check=project_active_check,
                    project_id=project_id,
                    extraction_id=extraction_id,
                    claim=claim,
                    allow_published=allow_published,
                    parent_guard=parent_guard,
                )
                if scope is None:
                    return False
                attachment = await session.scalar(select(KnowledgeAttachmentRow).where(KnowledgeAttachmentRow.extraction_id == extraction_id).order_by(KnowledgeAttachmentRow.id).limit(1).with_for_update())
                if attachment is None:
                    break
                if (
                    attachment.project_id,
                    attachment.knowledge_base_id,
                    attachment.knowledge_document_id,
                ) != (scope.project_id, scope.base_id, scope.document_id):
                    raise _conflict()
                attachment.state = "deleting"
                attachment.upload_state = "delete_pending"
                attachment_id = attachment.id
                current_attachment_id = attachment.id
                attachment_key = attachment.storage_key
            if not is_extraction_storage_key(
                attachment_key,
                project_id=scope.project_id,
                base_id=scope.base_id,
                document_id=scope.document_id,
                extraction_id=scope.extraction_id,
            ):
                raise _conflict()
            await object_store.delete(attachment_key)
            await object_store.require_absent(attachment_key)
            async with session_factory() as session, session.begin():
                locked_scope = await _lock_scope(
                    session,
                    project_active_check=project_active_check,
                    project_id=project_id,
                    extraction_id=extraction_id,
                    claim=claim,
                    allow_published=allow_published,
                    parent_guard=parent_guard,
                )
                if locked_scope != scope:
                    raise _conflict()
                attachment = await session.get(KnowledgeAttachmentRow, attachment_id, with_for_update=True)
                if attachment is None:
                    continue
                if (
                    attachment.project_id,
                    attachment.extraction_id,
                    attachment.storage_key,
                ) != (project_id, extraction_id, attachment_key):
                    raise _conflict()
                attachment.upload_state = "deleted"
                await quota.release(session, object_id=attachment.id)
                await session.delete(attachment)
            current_attachment_id = None

        async with session_factory() as session, session.begin():
            scope = await _lock_scope(
                session,
                project_active_check=project_active_check,
                project_id=project_id,
                extraction_id=extraction_id,
                claim=claim,
                allow_published=allow_published,
                parent_guard=parent_guard,
            )
            if scope is None:
                return False
            row = await session.get(KnowledgeExtractionRow, extraction_id, with_for_update=True)
            if row is None:
                return False
            registered_manifest = _registered_manifest_fact(row)
            if registered_manifest:
                row.manifest_upload_state = "delete_pending"
        if scope.manifest_key is not None:
            if not is_extraction_storage_key(
                scope.manifest_key,
                project_id=scope.project_id,
                base_id=scope.base_id,
                document_id=scope.document_id,
                extraction_id=scope.extraction_id,
            ):
                raise _conflict()
            await object_store.delete(scope.manifest_key)
            await object_store.require_absent(scope.manifest_key)
        async with session_factory() as session, session.begin():
            locked_scope = await _lock_scope(
                session,
                project_active_check=project_active_check,
                project_id=project_id,
                extraction_id=extraction_id,
                claim=claim,
                allow_published=allow_published,
                parent_guard=parent_guard,
            )
            if locked_scope is None:
                return False
            if locked_scope != scope:
                raise _conflict()
            has_attachment = await session.scalar(select(KnowledgeAttachmentRow.id).where(KnowledgeAttachmentRow.extraction_id == extraction_id).limit(1))
            has_binding = await session.scalar(select(KnowledgeSegmentAttachmentRow.attachment_id).where(KnowledgeSegmentAttachmentRow.extraction_id == extraction_id).limit(1))
            has_segment = await session.scalar(select(KnowledgeSegmentRow.id).where(KnowledgeSegmentRow.extraction_id == extraction_id).limit(1))
            if has_attachment is not None or has_binding is not None or has_segment is not None:
                raise _conflict()
            row = await session.get(KnowledgeExtractionRow, extraction_id, with_for_update=True)
            if row is None:
                return False
            if _registered_manifest_fact(row) != registered_manifest:
                raise _conflict()
            if registered_manifest:
                row.manifest_upload_state = "deleted"
                await quota.release(session, object_id=row.id)
            await session.delete(row)
    except KnowledgeProjectInactive:
        raise
    except (KnowledgeError, SQLAlchemyError):
        await _record_failure(
            session_factory,
            project_active_check=project_active_check,
            project_id=project_id,
            extraction_id=extraction_id,
            attachment_id=current_attachment_id,
        )
        raise _storage_unavailable() from None
    return True


class KnowledgeExtractionDeletionHandler:
    """Execute one server-admitted ``delete_extraction`` claim."""

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
        await delete_registered_extraction(
            session_factory=self._session_factory,
            object_store=self._object_store,
            quota=self._quota,
            project_active_check=self._project_active_check,
            project_id=claim.project_id,
            extraction_id=claim.resource_id,
            claim=claim,
        )
