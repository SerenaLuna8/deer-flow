"""Segment governance on a ready document's current version.

Content edits and manual additions re-embed synchronously with the base's
bound embedding model, so the vector never lags the text. The embedding call
happens outside any transaction; the write transaction re-checks the document
version and fails with ``KNOWLEDGE_CONFLICT`` when a re-ingest or delete won
the race — a stale vector is never published.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..authority import KnowledgeProjectAuthority, revalidate_project_authority
from ..contracts import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_LEXICAL_VERSION,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KNOWLEDGE_SEGMENT_DETAIL_CHILD_PAGE_SIZE,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeDocumentView,
    KnowledgeEmbeddingMaterial,
    KnowledgeError,
    KnowledgeModelPort,
    KnowledgeSegmentAttachmentView,
    KnowledgeSegmentChildView,
    KnowledgeSegmentCreate,
    KnowledgeSegmentDetail,
    KnowledgeSegmentSummaryView,
    KnowledgeSegmentUpdate,
    KnowledgeSegmentView,
    KnowledgeSettings,
)
from ..documents.service import document_view
from ..extraction.contracts import Document as ParsedDocument
from ..extraction.contracts import ProcessingProfile, SourceSpan
from ..ingestion.index_text import build_index_text
from ..ingestion.splitter import (
    ChildDraft,
    fits_chunk,
    split_child_chunks,
    split_documents,
)
from ..ingestion.summary_admission import enqueue_summary_refresh
from ..ingestion.tokenizer import count_knowledge_tokens
from ..models.client import KnowledgeModelClient
from ..persistence.derivations import document_delete_error_expression
from ..persistence.models import (
    KnowledgeAttachmentRow,
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentAttachmentRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
    KnowledgeSegmentSummaryRow,
)
from ..retrieval.lexical import lexical_index_input

logger = logging.getLogger(__name__)

# Manual and edited segments obey the same ceiling as the splitter's largest
# allowed chunk, so one segment can never dwarf ingested ones.
MAX_SEGMENT_CONTENT_CHARS = 4000
_LOGICAL_ATTACHMENT_PREFIX = "knowledge-attachment:"
_LOGICAL_ATTACHMENT_IMAGE = re.compile(r"!\[((?:\\.|[^\]\\])*)\]\(knowledge-attachment:([0-9a-f]{64})\)")


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
    index_text: str
    token_count: int
    children: tuple[ChildDraft, ...]
    child_vectors: tuple[list[float], ...]


@dataclass(frozen=True, slots=True)
class _ManualAttachment:
    ref: str
    alt_text: str


def _manual_attachments(content: str) -> tuple[_ManualAttachment, ...]:
    """Parse ordered logical image refs and reject malformed/raw identities."""

    matches = list(_LOGICAL_ATTACHMENT_IMAGE.finditer(content))
    outside = _LOGICAL_ATTACHMENT_IMAGE.sub("", content)
    if _LOGICAL_ATTACHMENT_PREFIX in outside:
        raise _invalid("content 包含无效的附件引用")
    return tuple(
        _ManualAttachment(
            ref=match.group(2),
            alt_text=re.sub(r"\\([\\\]])", r"\1", match.group(1)),
        )
        for match in matches
    )


def _manual_derivation(
    document: KnowledgeDocumentRow,
    content: str,
) -> tuple[str, int, tuple[ChildDraft, ...]]:
    """Derive one manually governed parent without claiming source offsets."""

    index_text = build_index_text(content)
    if not index_text or not fits_chunk(content, 4000):
        raise _invalid("content 超出 Knowledge 分段预算或没有可索引文字")
    token_count = count_knowledge_tokens(index_text)
    if document.chunking_mode != "parent_child":
        return index_text, token_count, ()

    profile_value = document.parsing_profile
    if profile_value is None:
        children = split_child_chunks(
            content,
            child_chunk_size=document.child_chunk_size,
            child_chunk_separator=document.child_chunk_separator,
        )
        return (
            index_text,
            token_count,
            tuple(
                ChildDraft(
                    child,
                    build_index_text(child),
                    count_knowledge_tokens(build_index_text(child)),
                )
                for child in children
            ),
        )

    try:
        profile = ProcessingProfile.model_validate(profile_value)
    except ValueError:
        raise _storage_unavailable() from None
    if profile.chunk.unit == "character":
        children = split_child_chunks(
            content,
            child_chunk_size=document.child_chunk_size,
            child_chunk_separator=document.child_chunk_separator,
        )
        return (
            index_text,
            token_count,
            tuple(
                ChildDraft(
                    child,
                    build_index_text(child),
                    count_knowledge_tokens(build_index_text(child)),
                )
                for child in children
            ),
        )

    manual_profile = profile.chunk.model_copy(
        update={
            "mode": "parent_child",
            "size": 4000,
            "overlap": 0,
            "child_size": document.child_chunk_size,
            "child_separator": document.child_chunk_separator,
            "remove_extra_spaces": False,
            "remove_urls_emails": False,
        }
    )
    synthetic = ParsedDocument(
        page_content=content,
        source_spans=(
            SourceSpan(
                block_id="manual",
                start=0,
                end=len(content),
                location={},
            ),
        ),
    )
    drafts = split_documents((synthetic,), profile=manual_profile)
    if len(drafts) != 1 or drafts[0].content != content:
        raise _invalid("content 超出 Knowledge 分段预算")
    return (
        index_text,
        token_count,
        tuple(
            ChildDraft(
                child.content,
                child.index_text,
                child.token_count,
            )
            for child in drafts[0].children
        ),
    )


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
        token_count=row.token_count,
        source_spans=tuple(SourceSpan.model_validate(span) for span in row.source_spans),
    )


async def _load_published_segment(
    session: AsyncSession,
    project_id: UUID,
    document_id: UUID,
    segment_id: UUID,
    *,
    base_id: UUID | None = None,
    expected_document_version: int | None,
    expected_content_digest: str | None,
) -> tuple[KnowledgeSegmentRow, KnowledgeDocumentRow, KnowledgeBaseRow]:
    """Load retained publication for management, including disabled content.

    The caller owns project authorization and the transaction. Expectations
    bind the Segment's publication, never a newer failed processing target.
    """
    scope = (
        KnowledgeBaseRow.project_id == project_id,
        KnowledgeDocumentRow.project_id == project_id,
        KnowledgeSegmentRow.project_id == project_id,
        KnowledgeSegmentRow.knowledge_base_id == KnowledgeBaseRow.id,
        KnowledgeDocumentRow.id == document_id,
        KnowledgeSegmentRow.id == segment_id,
        KnowledgeBaseRow.status != "deleting",
        KnowledgeDocumentRow.status != "deleting",
    )
    if base_id is not None:
        scope += (KnowledgeBaseRow.id == base_id,)
    result = (
        await session.execute(
            select(KnowledgeSegmentRow, KnowledgeDocumentRow, KnowledgeBaseRow)
            .join(KnowledgeDocumentRow, KnowledgeDocumentRow.id == KnowledgeSegmentRow.knowledge_document_id)
            .join(KnowledgeBaseRow, KnowledgeBaseRow.id == KnowledgeDocumentRow.knowledge_base_id)
            .where(*scope)
        )
    ).one_or_none()
    if result is None:
        raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Segment 不存在")
    segment, document, base = result
    if document.published_version is None or segment.document_version != document.published_version:
        raise _conflict()
    if expected_document_version is not None and expected_document_version != segment.document_version:
        raise _conflict()
    if expected_content_digest is not None and expected_content_digest != hashlib.sha256(segment.content.encode("utf-8")).hexdigest():
        raise _conflict()
    return segment, document, base


async def load_managed_segment(
    session: AsyncSession,
    project_id: UUID,
    document_id: UUID,
    segment_id: UUID,
    *,
    expected_document_version: int | None,
    expected_content_digest: str | None,
) -> tuple[KnowledgeSegmentRow, KnowledgeDocumentRow, KnowledgeBaseRow]:
    """Management expectations identify the retained published Segment."""
    return await _load_published_segment(session, project_id, document_id, segment_id, expected_document_version=expected_document_version, expected_content_digest=expected_content_digest)


async def load_citation_segment(
    session: AsyncSession,
    project_id: UUID,
    base_id: UUID,
    document_id: UUID,
    segment_id: UUID,
    *,
    expected_document_version: int | None,
    expected_content_digest: str | None,
) -> tuple[KnowledgeSegmentRow, KnowledgeDocumentRow, KnowledgeBaseRow]:
    """Load only an enabled current publication eligible for citations."""
    segment, document, base = await _load_published_segment(
        session,
        project_id,
        document_id,
        segment_id,
        base_id=base_id,
        expected_document_version=expected_document_version,
        expected_content_digest=expected_content_digest,
    )
    if base.status != "active" or document.status != "ready" or not document.enabled or not segment.enabled or segment.document_version != document.version:
        raise _conflict()
    return segment, document, base


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
        attachments: tuple[_ManualAttachment, ...] = ()
        snapshot_version: int | None = None
        embedded_model_id: UUID | None = None
        if content is not None:
            attachments = _manual_attachments(content)
            document, _segment, other_vector_entries = await self._load_segment_snapshot(
                project_id,
                segment_id,
                attachments=attachments,
                authority=authority,
            )
            # Freeze the pre-provider-call coordinates. Re-embedding keeps
            # row UUIDs, so "the row still exists on the current version" no
            # longer proves nothing happened in between; only an explicit
            # version-and-binding comparison keeps a late edit from writing
            # an old model's vector into the new space.
            snapshot_version = document.version
            material = await self._embedding_material(
                project_id,
                document.knowledge_base_id,
                authority=authority,
            )
            embedded_model_id = material.model_id
            embedded = await self._embed_for_document(
                document,
                content,
                material,
                available_vector_entries=(self._settings.max_segments_per_document - other_vector_entries),
                authority=authority,
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
                if content is not None and document.version != snapshot_version:
                    raise _conflict()
                if content is not None and not await self._binding_unchanged(session, document.knowledge_base_id, embedded_model_id):
                    raise _conflict()
                if content is not None and embedded is not None:
                    attachment_rows = await self._validated_attachment_rows(
                        session,
                        document,
                        attachments,
                        lock=True,
                    )
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
                    segment.index_text = embedded.index_text
                    segment.token_count = embedded.token_count
                    segment.source_spans = []
                    segment.source_position = {"manual": True}
                    segment.word_count = word_count
                    segment.embedding = vector
                    # The lexical derivation moves with the text in the same
                    # transaction; an enabled-only toggle never touches it.
                    segment.lexical_tsv = func.to_tsvector(
                        "simple",
                        lexical_index_input(embedded.index_text),
                    )
                    segment.lexical_version = KNOWLEDGE_LEXICAL_VERSION
                    if document.chunking_mode == "parent_child":
                        await self._replace_children(session, segment, embedded)
                    await self._replace_attachments(
                        session,
                        document,
                        segment,
                        attachments,
                        attachment_rows,
                    )
                if update.enabled is not None:
                    segment.enabled = update.enabled
                if content is not None:
                    await session.execute(delete(KnowledgeSegmentSummaryRow).where(KnowledgeSegmentSummaryRow.knowledge_segment_id == segment.id))
                await session.flush()
                if content is not None:
                    await enqueue_summary_refresh(session, document, self._model_port)
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
        attachments = _manual_attachments(content)
        document, current_vector_entries = await self._load_document_snapshot(
            project_id,
            document_id,
            attachments=attachments,
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
            authority=authority,
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
                if not await self._binding_unchanged(session, document.knowledge_base_id, material.model_id):
                    raise _conflict()
                attachment_rows = await self._validated_attachment_rows(
                    session,
                    document,
                    attachments,
                    lock=True,
                )
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
                    extraction_id=document.published_extraction_id,
                    position=next_position,
                    content=content,
                    index_text=embedded.index_text,
                    token_count=embedded.token_count,
                    source_spans=[],
                    word_count=word_count,
                    source_position={"manual": True},
                    embedding=vector,
                    lexical_tsv=func.to_tsvector(
                        "simple",
                        lexical_index_input(embedded.index_text),
                    ),
                    lexical_version=KNOWLEDGE_LEXICAL_VERSION,
                )
                session.add(segment)
                if document.chunking_mode == "parent_child" or attachments:
                    await session.flush()
                if attachments:
                    await self._replace_attachments(
                        session,
                        document,
                        segment,
                        attachments,
                        attachment_rows,
                    )
                if document.chunking_mode == "parent_child":
                    await self._replace_children(session, segment, embedded)
                document.segment_count = document.segment_count + 1
                document.word_count = document.word_count + word_count
                document.updated_at = func.now()  # type: ignore[assignment]
                await session.flush()
                await enqueue_summary_refresh(session, document, self._model_port)
                await session.refresh(segment)
                return _segment_view(segment)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def get_segment_detail(
        self,
        project_id: UUID,
        base_id: UUID,
        document_id: UUID,
        segment_id: UUID,
        *,
        expected_document_version: int | None = None,
        expected_content_digest: str | None = None,
        child_page: int = 1,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeSegmentDetail:
        """Authoritative single-segment read with paged children.

        Opened from a search hit, callers pass the hit's document version and
        content digest: any drift — including a document that is no longer
        ready or a row that is no longer the current generation — raises
        ``KNOWLEDGE_CONFLICT`` instead of silently explaining old scores with
        new text. Plain maintenance browsing omits the expectations and may
        read ``stale`` rows left behind by a failed reprocessing, clearly
        labeled with both generations. Every child page re-runs the complete
        validation; there is no cross-request detail state.
        """

        if type(child_page) is not int or child_page < 1:
            raise _invalid("child_page 必须是不小于 1 的整数")
        if expected_document_version is not None and (type(expected_document_version) is not int or expected_document_version < 1):
            raise _invalid("expected_document_version 必须是不小于 1 的整数")
        if expected_content_digest is not None and not isinstance(expected_content_digest, str):
            raise _invalid("expected_content_digest 必须是字符串")
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                if expected_document_version is not None or expected_content_digest is not None:
                    segment, document, _base = await load_citation_segment(
                        session,
                        project_id,
                        base_id,
                        document_id,
                        segment_id,
                        expected_document_version=expected_document_version,
                        expected_content_digest=expected_content_digest,
                    )
                else:
                    segment, document, _base = await _load_published_segment(
                        session,
                        project_id,
                        document_id,
                        segment_id,
                        base_id=base_id,
                        expected_document_version=None,
                        expected_content_digest=None,
                    )
                content_state = "current" if segment.document_version == document.version else "stale"
                children_scope = (
                    KnowledgeSegmentChildRow.knowledge_segment_id == segment.id,
                    KnowledgeSegmentChildRow.document_version == segment.document_version,
                )
                children_total = int(await session.scalar(select(func.count()).select_from(KnowledgeSegmentChildRow).where(*children_scope)) or 0)
                child_rows = (
                    await session.scalars(
                        select(KnowledgeSegmentChildRow)
                        .where(*children_scope)
                        .order_by(KnowledgeSegmentChildRow.position.asc(), KnowledgeSegmentChildRow.id.asc())
                        .offset((child_page - 1) * KNOWLEDGE_SEGMENT_DETAIL_CHILD_PAGE_SIZE)
                        .limit(KNOWLEDGE_SEGMENT_DETAIL_CHILD_PAGE_SIZE)
                    )
                ).all()
                summary = await session.scalar(
                    select(KnowledgeSegmentSummaryRow).where(
                        KnowledgeSegmentSummaryRow.project_id == project_id,
                        KnowledgeSegmentSummaryRow.knowledge_base_id == base_id,
                        KnowledgeSegmentSummaryRow.knowledge_document_id == document_id,
                        KnowledgeSegmentSummaryRow.knowledge_segment_id == segment.id,
                        KnowledgeSegmentSummaryRow.document_version == segment.document_version,
                        KnowledgeSegmentSummaryRow.source_content_digest == hashlib.sha256(segment.content.encode("utf-8")).hexdigest(),
                    )
                )
                attachment_rows = (
                    await session.execute(
                        select(
                            KnowledgeSegmentAttachmentRow,
                            KnowledgeAttachmentRow,
                        )
                        .join(
                            KnowledgeAttachmentRow,
                            and_(
                                KnowledgeAttachmentRow.id == KnowledgeSegmentAttachmentRow.attachment_id,
                                KnowledgeAttachmentRow.project_id == KnowledgeSegmentAttachmentRow.project_id,
                                KnowledgeAttachmentRow.knowledge_base_id == KnowledgeSegmentAttachmentRow.knowledge_base_id,
                                KnowledgeAttachmentRow.knowledge_document_id == KnowledgeSegmentAttachmentRow.knowledge_document_id,
                                KnowledgeAttachmentRow.extraction_id == KnowledgeSegmentAttachmentRow.extraction_id,
                            ),
                        )
                        .where(
                            KnowledgeSegmentAttachmentRow.project_id == segment.project_id,
                            KnowledgeSegmentAttachmentRow.knowledge_base_id == segment.knowledge_base_id,
                            KnowledgeSegmentAttachmentRow.knowledge_document_id == segment.knowledge_document_id,
                            KnowledgeSegmentAttachmentRow.extraction_id == segment.extraction_id,
                            KnowledgeSegmentAttachmentRow.segment_id == segment.id,
                        )
                        .order_by(
                            KnowledgeSegmentAttachmentRow.position.asc(),
                            KnowledgeSegmentAttachmentRow.attachment_id.asc(),
                        )
                    )
                ).all()
                return KnowledgeSegmentDetail(
                    segment=_segment_view(segment),
                    knowledge_base_id=base_id,
                    document_id=document.id,
                    document_name=document.name,
                    content_state=content_state,
                    stored_content_version=segment.document_version,
                    current_document_version=document.version,
                    children_total=children_total,
                    child_page=child_page,
                    summary=KnowledgeSegmentSummaryView(content=summary.content, created_at=summary.created_at) if summary is not None else None,
                    attachments=tuple(
                        KnowledgeSegmentAttachmentView(
                            attachment_id=attachment.id,
                            ref=attachment.sha256,
                            alt_text=binding.alt_text,
                            media_type=attachment.media_type,
                            width=attachment.width,
                            height=attachment.height,
                        )
                        for binding, attachment in attachment_rows
                    ),
                    children=tuple(
                        KnowledgeSegmentChildView(
                            id=row.id,
                            position=row.position,
                            content=row.content,
                            word_count=row.word_count,
                        )
                        for row in child_rows
                    ),
                )
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
        attachments: tuple[_ManualAttachment, ...] = (),
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
                    await self._validated_attachment_rows(
                        session,
                        document,
                        attachments,
                        lock=False,
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
        attachments: tuple[_ManualAttachment, ...] = (),
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
                    await self._validated_attachment_rows(
                        session,
                        document,
                        attachments,
                        lock=False,
                    )
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        if document is None:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
        return document, vector_entries

    @staticmethod
    async def _validated_attachment_rows(
        session: AsyncSession,
        document: KnowledgeDocumentRow,
        attachments: tuple[_ManualAttachment, ...],
        *,
        lock: bool,
    ) -> dict[str, KnowledgeAttachmentRow]:
        """Resolve refs only inside this Document's current publication."""

        refs = {attachment.ref for attachment in attachments}
        if not refs:
            return {}
        if document.published_extraction_id is None:
            raise _invalid("content 引用了当前文档不可用的附件")
        statement = select(KnowledgeAttachmentRow).where(
            KnowledgeAttachmentRow.project_id == document.project_id,
            KnowledgeAttachmentRow.knowledge_base_id == document.knowledge_base_id,
            KnowledgeAttachmentRow.knowledge_document_id == document.id,
            KnowledgeAttachmentRow.extraction_id == document.published_extraction_id,
            KnowledgeAttachmentRow.sha256.in_(refs),
            KnowledgeAttachmentRow.state == "ready",
            KnowledgeAttachmentRow.upload_state == "stored",
            KnowledgeAttachmentRow.quota_state == "committed",
        )
        if lock:
            statement = statement.with_for_update()
        rows = list((await session.scalars(statement)).all())
        by_ref = {row.sha256: row for row in rows}
        if set(by_ref) != refs:
            raise _invalid("content 引用了当前文档不可用的附件")
        return by_ref

    @staticmethod
    async def _replace_attachments(
        session: AsyncSession,
        document: KnowledgeDocumentRow,
        segment: KnowledgeSegmentRow,
        attachments: tuple[_ManualAttachment, ...],
        attachment_rows: dict[str, KnowledgeAttachmentRow],
    ) -> None:
        """Atomically replace ordered bindings for edited Markdown."""

        await session.execute(
            delete(KnowledgeSegmentAttachmentRow).where(
                KnowledgeSegmentAttachmentRow.segment_id == segment.id,
            )
        )
        if not attachments:
            return
        extraction_id = document.published_extraction_id
        if extraction_id is None:  # guarded by _validated_attachment_rows
            raise _invalid("content 引用了当前文档不可用的附件")
        segment.extraction_id = extraction_id
        await session.flush()
        session.add_all(
            [
                KnowledgeSegmentAttachmentRow(
                    project_id=document.project_id,
                    knowledge_base_id=document.knowledge_base_id,
                    knowledge_document_id=document.id,
                    extraction_id=extraction_id,
                    segment_id=segment.id,
                    attachment_id=attachment_rows[attachment.ref].id,
                    position=position,
                    alt_text=attachment.alt_text,
                )
                for position, attachment in enumerate(attachments, start=1)
            ]
        )

    async def _binding_unchanged(
        self,
        session: AsyncSession,
        base_id: UUID,
        embedded_model_id: UUID | None,
    ) -> bool:
        """True while the base still binds the model the vector came from."""

        current = await session.scalar(select(KnowledgeBaseRow.embedding_model_id).where(KnowledgeBaseRow.id == base_id))
        return current == embedded_model_id

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
        authority: KnowledgeProjectAuthority | None,
    ) -> _EmbeddedContent:
        """Embed per the document's frozen chunking mode.

        General mode embeds the segment itself; parent_child mode re-splits the
        content with the document's frozen child parameters and embeds the
        children — the parent row keeps a NULL vector.
        """

        index_text, token_count, children = _manual_derivation(document, content)

        async def before_batch() -> None:
            if authority is None:
                return
            try:
                async with self._session_factory() as session, session.begin():
                    await revalidate_project_authority(authority, session, project_id=document.project_id)
            except SQLAlchemyError:
                raise _storage_unavailable() from None

        if document.chunking_mode != "parent_child":
            if available_vector_entries < 1:
                raise _quota_exceeded(
                    self._settings.max_segments_per_document,
                )
            return _EmbeddedContent(
                parent_vector=(
                    await self._client.embed(
                        material,
                        [index_text],
                        batch_guard=before_batch,
                    )
                )[0],
                index_text=index_text,
                token_count=token_count,
                children=(),
                child_vectors=(),
            )
        if not children:  # pragma: no cover - non-empty content always splits
            raise _invalid("内容未能切分出子块")
        if len(children) > available_vector_entries:
            raise _quota_exceeded(
                self._settings.max_segments_per_document,
            )
        child_vectors = await self._client.embed(
            material,
            [child.index_text for child in children],
            batch_guard=before_batch,
        )
        return _EmbeddedContent(
            parent_vector=None,
            index_text=index_text,
            token_count=token_count,
            children=children,
            child_vectors=tuple(child_vectors),
        )

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
                    content=child.content,
                    index_text=child.index_text,
                    token_count=child.token_count,
                    source_spans=[],
                    word_count=len(child.content),
                    embedding=vector,
                    lexical_tsv=func.to_tsvector(
                        "simple",
                        lexical_index_input(child.index_text),
                    ),
                    lexical_version=KNOWLEDGE_LEXICAL_VERSION,
                )
                for position, (child, vector) in enumerate(
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
