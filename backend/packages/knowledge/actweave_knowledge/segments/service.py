"""Segment governance on a ready document's current version.

Content edits and manual additions re-embed synchronously with the base's
bound embedding model, so the vector never lags the text. The embedding call
happens outside any transaction; the write transaction re-checks the document
version and fails with ``KNOWLEDGE_CONFLICT`` when a re-ingest or delete won
the race — a stale vector is never published.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..authority import KnowledgeProjectAuthority, revalidate_project_authority
from ..contracts import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeDocumentView,
    KnowledgeEmbeddingMaterial,
    KnowledgeError,
    KnowledgeModelPort,
    KnowledgeSegmentCreate,
    KnowledgeSegmentUpdate,
    KnowledgeSegmentView,
    KnowledgeSettings,
)
from ..documents.service import document_view
from ..ingestion.splitter import split_child_chunks
from ..models.client import KnowledgeModelClient
from ..persistence.derivations import document_delete_error_expression
from ..persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
)

logger = logging.getLogger(__name__)

# Manual and edited segments obey the same ceiling as the splitter's largest
# allowed chunk, so one segment can never dwarf ingested ones.
MAX_SEGMENT_CONTENT_CHARS = 4000


def _invalid(message: str) -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_INVALID_REQUEST, message)


def _conflict() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_CONFLICT, "文档内容已更新，请刷新后重试")


def _storage_unavailable() -> KnowledgeError:
    """Public storage error; logs the database exception being mapped, if any."""

    if sys.exc_info()[0] is not None:
        logger.warning("knowledge database operation failed", exc_info=True)
    return KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用")


def _quota_exceeded(limit: int) -> KnowledgeError:
    return KnowledgeError(
        KNOWLEDGE_QUOTA_EXCEEDED,
        f"Document 内向量条目数量超过上限 {limit}",
    )


def _validated_content(content: str) -> str:
    if not isinstance(content, str):
        raise _invalid("content 必须是字符串")
    cleaned = content.strip()
    if not cleaned or len(cleaned) > MAX_SEGMENT_CONTENT_CHARS:
        raise _invalid(f"content 必须是 1-{MAX_SEGMENT_CONTENT_CHARS} 个字符的非空文本")
    return cleaned


@dataclass(frozen=True, slots=True)
class _EmbeddedContent:
    """Pre-transaction embedding result for one segment's new content."""

    parent_vector: list[float] | None
    children: tuple[str, ...]
    child_vectors: tuple[list[float], ...]


def _segment_view(row: KnowledgeSegmentRow) -> KnowledgeSegmentView:
    return KnowledgeSegmentView(
        id=row.id,
        document_version=row.document_version,
        position=row.position,
        content=row.content,
        word_count=row.word_count,
        enabled=row.enabled,
        hit_count=row.hit_count,
        source_position=dict(row.source_position),
        created_at=row.created_at,
    )


class KnowledgeSegmentService:
    """Edit, add, delete, and toggle segments of ready documents."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: KnowledgeSettings,
        client: KnowledgeModelClient,
        model_port: KnowledgeModelPort,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._client = client
        self._model_port = model_port

    async def update_segment(
        self,
        project_id: UUID,
        segment_id: UUID,
        update: KnowledgeSegmentUpdate,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeSegmentView:
        """Edit content (with synchronous re-embedding) and/or flip ``enabled``."""

        if update.content is None and update.enabled is None:
            raise _invalid("至少提供一个要修改的字段")
        if update.enabled is not None and type(update.enabled) is not bool:
            raise _invalid("enabled 必须是布尔值")
        content = _validated_content(update.content) if update.content is not None else None

        vector: list[float] | None = None
        embedded: _EmbeddedContent | None = None
        if content is not None:
            document, _segment, other_vector_entries = await self._load_segment_snapshot(
                project_id,
                segment_id,
                authority=authority,
            )
            material = await self._embedding_material(
                project_id,
                document.knowledge_base_id,
                authority=authority,
            )
            embedded = await self._embed_for_document(
                document,
                content,
                material,
                available_vector_entries=(self._settings.max_segments_per_document - other_vector_entries),
            )
            vector = embedded.parent_vector

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                # After the embedding call any drift is a lost race, not a bad
                # request: surface it as a refresh-and-retry conflict.
                document, segment = await self._lock_segment(session, project_id, segment_id, as_conflict=content is not None)
                if content is not None and embedded is not None:
                    other_vector_entries = await self._vector_entry_count(
                        session,
                        document,
                        excluding_segment_id=segment.id,
                    )
                    if other_vector_entries + _embedded_vector_entry_count(embedded) > self._settings.max_segments_per_document:
                        raise _quota_exceeded(
                            self._settings.max_segments_per_document,
                        )
                    word_count = len(content)
                    document.word_count = document.word_count - segment.word_count + word_count
                    document.updated_at = func.now()  # type: ignore[assignment]
                    segment.content = content
                    segment.word_count = word_count
                    segment.embedding = vector
                    if document.chunking_mode == "parent_child":
                        await self._replace_children(session, segment, embedded)
                if update.enabled is not None:
                    segment.enabled = update.enabled
                await session.flush()
                await session.refresh(segment)
                return _segment_view(segment)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def create_segment(
        self,
        project_id: UUID,
        document_id: UUID,
        create: KnowledgeSegmentCreate,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeSegmentView:
        """Append one manual segment to the document's current version."""

        content = _validated_content(create.content)
        document, current_vector_entries = await self._load_document_snapshot(
            project_id,
            document_id,
            authority=authority,
        )
        material = await self._embedding_material(
            project_id,
            document.knowledge_base_id,
            authority=authority,
        )
        embedded = await self._embed_for_document(
            document,
            content,
            material,
            available_vector_entries=(self._settings.max_segments_per_document - current_vector_entries),
        )
        vector = embedded.parent_vector
        snapshot_version = document.version

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id).with_for_update())
                if document is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                if document.status != "ready" or document.version != snapshot_version:
                    raise _conflict()
                current = (
                    KnowledgeSegmentRow.knowledge_document_id == document.id,
                    KnowledgeSegmentRow.document_version == document.version,
                )
                current_vector_entries = await self._vector_entry_count(
                    session,
                    document,
                )
                if current_vector_entries + _embedded_vector_entry_count(embedded) > self._settings.max_segments_per_document:
                    raise _quota_exceeded(
                        self._settings.max_segments_per_document,
                    )
                next_position = int(await session.scalar(select(func.max(KnowledgeSegmentRow.position)).where(*current)) or 0) + 1
                word_count = len(content)
                segment = KnowledgeSegmentRow(
                    id=uuid4(),
                    project_id=document.project_id,
                    knowledge_base_id=document.knowledge_base_id,
                    knowledge_document_id=document.id,
                    document_version=document.version,
                    position=next_position,
                    content=content,
                    word_count=word_count,
                    source_position={"manual": True},
                    embedding=vector,
                )
                session.add(segment)
                if document.chunking_mode == "parent_child":
                    await session.flush()
                    await self._replace_children(session, segment, embedded)
                document.segment_count = document.segment_count + 1
                document.word_count = document.word_count + word_count
                document.updated_at = func.now()  # type: ignore[assignment]
                await session.flush()
                await session.refresh(segment)
                return _segment_view(segment)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def delete_segment(
        self,
        project_id: UUID,
        segment_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeDocumentView:
        """Delete the row (its vector goes with it) and return the updated document."""

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                document, segment = await self._lock_segment(session, project_id, segment_id, as_conflict=False)
                await session.delete(segment)
                document.segment_count = document.segment_count - 1
                document.word_count = document.word_count - segment.word_count
                document.updated_at = func.now()  # type: ignore[assignment]
                await session.flush()
                await session.refresh(document)
                delete_error = await session.scalar(select(document_delete_error_expression(KnowledgeDocumentRow.id)).where(KnowledgeDocumentRow.id == document.id))
                return document_view(document, delete_error=delete_error)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def _load_segment_snapshot(
        self,
        project_id: UUID,
        segment_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> tuple[KnowledgeDocumentRow, KnowledgeSegmentRow, int]:
        """Pre-embedding read: the segment must sit on a ready current version."""

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                result = (
                    await session.execute(
                        select(KnowledgeDocumentRow, KnowledgeSegmentRow)
                        .join(KnowledgeSegmentRow, KnowledgeSegmentRow.knowledge_document_id == KnowledgeDocumentRow.id)
                        .where(KnowledgeSegmentRow.project_id == project_id, KnowledgeSegmentRow.id == segment_id)
                    )
                ).one_or_none()
                if result is not None:
                    document, segment = result
                    _require_current_ready(document, segment)
                    other_vector_entries = await self._vector_entry_count(
                        session,
                        document,
                        excluding_segment_id=segment.id,
                    )
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        if result is None:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Segment 不存在")
        return document, segment, other_vector_entries

    async def _load_document_snapshot(
        self,
        project_id: UUID,
        document_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> tuple[KnowledgeDocumentRow, int]:
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id))
                if document is not None:
                    if document.status != "ready":
                        raise _invalid("仅 ready 状态的文档支持维护分段")
                    vector_entries = await self._vector_entry_count(
                        session,
                        document,
                    )
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        if document is None:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
        return document, vector_entries

    async def _lock_segment(
        self,
        session: AsyncSession,
        project_id: UUID,
        segment_id: UUID,
        *,
        as_conflict: bool,
    ) -> tuple[KnowledgeDocumentRow, KnowledgeSegmentRow]:
        """Lock document then segment (the ingest publisher's lock order).

        With ``as_conflict`` any missing/stale state maps to the refresh-and-
        retry conflict (the caller already validated before embedding);
        otherwise first-touch errors stay precise.
        """

        segment = await session.scalar(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.project_id == project_id, KnowledgeSegmentRow.id == segment_id))
        if segment is None:
            raise _conflict() if as_conflict else KnowledgeError(KNOWLEDGE_NOT_FOUND, "Segment 不存在")
        document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == segment.knowledge_document_id).with_for_update())
        if document is None:  # pragma: no cover - FK keeps the document alive
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
        segment = await session.scalar(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.id == segment_id).with_for_update())
        if segment is None:
            # A concurrent re-ingest deleted the row between the two reads.
            raise _conflict() if as_conflict else KnowledgeError(KNOWLEDGE_NOT_FOUND, "Segment 不存在")
        if as_conflict and (segment.document_version != document.version or document.status != "ready"):
            raise _conflict()
        _require_current_ready(document, segment)
        return document, segment

    async def _embed_for_document(
        self,
        document: KnowledgeDocumentRow,
        content: str,
        material: KnowledgeEmbeddingMaterial,
        *,
        available_vector_entries: int,
    ) -> _EmbeddedContent:
        """Embed per the document's frozen chunking mode.

        General mode embeds the segment itself; parent_child mode re-splits the
        content with the document's frozen child parameters and embeds the
        children — the parent row keeps a NULL vector.
        """

        if document.chunking_mode != "parent_child":
            if available_vector_entries < 1:
                raise _quota_exceeded(
                    self._settings.max_segments_per_document,
                )
            return _EmbeddedContent(
                parent_vector=(await self._client.embed(material, [content]))[0],
                children=(),
                child_vectors=(),
            )
        children = split_child_chunks(
            content,
            child_chunk_size=document.child_chunk_size,
            child_chunk_separator=document.child_chunk_separator,
        )
        if not children:  # pragma: no cover - non-empty content always splits
            raise _invalid("内容未能切分出子块")
        if len(children) > available_vector_entries:
            raise _quota_exceeded(
                self._settings.max_segments_per_document,
            )
        child_vectors = await self._client.embed(material, list(children))
        return _EmbeddedContent(parent_vector=None, children=children, child_vectors=tuple(child_vectors))

    async def _replace_children(
        self,
        session: AsyncSession,
        segment: KnowledgeSegmentRow,
        embedded: _EmbeddedContent,
    ) -> None:
        """Swap the segment's child rows for the freshly embedded ones."""

        await session.execute(delete(KnowledgeSegmentChildRow).where(KnowledgeSegmentChildRow.knowledge_segment_id == segment.id))
        session.add_all(
            [
                KnowledgeSegmentChildRow(
                    id=uuid4(),
                    project_id=segment.project_id,
                    knowledge_base_id=segment.knowledge_base_id,
                    knowledge_document_id=segment.knowledge_document_id,
                    knowledge_segment_id=segment.id,
                    document_version=segment.document_version,
                    position=position,
                    content=content,
                    word_count=len(content),
                    embedding=vector,
                )
                for position, (content, vector) in enumerate(
                    zip(embedded.children, embedded.child_vectors, strict=True),
                    start=1,
                )
            ]
        )

    async def _embedding_material(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeEmbeddingMaterial:
        """The base's bound embedding model materialized through the host port."""

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                row = (await session.execute(select(KnowledgeBaseRow.status, KnowledgeBaseRow.embedding_model_id).where(KnowledgeBaseRow.id == base_id))).one_or_none()
                if row is not None:
                    base_status, embedding_model_id = row
                    if base_status != "active":
                        raise _invalid("仅 active 状态的 Knowledge Base 支持维护分段")
                    # The port validates type and active status and raises
                    # KNOWLEDGE_MODEL_UNAVAILABLE for anything unresolvable.
                    return await self._model_port.embedding_material(session, embedding_model_id)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        # pragma: no cover - RESTRICT FK keeps the base alive
        raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")

    @staticmethod
    async def _vector_entry_count(
        session: AsyncSession,
        document: KnowledgeDocumentRow,
        *,
        excluding_segment_id: UUID | None = None,
    ) -> int:
        """Count vector-bearing rows for the current Document version."""

        if document.chunking_mode == "parent_child":
            statement = (
                select(func.count())
                .select_from(
                    KnowledgeSegmentChildRow,
                )
                .where(
                    KnowledgeSegmentChildRow.knowledge_document_id == document.id,
                    KnowledgeSegmentChildRow.document_version == document.version,
                )
            )
            if excluding_segment_id is not None:
                statement = statement.where(
                    KnowledgeSegmentChildRow.knowledge_segment_id != excluding_segment_id,
                )
        else:
            statement = (
                select(func.count())
                .select_from(
                    KnowledgeSegmentRow,
                )
                .where(
                    KnowledgeSegmentRow.knowledge_document_id == document.id,
                    KnowledgeSegmentRow.document_version == document.version,
                )
            )
            if excluding_segment_id is not None:
                statement = statement.where(
                    KnowledgeSegmentRow.id != excluding_segment_id,
                )
        return int(await session.scalar(statement) or 0)


def _embedded_vector_entry_count(embedded: _EmbeddedContent) -> int:
    return 1 if embedded.parent_vector is not None else len(embedded.child_vectors)


def _require_current_ready(document: KnowledgeDocumentRow, segment: KnowledgeSegmentRow) -> None:
    """Segment operations target the ready document's current version only."""

    if segment.document_version != document.version:
        raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Segment 不存在")
    if document.status != "ready":
        raise _invalid("仅 ready 状态的文档支持维护分段")
