"""Knowledge Document upload, listing, and original-file download.

Upload is a three-step pipeline: persist an ``uploading`` row, write the
object to MinIO, then in one transaction flip the row to ``queued`` and create
the ingest task. An ingest task therefore only ever references an object that
was written successfully; any failure deletes both the object and the
``uploading`` row before the error reaches the caller.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..contracts import (
    KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeChunkingMode,
    KnowledgeDocumentUpload,
    KnowledgeDocumentView,
    KnowledgeError,
    KnowledgeSegmentView,
    KnowledgeSettings,
)
from ..persistence.derivations import delete_error_expression
from ..persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from ..persistence.tasks import TASK_OPEN_STATUSES
from ..storage import MinioObjectStore, document_storage_key

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100

# Frozen by the system requirements; changing this set is a product decision,
# not a configuration knob.
ALLOWED_DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".txt", ".md", ".csv", ".xlsx", ".html", ".htm", ".pptx", ".epub"})

_MAX_NAME_LENGTH = 255
_MAX_MEDIA_TYPE_LENGTH = 255
# RFC 6838 type/subtype with optional parameters; garbage here would otherwise
# surface later as a confusing storage error from the object-store HTTP layer.
_MEDIA_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*(?:\s*;.*)?$")
_CHUNK_SIZE_RANGE = (200, 4000)
_CHUNK_OVERLAP_RANGE = (0, 500)
_CHUNK_SEPARATOR_MAX_LENGTH = 64
_CHILD_CHUNK_SIZE_RANGE = (100, 2000)

_DOWNLOADABLE_STATUSES = frozenset({"queued", "processing", "ready", "failed"})


def _invalid(message: str) -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_INVALID_REQUEST, message)


def _storage_unavailable() -> KnowledgeError:
    """Public storage error; logs the database exception being mapped, if any.

    The public message stays sanitized, so without this server-side log a
    constraint violation or pool exhaustion would be undiagnosable.
    """

    if sys.exc_info()[0] is not None:
        logger.warning("knowledge database operation failed", exc_info=True)
    return KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用")


def _validated_page(page: int, page_size: int) -> tuple[int, int]:
    if type(page) is not int or page < 1:
        raise _invalid("page 必须是不小于 1 的整数")
    if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise _invalid(f"page_size 必须是 1-{MAX_PAGE_SIZE} 之间的整数")
    return page, page_size


def validated_original_name(original_name: str) -> str:
    """Shared by upload and chunk preview: length plus allowed extension."""

    cleaned = original_name.strip()
    if not cleaned or len(cleaned) > _MAX_NAME_LENGTH:
        raise _invalid(f"original_name 必须是 1-{_MAX_NAME_LENGTH} 个字符的非空文本")
    extension = Path(cleaned).suffix.lower()
    if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
        allowed = "、".join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))
        raise _invalid(f"不支持的文件类型 {extension or '(无扩展名)'}，仅支持 {allowed}")
    return cleaned


def validated_upload_size(size_bytes: int, settings: KnowledgeSettings) -> int:
    """Shared by upload and chunk preview: non-empty and within the cap."""

    if type(size_bytes) is not int or size_bytes <= 0:
        raise _invalid("不能上传空文件")
    if size_bytes > settings.upload_max_bytes:
        raise _invalid(f"文件大小超过上限 {settings.upload_max_bytes} 字节")
    return size_bytes


def validated_chunk_settings(chunk_size: int, chunk_overlap: int, chunk_separator: str) -> tuple[int, int, str]:
    """Shared by upload and chunk preview: sizes, overlap, and separator.

    The separator is validated in its escaped form exactly as stored; it is
    never stripped because a separator may legitimately be whitespace.
    """

    low, high = _CHUNK_SIZE_RANGE
    if type(chunk_size) is not int or not low <= chunk_size <= high:
        raise _invalid(f"chunk_size 必须是 {low}-{high} 之间的整数")
    low, high = _CHUNK_OVERLAP_RANGE
    if type(chunk_overlap) is not int or not low <= chunk_overlap <= high or chunk_overlap >= chunk_size:
        raise _invalid(f"chunk_overlap 必须是 {low}-{high} 之间且小于 chunk_size 的整数")
    if type(chunk_separator) is not str or not 1 <= len(chunk_separator) <= _CHUNK_SEPARATOR_MAX_LENGTH:
        raise _invalid(f"chunk_separator 必须是 1-{_CHUNK_SEPARATOR_MAX_LENGTH} 个字符的文本")
    return chunk_size, chunk_overlap, chunk_separator


def validated_preprocessing_rules(remove_extra_spaces: bool, remove_urls_emails: bool) -> tuple[bool, bool]:
    if type(remove_extra_spaces) is not bool or type(remove_urls_emails) is not bool:
        raise _invalid("预处理规则开关必须是布尔值")
    return remove_extra_spaces, remove_urls_emails


def validated_chunking_mode(
    chunking_mode: str,
    child_chunk_size: int,
    child_chunk_separator: str,
    *,
    chunk_size: int,
) -> tuple[KnowledgeChunkingMode, int, str]:
    """Shared by upload and chunk preview: mode plus its child parameters.

    General mode ignores the child inputs and stores the column defaults, so a
    stray client value can never violate the parent_child-only ratio check.
    """

    if chunking_mode not in ("general", "parent_child"):
        raise _invalid("chunking_mode 只能是 general 或 parent_child")
    if chunking_mode == "general":
        return "general", 500, KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR
    low, high = _CHILD_CHUNK_SIZE_RANGE
    if type(child_chunk_size) is not int or not low <= child_chunk_size <= high:
        raise _invalid(f"child_chunk_size 必须是 {low}-{high} 之间的整数")
    if child_chunk_size >= chunk_size:
        raise _invalid("child_chunk_size 必须小于 chunk_size")
    if type(child_chunk_separator) is not str or not 1 <= len(child_chunk_separator) <= _CHUNK_SEPARATOR_MAX_LENGTH:
        raise _invalid(f"child_chunk_separator 必须是 1-{_CHUNK_SEPARATOR_MAX_LENGTH} 个字符的文本")
    return "parent_child", child_chunk_size, child_chunk_separator


def _validated_upload(upload: KnowledgeDocumentUpload, settings: KnowledgeSettings) -> KnowledgeDocumentUpload:
    name = upload.name.strip()
    if not name or len(name) > _MAX_NAME_LENGTH:
        raise _invalid(f"name 必须是 1-{_MAX_NAME_LENGTH} 个字符的非空文本")
    original_name = validated_original_name(upload.original_name)
    media_type = upload.media_type.strip() if upload.media_type else None
    if media_type is not None and (len(media_type) > _MAX_MEDIA_TYPE_LENGTH or not _MEDIA_TYPE_PATTERN.match(media_type)):
        raise _invalid("media_type 不是有效的 MIME 类型")
    size_bytes = validated_upload_size(upload.size_bytes, settings)
    chunk_size, chunk_overlap, chunk_separator = validated_chunk_settings(upload.chunk_size, upload.chunk_overlap, upload.chunk_separator)
    remove_extra_spaces, remove_urls_emails = validated_preprocessing_rules(upload.remove_extra_spaces, upload.remove_urls_emails)
    chunking_mode, child_chunk_size, child_chunk_separator = validated_chunking_mode(
        upload.chunking_mode,
        upload.child_chunk_size,
        upload.child_chunk_separator,
        chunk_size=chunk_size,
    )
    return KnowledgeDocumentUpload(
        name=name,
        original_name=original_name,
        source_path=upload.source_path,
        size_bytes=size_bytes,
        media_type=media_type,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        chunk_separator=chunk_separator,
        remove_extra_spaces=remove_extra_spaces,
        remove_urls_emails=remove_urls_emails,
        chunking_mode=chunking_mode,
        child_chunk_size=child_chunk_size,
        child_chunk_separator=child_chunk_separator,
    )


def document_view(row: KnowledgeDocumentRow, *, delete_error: str | None) -> KnowledgeDocumentView:
    """Package-internal view builder, shared with the segment service."""

    return KnowledgeDocumentView(
        id=row.id,
        project_id=row.project_id,
        knowledge_base_id=row.knowledge_base_id,
        name=row.name,
        original_name=row.original_name,
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        status=row.status,  # type: ignore[arg-type]
        enabled=row.enabled,
        version=row.version,
        chunk_size=row.chunk_size,
        chunk_overlap=row.chunk_overlap,
        chunk_separator=row.chunk_separator,
        remove_extra_spaces=row.remove_extra_spaces,
        remove_urls_emails=row.remove_urls_emails,
        chunking_mode=row.chunking_mode,  # type: ignore[arg-type]
        child_chunk_size=row.child_chunk_size,
        child_chunk_separator=row.child_chunk_separator,
        segment_count=row.segment_count,
        word_count=row.word_count,
        hit_count=row.hit_count,
        doc_metadata=dict(row.doc_metadata),
        error_message=row.error_message,
        delete_error=delete_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _document_with_derivations(project_id: UUID):  # noqa: ANN202 - SQLAlchemy select
    return select(
        KnowledgeDocumentRow,
        delete_error_expression("delete_document", KnowledgeDocumentRow.id).label("delete_error"),
    ).where(KnowledgeDocumentRow.project_id == project_id)


class KnowledgeDocumentService:
    """Upload, list, read, and download documents scoped to one project."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: KnowledgeSettings,
        object_store: MinioObjectStore,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._object_store = object_store

    async def upload_document(
        self,
        project_id: UUID,
        base_id: UUID,
        upload: KnowledgeDocumentUpload,
    ) -> KnowledgeDocumentView:
        validated = _validated_upload(upload, self._settings)
        document_id = uuid4()
        storage_key = document_storage_key(project_id, base_id, document_id, validated.original_name)

        await self._create_uploading_row(project_id, base_id, document_id, storage_key, validated)
        try:
            await self._object_store.upload_from(
                storage_key,
                validated.source_path,
                media_type=validated.media_type,
            )
            return await self._publish_queued_document(project_id, document_id)
        except BaseException:
            # Cancellation (client disconnect) and unexpected bugs must roll
            # back exactly like KnowledgeError; the shield keeps a second
            # cancellation from interrupting the cleanup itself.
            await asyncio.shield(self._cleanup_failed_upload(project_id, document_id, storage_key))
            raise

    async def _create_uploading_row(
        self,
        project_id: UUID,
        base_id: UUID,
        document_id: UUID,
        storage_key: str,
        validated: KnowledgeDocumentUpload,
    ) -> None:
        """Reserve the document under the base lock: status gate, quota, row insert."""

        try:
            async with self._session_factory() as session, session.begin():
                base = await session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id, KnowledgeBaseRow.id == base_id).with_for_update())
                if base is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
                if base.status != "active":
                    raise _invalid("仅 active 状态的 Knowledge Base 接受上传")
                document_count = await session.scalar(select(func.count()).select_from(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == base_id))
                if int(document_count or 0) >= self._settings.max_documents_per_knowledge_base:
                    raise KnowledgeError(
                        KNOWLEDGE_QUOTA_EXCEEDED,
                        f"Knowledge Base 内 Document 数量已达上限 {self._settings.max_documents_per_knowledge_base}",
                    )
                session.add(
                    KnowledgeDocumentRow(
                        id=document_id,
                        project_id=project_id,
                        knowledge_base_id=base_id,
                        name=validated.name,
                        original_name=validated.original_name,
                        storage_key=storage_key,
                        media_type=validated.media_type,
                        size_bytes=validated.size_bytes,
                        status="uploading",
                        version=1,
                        chunk_size=validated.chunk_size,
                        chunk_overlap=validated.chunk_overlap,
                        chunk_separator=validated.chunk_separator,
                        remove_extra_spaces=validated.remove_extra_spaces,
                        remove_urls_emails=validated.remove_urls_emails,
                        chunking_mode=validated.chunking_mode,
                        child_chunk_size=validated.child_chunk_size,
                        child_chunk_separator=validated.child_chunk_separator,
                    )
                )
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def _publish_queued_document(self, project_id: UUID, document_id: UUID) -> KnowledgeDocumentView:
        """Flip the row to ``queued`` and create the ingest task in one transaction."""

        try:
            async with self._session_factory() as session, session.begin():
                row = await session.get(KnowledgeDocumentRow, document_id, with_for_update=True)
                if row is None:  # pragma: no cover - row was created moments ago
                    raise _storage_unavailable()
                row.status = "queued"
                row.updated_at = func.now()  # type: ignore[assignment]
                session.add(
                    KnowledgeTaskRow(
                        id=uuid4(),
                        project_id=project_id,
                        resource_id=document_id,
                        kind="ingest_document",
                        target_version=row.version,
                        status="queued",
                    )
                )
                await session.flush()
                await session.refresh(row)
                return document_view(row, delete_error=None)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def _cleanup_failed_upload(self, project_id: UUID, document_id: UUID, storage_key: str) -> None:
        """Roll back a failed upload without ever orphaning the object.

        The object delete is attempted unconditionally: a put whose response
        was lost may have durably written the object, and deleting an absent
        key already succeeds. The row is removed only after the object is
        confirmed gone (objects before rows); when the object delete fails,
        the pair is handed to the retrying delete worker instead. Never masks
        the caller's original error.
        """

        try:
            await self._object_store.delete(storage_key)
        except KnowledgeError:
            # Log the document id, not the storage key: object keys are
            # storage locators and must stay out of logs.
            logger.warning("failed-upload object delete failed, deferring to delete task: %s", document_id)
            await self._enqueue_delete_for_failed_upload(project_id, document_id)
            return
        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(delete(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == document_id))
        except SQLAlchemyError:
            logger.warning("uploading document row left behind after failed upload: %s", document_id, exc_info=True)

    async def _enqueue_delete_for_failed_upload(self, project_id: UUID, document_id: UUID) -> None:
        """Flip the leftover ``uploading`` row to ``deleting`` with a delete task."""

        try:
            async with self._session_factory() as session, session.begin():
                row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == document_id).with_for_update())
                if row is None:
                    return
                row.status = "deleting"
                row.updated_at = func.now()  # type: ignore[assignment]
                session.add(_delete_task(project_id, document_id, "delete_document"))
        except SQLAlchemyError:
            logger.warning("uploading document row left behind after failed upload: %s", document_id, exc_info=True)

    async def list_documents(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[KnowledgeDocumentView], int]:
        page, page_size = _validated_page(page, page_size)
        try:
            async with self._session_factory() as session:
                base_exists = await session.scalar(select(KnowledgeBaseRow.id).where(KnowledgeBaseRow.project_id == project_id, KnowledgeBaseRow.id == base_id))
                if base_exists is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
                total = await session.scalar(select(func.count()).select_from(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == base_id))
                rows = await session.execute(
                    _document_with_derivations(project_id)
                    .where(KnowledgeDocumentRow.knowledge_base_id == base_id)
                    .order_by(KnowledgeDocumentRow.created_at.desc(), KnowledgeDocumentRow.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                views = [document_view(row, delete_error=delete_error) for row, delete_error in rows.all()]
                return views, int(total or 0)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def get_document(self, project_id: UUID, document_id: UUID) -> KnowledgeDocumentView:
        row, delete_error = await self._load_document(project_id, document_id)
        return document_view(row, delete_error=delete_error)

    async def download_document(self, project_id: UUID, document_id: UUID, target_path: Path) -> KnowledgeDocumentView:
        """Fetch the original file into ``target_path`` and return the document view."""

        row, delete_error = await self._load_document(project_id, document_id)
        if row.status not in _DOWNLOADABLE_STATUSES:
            raise _invalid("文档当前状态不支持下载")
        await self._object_store.download_to(row.storage_key, target_path)
        return document_view(row, delete_error=delete_error)

    async def retry_document(self, project_id: UUID, document_id: UUID) -> KnowledgeDocumentView:
        """Re-queue a failed document under a new version, in one transaction.

        The version bump makes any still-running old ingest a late result: its
        publish sees a version mismatch and settles as a no-op.
        """

        try:
            async with self._session_factory() as session, session.begin():
                row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id).with_for_update())
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                if row.status != "failed":
                    raise _invalid("仅 failed 状态的文档支持重试")
                base_status = await session.scalar(select(KnowledgeBaseRow.status).where(KnowledgeBaseRow.id == row.knowledge_base_id))
                if base_status != "active":
                    raise _invalid("所属 Knowledge Base 不是 active 状态，不能重试")
                row.version = row.version + 1
                row.status = "queued"
                # The old version's segments are dead the moment the version
                # bumps; a stale count would contradict the segment browser.
                row.segment_count = 0
                row.word_count = 0
                row.error_message = None
                row.updated_at = func.now()  # type: ignore[assignment]
                session.add(
                    KnowledgeTaskRow(
                        id=uuid4(),
                        project_id=project_id,
                        resource_id=row.id,
                        kind="ingest_document",
                        target_version=row.version,
                        status="queued",
                    )
                )
                await session.flush()
                await session.refresh(row)
                delete_error = await self._derived_delete_error(session, row.id)
                return document_view(row, delete_error=delete_error)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def rename_document(self, project_id: UUID, document_id: UUID, name: str) -> KnowledgeDocumentView:
        """Change the display ``name``; ``original_name`` and the object stay."""

        cleaned = name.strip() if isinstance(name, str) else ""
        if not cleaned or len(cleaned) > _MAX_NAME_LENGTH:
            raise _invalid(f"name 必须是 1-{_MAX_NAME_LENGTH} 个字符的非空文本")
        try:
            async with self._session_factory() as session, session.begin():
                row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id).with_for_update())
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                if row.status == "deleting":
                    raise _invalid("删除中的文档不支持重命名")
                row.name = cleaned
                row.updated_at = func.now()  # type: ignore[assignment]
                await session.flush()
                await session.refresh(row)
                delete_error = await self._derived_delete_error(session, row.id)
                return document_view(row, delete_error=delete_error)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def set_documents_enabled(
        self,
        project_id: UUID,
        document_ids: list[UUID],
        enabled: bool,
    ) -> list[KnowledgeDocumentView]:
        """Flip retrieval visibility for the batch, all-or-nothing.

        Disabling keeps segments and vectors intact; re-enabling restores
        retrievability immediately. Documents being deleted reject the batch.
        """

        ids = _validated_document_ids(document_ids)
        if type(enabled) is not bool:
            raise _invalid("enabled 必须是布尔值")
        try:
            async with self._session_factory() as session, session.begin():
                rows = (await session.scalars(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id.in_(ids)).order_by(KnowledgeDocumentRow.id).with_for_update())).all()
                by_id = {row.id: row for row in rows}
                if len(by_id) != len(ids):
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                for row in rows:
                    if row.status == "deleting":
                        raise _invalid("删除中的文档不支持启停")
                for row in rows:
                    if row.enabled != enabled:
                        row.enabled = enabled
                        row.updated_at = func.now()  # type: ignore[assignment]
                await session.flush()
                return await self._batch_views(session, project_id, ids)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def delete_documents(self, project_id: UUID, document_ids: list[UUID]) -> list[KnowledgeDocumentView]:
        """Mark the batch ``deleting`` with delete tasks, all-or-nothing."""

        ids = _validated_document_ids(document_ids)
        try:
            async with self._session_factory() as session, session.begin():
                rows = (await session.scalars(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id.in_(ids)).order_by(KnowledgeDocumentRow.id).with_for_update())).all()
                by_id = {row.id: row for row in rows}
                if len(by_id) != len(ids):
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                for row in rows:
                    await _mark_document_deleting(session, project_id, row)
                await session.flush()
                return await self._batch_views(session, project_id, ids)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def _batch_views(
        self,
        session: AsyncSession,
        project_id: UUID,
        ids: list[UUID],
    ) -> list[KnowledgeDocumentView]:
        """Views in request order, re-read so server-side ``now()`` is resolved."""

        result = await session.execute(_document_with_derivations(project_id).where(KnowledgeDocumentRow.id.in_(ids)).execution_options(populate_existing=True))
        view_by_id = {row.id: document_view(row, delete_error=delete_error) for row, delete_error in result.all()}
        return [view_by_id[document_id] for document_id in ids]

    async def list_document_segments(
        self,
        project_id: UUID,
        document_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[KnowledgeSegmentView], int]:
        """Published segments of the document's current version, by position."""

        page, page_size = _validated_page(page, page_size)
        try:
            async with self._session_factory() as session:
                document = (await session.execute(select(KnowledgeDocumentRow.id, KnowledgeDocumentRow.version).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id))).one_or_none()
                if document is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                _, current_version = document
                segment_filter = (
                    KnowledgeSegmentRow.knowledge_document_id == document_id,
                    KnowledgeSegmentRow.document_version == current_version,
                )
                total = await session.scalar(select(func.count()).select_from(KnowledgeSegmentRow).where(*segment_filter))
                rows = await session.scalars(select(KnowledgeSegmentRow).where(*segment_filter).order_by(KnowledgeSegmentRow.position, KnowledgeSegmentRow.id).offset((page - 1) * page_size).limit(page_size))
                views = [
                    KnowledgeSegmentView(
                        id=segment.id,
                        document_version=segment.document_version,
                        position=segment.position,
                        content=segment.content,
                        word_count=segment.word_count,
                        enabled=segment.enabled,
                        hit_count=segment.hit_count,
                        source_position=dict(segment.source_position),
                        created_at=segment.created_at,
                    )
                    for segment in rows.all()
                ]
                return views, int(total or 0)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def delete_document(self, project_id: UUID, document_id: UUID) -> KnowledgeDocumentView:
        """Mark the document ``deleting`` and ensure one open delete task exists.

        Calling delete again after a finally-failed deletion creates a fresh
        task; while a delete task is open the view's ``delete_error`` is null.
        """

        try:
            async with self._session_factory() as session, session.begin():
                row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id).with_for_update())
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                await _mark_document_deleting(session, project_id, row)
                await session.flush()
                await session.refresh(row)
                delete_error = await self._derived_delete_error(session, row.id)
                return document_view(row, delete_error=delete_error)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def _derived_delete_error(self, session: AsyncSession, document_id: UUID) -> str | None:
        return await session.scalar(select(delete_error_expression("delete_document", KnowledgeDocumentRow.id)).where(KnowledgeDocumentRow.id == document_id))

    async def _load_document(self, project_id: UUID, document_id: UUID) -> tuple[KnowledgeDocumentRow, str | None]:
        try:
            async with self._session_factory() as session:
                result = (await session.execute(_document_with_derivations(project_id).where(KnowledgeDocumentRow.id == document_id))).one_or_none()
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        if result is None:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
        row, delete_error = result
        return row, delete_error


_MAX_BATCH_DOCUMENTS = 100


def _validated_document_ids(document_ids: list[UUID]) -> list[UUID]:
    """Deduplicate while preserving request order; bound the batch size."""

    ids = list(dict.fromkeys(document_ids))
    if not ids:
        raise _invalid("document_ids 不能为空")
    if len(ids) > _MAX_BATCH_DOCUMENTS:
        raise _invalid(f"一次最多操作 {_MAX_BATCH_DOCUMENTS} 个文档")
    return ids


async def _mark_document_deleting(session: AsyncSession, project_id: UUID, row: KnowledgeDocumentRow) -> None:
    """Flip one locked row to ``deleting`` and ensure one open delete task exists."""

    if row.status != "deleting":
        # The version bump turns any in-flight ingest into a late result
        # that will not publish.
        row.status = "deleting"
        row.version = row.version + 1
        row.error_message = None
        row.updated_at = func.now()  # type: ignore[assignment]
        session.add(_delete_task(project_id, row.id, "delete_document"))
    elif not await _open_delete_task_exists(session, "delete_document", row.id):
        session.add(_delete_task(project_id, row.id, "delete_document"))


def _delete_task(project_id: UUID, resource_id: UUID, kind: str) -> KnowledgeTaskRow:
    return KnowledgeTaskRow(
        id=uuid4(),
        project_id=project_id,
        resource_id=resource_id,
        kind=kind,
        target_version=None,
        status="queued",
    )


async def _open_delete_task_exists(session: AsyncSession, kind: str, resource_id: UUID) -> bool:
    open_task = await session.scalar(
        select(KnowledgeTaskRow.id).where(
            KnowledgeTaskRow.kind == kind,
            KnowledgeTaskRow.resource_id == resource_id,
            KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES),
        )
    )
    return open_task is not None
