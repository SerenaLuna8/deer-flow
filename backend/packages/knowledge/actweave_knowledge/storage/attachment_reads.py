"""Authorized published-image reads, fenced before and after bounded object I/O."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..asyncio_utils import run_sync_to_completion
from ..authority import KnowledgeProjectAuthority, revalidate_project_authority
from ..contracts import KNOWLEDGE_CONFLICT, KNOWLEDGE_INVALID_REQUEST, KNOWLEDGE_NOT_FOUND, KNOWLEDGE_STORAGE_UNAVAILABLE, KnowledgeError
from ..extraction import ExtractionLimits
from ..persistence.models import KnowledgeAttachmentRow, KnowledgeBaseRow, KnowledgeDocumentRow, KnowledgeExtractionRow, KnowledgeSegmentAttachmentRow, KnowledgeSegmentRow
from ..segments.service import load_citation_segment, load_managed_segment
from .minio_store import MinioObjectStore


@dataclass(frozen=True, slots=True)
class AttachmentReadMetadata:
    media_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class AuthorizedAttachmentSnapshot:
    """Server-only evidence; never serialize into a view, URL, or model input."""

    project_id: UUID
    base_id: UUID
    document_id: UUID
    segment_id: UUID
    attachment_id: UUID
    extraction_id: UUID
    document_version: int
    content_digest: str
    sha256: str
    storage_key: str = field(repr=False)
    media_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _Scope:
    project_id: UUID
    document_id: UUID
    segment_id: UUID
    attachment_id: UUID


@dataclass(frozen=True, slots=True)
class _Expected:
    document_version: int
    content_digest: str


_SegmentLoader = Callable[[AsyncSession], Awaitable[tuple[KnowledgeSegmentRow, KnowledgeDocumentRow, KnowledgeBaseRow]]]


def _storage_error() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "图片内容读取或校验失败")


def _verify_bytes(path: Path, snapshot: AuthorizedAttachmentSnapshot) -> None:
    """Verify actual bytes with the fixed per-image cap, not object metadata."""
    digest, total = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while block := stream.read(65536):
            total += len(block)
            if total > ExtractionLimits().max_image_bytes:
                raise _storage_error()
            digest.update(block)
    if total != snapshot.size_bytes or digest.hexdigest() != snapshot.sha256:
        raise _storage_error()


class KnowledgeAttachmentReadService:
    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], object_store: MinioObjectStore) -> None:
        self._sessions = session_factory
        self._objects = object_store

    async def download_managed(
        self,
        project_id: UUID,
        document_id: UUID,
        segment_id: UUID,
        attachment_id: UUID,
        target_path: Path,
        *,
        expected_document_version: int,
        expected_content_digest: str,
        authority: KnowledgeProjectAuthority,
    ) -> AttachmentReadMetadata:
        async def loader(session: AsyncSession):
            return await load_managed_segment(
                session,
                project_id,
                document_id,
                segment_id,
                expected_document_version=None,
                expected_content_digest=None,
            )

        return await self._download(loader, authority, _Scope(project_id, document_id, segment_id, attachment_id), _Expected(expected_document_version, expected_content_digest), target_path)

    async def download_citation(
        self,
        project_id: UUID,
        base_id: UUID,
        document_id: UUID,
        segment_id: UUID,
        attachment_id: UUID,
        target_path: Path,
        *,
        expected_document_version: int,
        expected_content_digest: str,
        authority: KnowledgeProjectAuthority,
    ) -> AttachmentReadMetadata:
        async def loader(session: AsyncSession):
            return await load_citation_segment(
                session,
                project_id,
                base_id,
                document_id,
                segment_id,
                expected_document_version=None,
                expected_content_digest=None,
            )

        return await self._download(loader, authority, _Scope(project_id, document_id, segment_id, attachment_id), _Expected(expected_document_version, expected_content_digest), target_path)

    async def _download(self, loader: _SegmentLoader, authority: KnowledgeProjectAuthority, scope: _Scope, expected: _Expected, target_path: Path) -> AttachmentReadMetadata:
        try:
            if (
                type(expected.document_version) is not int
                or expected.document_version < 1
                or not isinstance(expected.content_digest, str)
                or len(expected.content_digest) != 64
                or any(char not in "0123456789abcdef" for char in expected.content_digest)
            ):
                raise KnowledgeError(KNOWLEDGE_INVALID_REQUEST, "图片读取必须提供有效的版本和内容摘要")
            before = await self._load_authorized_snapshot(loader, authority, scope, expected)
            await self._objects.download_to(before.storage_key, target_path, max_bytes=ExtractionLimits().max_image_bytes)
            await run_sync_to_completion(_verify_bytes, target_path, before)
            after = await self._load_authorized_snapshot(loader, authority, scope, expected)
            if after != before:
                raise KnowledgeError(KNOWLEDGE_CONFLICT, "图片引用已变化")
            return AttachmentReadMetadata(media_type=after.media_type, size_bytes=after.size_bytes)
        except BaseException:
            await run_sync_to_completion(target_path.unlink, missing_ok=True)
            raise

    async def _load_authorized_snapshot(
        self,
        loader: _SegmentLoader,
        authority: KnowledgeProjectAuthority,
        scope: _Scope,
        expected: _Expected,
    ) -> AuthorizedAttachmentSnapshot:
        try:
            async with self._sessions() as session, session.begin():
                if authority is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")
                await revalidate_project_authority(authority, session, project_id=scope.project_id)
                segment, document, base = await loader(session)
                attachment = await session.scalar(
                    select(KnowledgeAttachmentRow)
                    .join(KnowledgeSegmentAttachmentRow, KnowledgeSegmentAttachmentRow.attachment_id == KnowledgeAttachmentRow.id)
                    .join(KnowledgeExtractionRow, KnowledgeExtractionRow.id == KnowledgeAttachmentRow.extraction_id)
                    .where(
                        KnowledgeSegmentAttachmentRow.project_id == scope.project_id,
                        KnowledgeSegmentAttachmentRow.knowledge_base_id == base.id,
                        KnowledgeSegmentAttachmentRow.knowledge_document_id == scope.document_id,
                        KnowledgeSegmentAttachmentRow.segment_id == scope.segment_id,
                        KnowledgeSegmentAttachmentRow.extraction_id == segment.extraction_id,
                        KnowledgeAttachmentRow.id == scope.attachment_id,
                        KnowledgeAttachmentRow.project_id == scope.project_id,
                        KnowledgeAttachmentRow.knowledge_base_id == base.id,
                        KnowledgeAttachmentRow.knowledge_document_id == scope.document_id,
                        KnowledgeAttachmentRow.extraction_id == segment.extraction_id,
                        KnowledgeAttachmentRow.extraction_id == document.published_extraction_id,
                        KnowledgeAttachmentRow.state == "ready",
                        KnowledgeAttachmentRow.upload_state == "stored",
                        KnowledgeExtractionRow.project_id == scope.project_id,
                        KnowledgeExtractionRow.knowledge_base_id == base.id,
                        KnowledgeExtractionRow.knowledge_document_id == scope.document_id,
                        KnowledgeExtractionRow.state == "ready",
                    )
                    .limit(1)
                )
                if attachment is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "图片不存在")
                content_digest = hashlib.sha256(segment.content.encode("utf-8")).hexdigest()
                if expected.document_version != segment.document_version or expected.content_digest != content_digest:
                    raise KnowledgeError(KNOWLEDGE_CONFLICT, "图片引用已变化")
                return AuthorizedAttachmentSnapshot(
                    project_id=scope.project_id,
                    base_id=base.id,
                    document_id=document.id,
                    segment_id=segment.id,
                    attachment_id=attachment.id,
                    extraction_id=attachment.extraction_id,
                    document_version=segment.document_version,
                    content_digest=content_digest,
                    sha256=attachment.sha256,
                    storage_key=attachment.storage_key,
                    media_type=attachment.media_type,
                    size_bytes=attachment.size_bytes,
                )
        except SQLAlchemyError:
            raise _storage_error() from None
