"""Knowledge Document upload, listing, and original-file download.

Upload is a three-step pipeline: persist an ``uploading`` row, write the
object to MinIO, then in one transaction flip the row to ``queued`` and create
the ingest task. An ingest task therefore only ever references an object that
was written successfully. Failed writes retain their reservation until object
absence is confirmed; uncertain cleanup leaves an exact-object tombstone.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..asyncio_utils import run_sync_to_completion
from ..authority import KnowledgeProjectAuthority, revalidate_project_authority
from ..contracts import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeChunkingMode,
    KnowledgeChunkPreviewRequest,
    KnowledgeDocumentAttachmentView,
    KnowledgeDocumentUpload,
    KnowledgeDocumentView,
    KnowledgeError,
    KnowledgeReparsePreview,
    KnowledgeReparseRequest,
    KnowledgeSegmentView,
    KnowledgeSettings,
    KnowledgeTaskProgress,
)
from ..extraction.contracts import ParseWarning, ProcessingProfile, SourceSpan
from ..extraction.registry import default_registry
from ..extraction.runtime import ParserSlots
from ..persistence.derivations import document_delete_error_expression
from ..persistence.models import (
    KnowledgeAttachmentRow,
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from ..persistence.tasks import TASK_OPEN_STATUSES, VERSIONED_TASK_KINDS, validated_reparse_settings
from ..storage import DOCUMENT_STORAGE_EXTENSIONS, MinioObjectStore, document_storage_key
from ..storage.quota import KnowledgeStorageQuotaPort
from ..tasks.worker import KnowledgeProjectInactive, ProjectActiveCheck

if TYPE_CHECKING:
    from ..ingestion.profiles import FileCapabilities, ProcessingParameters

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100

# Frozen by the system requirements; changing this set is a product decision,
# not a configuration knob.
ALLOWED_DOCUMENT_EXTENSIONS = DOCUMENT_STORAGE_EXTENSIONS

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


def _processing_parameters(request: KnowledgeDocumentUpload | KnowledgeReparseRequest) -> ProcessingParameters:
    from ..ingestion.profiles import ProcessingParameters

    if request.processing_profile is not None:
        return ProcessingParameters.model_validate(request.processing_profile)
    return ProcessingParameters(
        size=request.chunk_size,
        overlap=request.chunk_overlap,
        separator=request.chunk_separator,
        mode=request.chunking_mode,
        child_size=request.child_chunk_size,
        child_separator=request.child_chunk_separator,
        remove_extra_spaces=request.remove_extra_spaces,
        remove_urls_emails=request.remove_urls_emails,
    )


def _apply_processing_parameters(request: KnowledgeDocumentUpload | KnowledgeReparseRequest) -> KnowledgeDocumentUpload | KnowledgeReparseRequest:
    from ..ingestion.profiles import ProcessingParameters

    if request.processing_profile is None:
        return request
    try:
        p = ProcessingParameters.model_validate(request.processing_profile)
        return replace(
            request,
            chunk_size=p.size,
            chunk_overlap=p.overlap,
            chunk_separator=p.separator,
            chunking_mode=p.mode,
            child_chunk_size=p.child_size,
            child_chunk_separator=p.child_separator,
            remove_extra_spaces=p.remove_extra_spaces,
            remove_urls_emails=p.remove_urls_emails,
        )
    except ValidationError:
        raise _invalid("分段参数无效") from None


def _validated_upload(upload: KnowledgeDocumentUpload, settings: KnowledgeSettings) -> KnowledgeDocumentUpload:
    upload = _apply_processing_parameters(upload)
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
        processing_profile=upload.processing_profile,
        expected_preview_fingerprint=upload.expected_preview_fingerprint,
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


def _source_digest(upload: KnowledgeDocumentUpload) -> str:
    """Hash actual admitted bytes before registering any quota fact."""
    digest = hashlib.sha256()
    size = 0
    try:
        with upload.source_path.open("rb") as source:
            while block := source.read(65536):
                size += len(block)
                if size > upload.size_bytes:
                    raise _invalid("实际文件大小与上传声明不一致")
                digest.update(block)
    except OSError:
        raise _invalid("无法读取上传文件") from None
    if size != upload.size_bytes:
        raise _invalid("实际文件大小与上传声明不一致")
    return digest.hexdigest()


def _validated_reparse(request: KnowledgeReparseRequest) -> KnowledgeReparseRequest:
    """Full parameter validation before anything is frozen or downloaded."""

    request = _apply_processing_parameters(request)
    if type(request.expected_version) is not int or request.expected_version < 1:
        raise _invalid("expected_version 必须是不小于 1 的整数")
    chunk_size, chunk_overlap, chunk_separator = validated_chunk_settings(request.chunk_size, request.chunk_overlap, request.chunk_separator)
    remove_extra_spaces, remove_urls_emails = validated_preprocessing_rules(request.remove_extra_spaces, request.remove_urls_emails)
    chunking_mode, child_chunk_size, child_chunk_separator = validated_chunking_mode(
        request.chunking_mode,
        request.child_chunk_size,
        request.child_chunk_separator,
        chunk_size=chunk_size,
    )
    return KnowledgeReparseRequest(
        expected_version=request.expected_version,
        processing_profile=request.processing_profile,
        expected_preview_fingerprint=request.expected_preview_fingerprint,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        chunk_separator=chunk_separator,
        remove_extra_spaces=remove_extra_spaces,
        remove_urls_emails=remove_urls_emails,
        chunking_mode=chunking_mode,
        child_chunk_size=child_chunk_size,
        child_chunk_separator=child_chunk_separator,
    )


# Statuses that accept an explicit re-parse: the document must be settled.
_REPARSABLE_STATUSES = frozenset({"ready", "failed"})


def _remove_temp_dir(path: str | Path) -> None:
    """Best-effort cleanup for a directory created during cancellation."""

    shutil.rmtree(path, ignore_errors=True)


def document_view(
    row: KnowledgeDocumentRow,
    *,
    delete_error: str | None,
    task_progress: KnowledgeTaskProgress | None = None,
) -> KnowledgeDocumentView:
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
        # Derived, never a second stored flag: a manually emptied document
        # stays initialized while a never-published one does not.
        content_initialized=row.published_version is not None,
        task_progress=task_progress,
        parsing_profile=ProcessingProfile.model_validate(row.parsing_profile) if row.parsing_profile else None,
        parse_warnings=tuple(ParseWarning.model_validate(w) for w in row.parse_warnings),
    )


def _document_with_derivations(project_id: UUID):  # noqa: ANN202 - SQLAlchemy select
    return select(
        KnowledgeDocumentRow,
        document_delete_error_expression(KnowledgeDocumentRow.id).label("delete_error"),
    ).where(KnowledgeDocumentRow.project_id == project_id)


def _task_progress_view(row: KnowledgeTaskRow) -> KnowledgeTaskProgress:
    """Safe projection of one indexing task row; no claim, lease, or storage
    material crosses this boundary."""

    return KnowledgeTaskProgress(
        kind=row.kind,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        stage=row.stage,  # type: ignore[arg-type]
        completed_units=row.completed_units,
        total_units=row.total_units,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        target_version=row.target_version if row.target_version is not None else 0,
        next_attempt_at=row.available_at if row.status == "retry_wait" else None,
    )


async def indexing_task_progress(
    session: AsyncSession,
    project_id: UUID,
    version_by_document: dict[UUID, int],
) -> dict[UUID, KnowledgeTaskProgress]:
    """The open (or finally failed) indexing task bound to each document's
    current generation.

    Succeeded tasks and tasks targeting another generation project nothing:
    after a retry bumps the version, the superseded attempt's failure no
    longer describes the document. When one generation carries both a failed
    task and a newer one, the newest row wins.
    """

    if not version_by_document:
        return {}
    rows = (
        await session.execute(
            select(KnowledgeTaskRow).where(
                KnowledgeTaskRow.project_id == project_id,
                KnowledgeTaskRow.kind.in_(VERSIONED_TASK_KINDS),
                KnowledgeTaskRow.resource_id.in_(version_by_document),
            )
        )
    ).scalars()
    newest: dict[UUID, KnowledgeTaskRow] = {}
    for row in rows:
        if row.target_version != version_by_document.get(row.resource_id):
            continue
        current = newest.get(row.resource_id)
        if current is None or (row.created_at, row.id.hex) > (current.created_at, current.id.hex):
            newest[row.resource_id] = row
    return {document_id: _task_progress_view(row) for document_id, row in newest.items() if row.status != "succeeded"}


def _failed_upload_tombstone(project_id: UUID, base_id: UUID, document_id: UUID, storage_key: str, upload: KnowledgeDocumentUpload, source_sha256: str) -> KnowledgeDocumentRow:
    return KnowledgeDocumentRow(
        id=document_id,
        project_id=project_id,
        knowledge_base_id=base_id,
        name=upload.name,
        original_name=upload.original_name,
        storage_key=storage_key,
        media_type=upload.media_type,
        size_bytes=upload.size_bytes,
        source_sha256=source_sha256,
        quota_state="reserved",
        upload_state="pending",
        status="deleting",
        version=2,
        chunk_size=upload.chunk_size,
        chunk_overlap=upload.chunk_overlap,
        chunk_separator=upload.chunk_separator,
        remove_extra_spaces=upload.remove_extra_spaces,
        remove_urls_emails=upload.remove_urls_emails,
        chunking_mode=upload.chunking_mode,
        child_chunk_size=upload.child_chunk_size,
        child_chunk_separator=upload.child_chunk_separator,
    )


async def _latest_indexing_task(session: AsyncSession, document_id: UUID):  # noqa: ANN202 - SQLAlchemy row projection
    return (
        await session.execute(
            select(KnowledgeTaskRow.kind, KnowledgeTaskRow.reparse_settings, KnowledgeTaskRow.status, KnowledgeTaskRow.target_version)
            .where(KnowledgeTaskRow.resource_id == document_id, KnowledgeTaskRow.kind.in_(VERSIONED_TASK_KINDS))
            .order_by(KnowledgeTaskRow.created_at.desc(), KnowledgeTaskRow.id.desc())
            .limit(1)
        )
    ).one_or_none()


class KnowledgeDocumentService:
    """Upload, list, read, and download documents scoped to one project."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: KnowledgeSettings,
        object_store: MinioObjectStore,
        quota: KnowledgeStorageQuotaPort,
        project_active_check: ProjectActiveCheck,
        file_capabilities: Callable[[], FileCapabilities],
        preview_parser_slots: ParserSlots | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._object_store = object_store
        self._quota = quota
        self._project_active_check = project_active_check
        self._file_capabilities = file_capabilities
        self._preview_parser_slots = preview_parser_slots or ParserSlots(1)

    def _require_parsing_capabilities(self) -> FileCapabilities:
        from ..ingestion.profiles import required_file_formats_ready

        capabilities = self._file_capabilities()
        if not required_file_formats_ready(capabilities):
            from ..extraction.contracts import ExtractionError

            raise ExtractionError("PARSER_SANDBOX_UNAVAILABLE", "文件解析暂不可用，请联系管理员")
        return capabilities

    async def upload_document(
        self,
        project_id: UUID,
        base_id: UUID,
        upload: KnowledgeDocumentUpload,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeDocumentView:
        from ..ingestion.profiles import chunk_settings, preview_fingerprint, resolve_processing_profile

        validated = _validated_upload(upload, self._settings)
        source_sha256 = await run_sync_to_completion(_source_digest, validated)
        registry = await run_sync_to_completion(default_registry)
        try:
            profile = await run_sync_to_completion(resolve_processing_profile, self._settings, _processing_parameters(validated), registry, extension=Path(validated.original_name).suffix)
        except ValueError:
            raise _invalid("分段参数无效") from None
        capabilities = self._require_parsing_capabilities()
        if validated.expected_preview_fingerprint is not None and validated.expected_preview_fingerprint != preview_fingerprint(
            source_sha256=source_sha256, extension=Path(validated.original_name).suffix, profile=profile, capability_revision=capabilities.capability_revision
        ):
            raise KnowledgeError(KNOWLEDGE_CONFLICT, "预览已过期，请重新预览")
        validated = replace(validated, **chunk_settings(profile))
        document_id = uuid4()
        storage_key = document_storage_key(project_id, base_id, document_id, validated.original_name)

        await self._create_uploading_row(
            project_id,
            base_id,
            document_id,
            storage_key,
            validated,
            source_sha256=source_sha256,
            profile=profile,
            capability_revision=capabilities.capability_revision,
            authority=authority,
        )
        stored = False
        try:
            uploading = asyncio.create_task(
                self._object_store.upload_from(
                    storage_key,
                    validated.source_path,
                    media_type=validated.media_type,
                )
            )
            cancelled = None
            while True:
                try:
                    # Do not cancel the adapter: its sync bridge otherwise
                    # drains a successful PUT but discards that success when
                    # it propagates cancellation. Retain the physical outcome
                    # for compensation before forwarding the cancellation.
                    await asyncio.shield(uploading)
                    stored = True
                    break
                except asyncio.CancelledError as exc:
                    if uploading.cancelled():
                        raise
                    cancelled = cancelled or exc
            if cancelled is not None:
                raise cancelled
            return await self._publish_queued_document(
                project_id,
                document_id,
                authority=authority,
            )
        except BaseException:
            # Cancellation (client disconnect) and unexpected bugs must roll
            # back exactly like KnowledgeError; the shield keeps a second
            # cancellation from interrupting the cleanup itself.
            cleanup = asyncio.create_task(
                self._cleanup_failed_upload(
                    project_id,
                    base_id,
                    document_id,
                    storage_key,
                    validated,
                    stored=stored,
                    source_sha256=source_sha256,
                )
            )
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    if cleanup.cancelled():
                        raise
            raise

    async def _create_uploading_row(
        self,
        project_id: UUID,
        base_id: UUID,
        document_id: UUID,
        storage_key: str,
        validated: KnowledgeDocumentUpload,
        *,
        source_sha256: str,
        profile: ProcessingProfile,
        capability_revision: str,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> None:
        """Reserve the document under the base lock: status gate, quota, row insert."""

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                if not await self._project_active_check(session, project_id):
                    raise KnowledgeProjectInactive()
                base = await session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id, KnowledgeBaseRow.id == base_id).with_for_update())
                if base is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
                if base.status != "active":
                    raise _invalid("仅 active 状态的 Knowledge Base 接受上传")
                if base.embedding_model_id is None:
                    raise _invalid("请先配置 Knowledge Base 的 Embedding 模型再上传文档")
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
                        source_sha256=source_sha256,
                        parsing_profile=profile.model_dump(mode="json"),
                        capability_revision=capability_revision,
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
                await session.flush()
                await self._quota.reserve(session, project_id=project_id, object_id=document_id, size_bytes=validated.size_bytes)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def _publish_queued_document(
        self,
        project_id: UUID,
        document_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeDocumentView:
        """Flip an unchanged ``uploading`` row to ``queued`` with its task.

        Deletion increments the Document version while the object put is in
        flight.  Requiring the original state here makes that deletion win;
        the caller's rollback path then removes the just-written object.
        """

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                if not await self._project_active_check(session, project_id):
                    raise KnowledgeProjectInactive()
                row = await session.scalar(
                    select(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.project_id == project_id,
                        KnowledgeDocumentRow.id == document_id,
                    )
                    .with_for_update()
                )
                if row is None or row.status != "uploading" or row.version != 1:
                    raise KnowledgeError(KNOWLEDGE_CONFLICT, "Document 上传期间已被删除")
                row.upload_state = "stored"
                await self._quota.commit(session, object_id=row.id)
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

    async def _cleanup_failed_upload(
        self,
        project_id: UUID,
        base_id: UUID,
        document_id: UUID,
        storage_key: str,
        validated: KnowledgeDocumentUpload,
        *,
        stored: bool,
        source_sha256: str,
    ) -> None:
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
            await self._object_store.require_absent(storage_key)
        except KnowledgeError:
            # Log the document id, not the storage key: object keys are
            # storage locators and must stay out of logs.
            logger.warning("failed-upload object delete failed, deferring to delete task: %s", document_id)
            await self._enqueue_delete_for_failed_upload(
                project_id,
                base_id,
                document_id,
                storage_key,
                validated,
                stored=stored,
                source_sha256=source_sha256,
            )
            return
        try:
            async with self._session_factory() as session, session.begin():
                # Compensation still takes the Project fence when inactive;
                # it may release only an existing, confirmed-deleted object.
                await self._project_active_check(session, project_id)
                base = await session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.id == base_id, KnowledgeBaseRow.project_id == project_id).with_for_update())
                row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == document_id, KnowledgeDocumentRow.project_id == project_id).with_for_update())
                if row is None and base is not None:
                    # A concurrent legacy delete may have removed the row
                    # while this PUT was still in flight. Restore only this
                    # admission's exact UUID and already-reserved bytes.
                    row = _failed_upload_tombstone(project_id, base_id, document_id, storage_key, validated, source_sha256)
                    session.add(row)
                    await session.flush()
                if row is not None:
                    row.upload_state = "deleted"
                    await self._quota.release(session, object_id=row.id)
                await session.execute(delete(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == document_id))
        except SQLAlchemyError:
            logger.warning("uploading document row left behind after failed upload: %s", document_id, exc_info=True)

    async def _enqueue_delete_for_failed_upload(
        self,
        project_id: UUID,
        base_id: UUID,
        document_id: UUID,
        storage_key: str,
        validated: KnowledgeDocumentUpload,
        *,
        stored: bool,
        source_sha256: str,
    ) -> None:
        """Persist exact-key cleanup and retain a user-retry tombstone when possible."""

        try:
            async with self._session_factory() as session, session.begin():
                # Lock even for inactive Projects, with no publication rights.
                await self._project_active_check(session, project_id)
                base = await session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.id == base_id, KnowledgeBaseRow.project_id == project_id).with_for_update())
                row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == document_id).with_for_update())
                if row is None:
                    if base is not None:
                        row = _failed_upload_tombstone(project_id, base_id, document_id, storage_key, validated, source_sha256)
                        session.add(row)
                        await session.flush()
                else:
                    if row.status != "deleting":
                        row.version = row.version + 1
                    row.status = "deleting"
                    row.error_message = None
                    row.updated_at = func.now()  # type: ignore[assignment]
                if row is not None:
                    if stored:
                        row.upload_state = "stored"
                        await self._quota.commit(session, object_id=row.id)
                    row.upload_state = "delete_pending"
                if not await _open_delete_task_exists(
                    session,
                    "delete_document_object",
                    document_id,
                ):
                    session.add(
                        _delete_task(
                            project_id,
                            document_id,
                            "delete_document_object",
                            storage_key=storage_key,
                        )
                    )
        except SQLAlchemyError:
            logger.warning("uploading document row left behind after failed upload: %s", document_id, exc_info=True)

    async def list_documents(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> tuple[list[KnowledgeDocumentView], int]:
        page, page_size = _validated_page(page, page_size)
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
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
                page_rows = rows.all()
                progress = await indexing_task_progress(
                    session,
                    project_id,
                    {row.id: row.version for row, _delete_error in page_rows},
                )
                views = [document_view(row, delete_error=delete_error, task_progress=progress.get(row.id)) for row, delete_error in page_rows]
                return views, int(total or 0)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def get_document(
        self,
        project_id: UUID,
        document_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeDocumentView:
        row, delete_error, task_progress = await self._load_document(
            project_id,
            document_id,
            authority=authority,
        )
        return document_view(row, delete_error=delete_error, task_progress=task_progress)

    async def list_document_attachments(
        self,
        project_id: UUID,
        document_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> tuple[list[KnowledgeDocumentAttachmentView], int]:
        """List selectable images from the ready Document's current publication."""

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                document = await session.scalar(
                    select(KnowledgeDocumentRow).where(
                        KnowledgeDocumentRow.project_id == project_id,
                        KnowledgeDocumentRow.id == document_id,
                    )
                )
                if document is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                if document.status != "ready" or document.published_extraction_id is None or document.published_version is None or document.published_version != document.version:
                    raise _invalid("仅 ready 状态且内容为当前发布版本的文档支持选择附件")
                rows = list(
                    (
                        await session.scalars(
                            select(KnowledgeAttachmentRow)
                            .where(
                                KnowledgeAttachmentRow.project_id == project_id,
                                KnowledgeAttachmentRow.knowledge_base_id == document.knowledge_base_id,
                                KnowledgeAttachmentRow.knowledge_document_id == document.id,
                                KnowledgeAttachmentRow.extraction_id == document.published_extraction_id,
                                KnowledgeAttachmentRow.state == "ready",
                                KnowledgeAttachmentRow.upload_state == "stored",
                            )
                            .order_by(KnowledgeAttachmentRow.sha256.asc(), KnowledgeAttachmentRow.id.asc())
                        )
                    ).all()
                )
                return (
                    [
                        KnowledgeDocumentAttachmentView(
                            attachment_id=row.id,
                            ref=row.sha256,
                            media_type=row.media_type,  # type: ignore[arg-type]  # database constraint
                            width=row.width,
                            height=row.height,
                        )
                        for row in rows
                    ],
                    document.version,
                )
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def download_document(
        self,
        project_id: UUID,
        document_id: UUID,
        target_path: Path,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeDocumentView:
        """Fetch the original file into ``target_path`` and return the document view."""

        row, delete_error, _task_progress = await self._load_document(
            project_id,
            document_id,
            authority=authority,
        )
        if row.status not in _DOWNLOADABLE_STATUSES:
            raise _invalid("文档当前状态不支持下载")
        await self._object_store.download_to(row.storage_key, target_path)
        # Object I/O runs outside PostgreSQL. Revalidate in a fresh, short
        # transaction before the copied bytes can be returned by the host.
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        return document_view(row, delete_error=delete_error)

    async def retry_document(
        self,
        project_id: UUID,
        document_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeDocumentView:
        """Re-queue a failed document under a new version, in one transaction.

        The version bump makes any still-running old task a late result: its
        publish sees a version mismatch and settles as a no-op. A manual retry
        inherits the failed operation's semantics — a failed re-embed retries
        as a re-embed (rows and counters survive), never silently escalating
        into a re-parse of the original file.
        """

        from ..ingestion.profiles import validate_frozen_processing_profile

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(authority, session, project_id=project_id)
                snapshot = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id))
                if snapshot is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                if await session.scalar(select(KnowledgeBaseRow.status).where(KnowledgeBaseRow.id == snapshot.knowledge_base_id)) != "active":
                    raise _invalid("所属 Knowledge Base 不是 active 状态，不能重试")
                prior_indexing = await _latest_indexing_task(session, snapshot.id)
            prior_kind = prior_indexing.kind if prior_indexing is not None else "ingest_document"
            if snapshot.status == "failed" and prior_kind == "ingest_document":
                self._require_parsing_capabilities()
                registry = await run_sync_to_completion(default_registry)
                prior_reparse = prior_indexing.reparse_settings if prior_indexing is not None else None
                frozen_profile = validated_reparse_settings(prior_reparse)["processing_profile"] if prior_reparse is not None else snapshot.parsing_profile
                await run_sync_to_completion(validate_frozen_processing_profile, frozen_profile, extension=Path(snapshot.original_name).suffix, registry=registry)
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id).with_for_update())
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                base_status = await session.scalar(select(KnowledgeBaseRow.status).where(KnowledgeBaseRow.id == row.knowledge_base_id))
                if base_status != "active":
                    raise _invalid("所属 Knowledge Base 不是 active 状态，不能重试")
                last_indexing = await _latest_indexing_task(session, row.id)
                if (row.version, row.original_name, row.parsing_profile, row.status) != (snapshot.version, snapshot.original_name, snapshot.parsing_profile, snapshot.status) or last_indexing != prior_indexing:
                    raise KnowledgeError(KNOWLEDGE_CONFLICT, "Document 已变更，请刷新后重试")
                if row.status == "ready":
                    # Summaries and lexical re-derivation never take a document
                    # out of ready, so their failed tasks retry in place.
                    if last_indexing is None or last_indexing.kind not in ("summarize_document", "relex_document") or last_indexing.status != "failed" or last_indexing.target_version != row.version:
                        raise _invalid("仅失败的文档、摘要或词法索引任务支持重试")
                    if await session.scalar(select(KnowledgeTaskRow.id).where(KnowledgeTaskRow.resource_id == row.id, KnowledgeTaskRow.kind.in_(VERSIONED_TASK_KINDS), KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES)).limit(1)):
                        raise _invalid("文档存在未完成的索引任务，暂不能重试")
                    if last_indexing.kind == "summarize_document":
                        summary_enabled = await session.scalar(select(KnowledgeBaseRow.summary_index_enabled).where(KnowledgeBaseRow.id == row.knowledge_base_id))
                        if not summary_enabled:
                            raise _invalid("请先启用知识库的摘要索引")
                    session.add(KnowledgeTaskRow(id=uuid4(), project_id=project_id, resource_id=row.id, kind=last_indexing.kind, target_version=row.version))
                    await session.flush()
                    progress = await indexing_task_progress(session, project_id, {row.id: row.version})
                    return document_view(row, delete_error=await self._derived_delete_error(session, row.id), task_progress=progress.get(row.id))
                if row.status != "failed":
                    raise _invalid("仅 failed 状态的文档支持重试")
                retry_kind, retry_reparse_settings = (last_indexing.kind, last_indexing.reparse_settings) if last_indexing is not None else ("ingest_document", None)
                row.version = row.version + 1
                row.status = "queued"
                if retry_kind == "ingest_document" and row.published_version is None:
                    # Nothing was ever published, so there are no rows for the
                    # counters to describe. A failed re-parse or re-embed keeps
                    # its published rows visible in maintenance views, so their
                    # counters must keep describing them.
                    row.segment_count = 0
                    row.word_count = 0
                row.error_message = None
                row.updated_at = func.now()  # type: ignore[assignment]
                session.add(
                    KnowledgeTaskRow(
                        id=uuid4(),
                        project_id=project_id,
                        resource_id=row.id,
                        kind=retry_kind,
                        target_version=row.version,
                        status="queued",
                        # A failed re-parse retries with the same confirmed
                        # parameters, never silently reverting to the stored ones.
                        reparse_settings=retry_reparse_settings,
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

    async def preview_reparse(
        self,
        project_id: UUID,
        document_id: UUID,
        request: KnowledgeReparseRequest,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeReparsePreview:
        """Read-only re-parse preview computed from the stored original file.

        The server resolves the file from the Document's own storage key —
        never from a client-supplied path — and reuses the upload preview's
        extract → clean → split, so for identical parameters the preview
        matches what a confirmed re-parse would publish. Object I/O and
        parsing run outside PostgreSQL, so authority and the document version
        are re-checked afterwards before the computed preview may leave.
        """

        validated = _validated_reparse(request)
        row, _delete_error, _task_progress = await self._load_document(
            project_id,
            document_id,
            authority=authority,
        )
        if row.status not in _REPARSABLE_STATUSES:
            raise _invalid("仅 ready 或 failed 状态的文档支持重新解析")
        if row.version != validated.expected_version:
            raise KnowledgeError(KNOWLEDGE_CONFLICT, "文档已被其他操作更新，请刷新后重试")

        # Deferred import: ingestion.preview imports this module's validators.
        from ..ingestion.preview import preview_document_chunks

        capabilities = self._require_parsing_capabilities()

        async def guard() -> None:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )

        temp_dir = Path(
            await run_sync_to_completion(
                tempfile.mkdtemp,
                prefix="actweave-knowledge-reparse-",
                cleanup_on_cancel=_remove_temp_dir,
            )
        )
        try:
            source_path = temp_dir / f"source{Path(row.original_name).suffix.lower()}"
            await self._object_store.download_to(row.storage_key, source_path)
            preview = await preview_document_chunks(
                KnowledgeChunkPreviewRequest(
                    original_name=row.original_name,
                    source_path=source_path,
                    size_bytes=row.size_bytes,
                    processing_profile=validated.processing_profile,
                    chunk_size=validated.chunk_size,
                    chunk_overlap=validated.chunk_overlap,
                    chunk_separator=validated.chunk_separator,
                    remove_extra_spaces=validated.remove_extra_spaces,
                    remove_urls_emails=validated.remove_urls_emails,
                    chunking_mode=validated.chunking_mode,
                    child_chunk_size=validated.child_chunk_size,
                    child_chunk_separator=validated.child_chunk_separator,
                ),
                self._settings,
                capability_revision=capabilities.capability_revision,
                parser_slots=self._preview_parser_slots,
                guard=guard,
            )
        finally:
            await run_sync_to_completion(shutil.rmtree, temp_dir, ignore_errors=True)
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                current_version = await session.scalar(select(KnowledgeDocumentRow.version).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id))
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        if current_version != validated.expected_version:
            raise KnowledgeError(KNOWLEDGE_CONFLICT, "文档已被其他操作更新，请刷新后重试")
        return KnowledgeReparsePreview(
            document_version=validated.expected_version,
            preview=preview,
        )

    async def reparse_document(
        self,
        project_id: UUID,
        document_id: UUID,
        request: KnowledgeReparseRequest,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeDocumentView:
        """Queue an explicit re-parse of the original file, in one transaction.

        The confirmed parameters are frozen onto the task's dedicated
        ``reparse_settings``; the document's stored parameter columns are
        replaced only when the new content publishes successfully, so a
        failure never leaves new parameters explaining old rows. This is
        never a model change — the request carries no model field at all.
        """

        from ..ingestion.profiles import chunk_settings, preview_fingerprint, resolve_processing_profile

        validated = _validated_reparse(request)
        snapshot, _, _ = await self._load_document(project_id, document_id, authority=authority)
        if snapshot.version != validated.expected_version:
            raise KnowledgeError(KNOWLEDGE_CONFLICT, "文档已被其他操作更新，请刷新后重试")
        registry = await run_sync_to_completion(default_registry)
        capabilities = self._require_parsing_capabilities()
        try:
            profile = await run_sync_to_completion(resolve_processing_profile, self._settings, _processing_parameters(validated), registry, extension=Path(snapshot.original_name).suffix)
        except ValueError:
            raise _invalid("分段参数无效") from None
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id).with_for_update())
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                if row.status not in _REPARSABLE_STATUSES:
                    raise _invalid("仅 ready 或 failed 状态的文档支持重新解析")
                base_status = await session.scalar(select(KnowledgeBaseRow.status).where(KnowledgeBaseRow.id == row.knowledge_base_id))
                if base_status != "active":
                    raise _invalid("所属 Knowledge Base 不是 active 状态，不能重新解析")
                if row.version != validated.expected_version or row.original_name != snapshot.original_name:
                    raise KnowledgeError(KNOWLEDGE_CONFLICT, "文档已被其他操作更新，请刷新后重试")
                open_indexing = await session.scalar(
                    select(KnowledgeTaskRow.id).where(
                        KnowledgeTaskRow.resource_id == row.id,
                        KnowledgeTaskRow.kind.in_(VERSIONED_TASK_KINDS),
                        KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES),
                    )
                )
                if open_indexing is not None:
                    raise _invalid("文档存在未完成的索引任务，暂不能重新解析")
                if validated.expected_preview_fingerprint is not None:
                    if row.source_sha256 is None or validated.expected_preview_fingerprint != preview_fingerprint(
                        source_sha256=row.source_sha256, extension=Path(row.original_name).suffix, profile=profile, capability_revision=capabilities.capability_revision
                    ):
                        raise KnowledgeError(KNOWLEDGE_CONFLICT, "预览已过期，请重新预览")
                frozen = {**chunk_settings(profile), "processing_profile": profile.model_dump(mode="json"), "capability_revision": capabilities.capability_revision}
                validated_reparse_settings(frozen)
                row.version = row.version + 1
                row.status = "queued"
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
                        reparse_settings=frozen,
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

    async def rename_document(
        self,
        project_id: UUID,
        document_id: UUID,
        name: str,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeDocumentView:
        """Change the display ``name``; ``original_name`` and the object stay."""

        cleaned = name.strip() if isinstance(name, str) else ""
        if not cleaned or len(cleaned) > _MAX_NAME_LENGTH:
            raise _invalid(f"name 必须是 1-{_MAX_NAME_LENGTH} 个字符的非空文本")
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
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
        *,
        authority: KnowledgeProjectAuthority | None = None,
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
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
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

    async def delete_documents(
        self,
        project_id: UUID,
        document_ids: list[UUID],
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> list[KnowledgeDocumentView]:
        """Mark the batch ``deleting`` with delete tasks, all-or-nothing."""

        ids = _validated_document_ids(document_ids)
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
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
        authority: KnowledgeProjectAuthority | None = None,
    ) -> tuple[list[KnowledgeSegmentView], int]:
        """Published segments of the document's published generation, by position.

        The published generation — not the admission version — owns the
        content rows: after a failed re-parse or re-embed the version has
        moved on while the residual rows stay on ``published_version``, and
        this read-only projection must keep showing them instead of an empty
        page. A never-published document has no rows to show.
        """

        page, page_size = _validated_page(page, page_size)
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                document = (await session.execute(select(KnowledgeDocumentRow.version, KnowledgeDocumentRow.published_version).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id))).one_or_none()
                if document is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                current_version, published_version = document
                content_version = published_version if published_version is not None else current_version
                segment_filter = (
                    KnowledgeSegmentRow.knowledge_document_id == document_id,
                    KnowledgeSegmentRow.document_version == content_version,
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
                        token_count=segment.token_count,
                        source_spans=tuple(SourceSpan.model_validate(span) for span in segment.source_spans),
                    )
                    for segment in rows.all()
                ]
                return views, int(total or 0)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def delete_document(
        self,
        project_id: UUID,
        document_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeDocumentView:
        """Mark the document ``deleting`` and ensure one open delete task exists.

        Calling delete again after a finally-failed deletion creates a fresh
        task; while a delete task is open the view's ``delete_error`` is null.
        """

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
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
        return await session.scalar(select(document_delete_error_expression(KnowledgeDocumentRow.id)).where(KnowledgeDocumentRow.id == document_id))

    async def _load_document(
        self,
        project_id: UUID,
        document_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> tuple[KnowledgeDocumentRow, str | None, KnowledgeTaskProgress | None]:
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                result = (await session.execute(_document_with_derivations(project_id).where(KnowledgeDocumentRow.id == document_id))).one_or_none()
                progress: dict[UUID, KnowledgeTaskProgress] = {}
                if result is not None:
                    progress = await indexing_task_progress(session, project_id, {result[0].id: result[0].version})
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        if result is None:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
        row, delete_error = result
        return row, delete_error, progress.get(row.id)


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


def _delete_task(
    project_id: UUID,
    resource_id: UUID,
    kind: str,
    *,
    storage_key: str | None = None,
) -> KnowledgeTaskRow:
    return KnowledgeTaskRow(
        id=uuid4(),
        project_id=project_id,
        resource_id=resource_id,
        kind=kind,
        target_version=None,
        storage_key=storage_key,
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
