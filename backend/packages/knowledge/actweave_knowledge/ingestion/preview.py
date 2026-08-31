"""Synchronous chunk preview: extract → clean → split, nothing persisted.

The preview and the ingestion pipeline share :func:`extract_clean_split`, so
for identical parameters the previewed chunks are exactly the segments a real
ingestion would publish. The caller stages the file like an upload and owns
temp-file cleanup; this module never touches the database, the object store,
or the task queue.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..contracts import (
    KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR,
    KNOWLEDGE_PARSE_FAILED,
    KnowledgeChunkingMode,
    KnowledgeChunkPreview,
    KnowledgeChunkPreviewChunk,
    KnowledgeChunkPreviewRequest,
    KnowledgeError,
    KnowledgeSettings,
)
from ..documents.service import (
    validated_chunk_settings,
    validated_chunking_mode,
    validated_original_name,
    validated_preprocessing_rules,
    validated_upload_size,
)
from .cleaner import clean_blocks
from .extractor import extract_blocks
from .splitter import SegmentDraft, attach_children, split_blocks

PREVIEW_CHUNK_LIMIT = 10


def extract_clean_split(
    source_path: Path,
    original_name: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
    chunk_separator: str,
    remove_extra_spaces: bool,
    remove_urls_emails: bool,
    max_total_chars: int,
    chunking_mode: KnowledgeChunkingMode = "general",
    child_chunk_size: int = 500,
    child_chunk_separator: str = KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR,
) -> list[SegmentDraft]:
    """Blocking single source of truth for ingestion and preview chunking."""

    blocks = extract_blocks(
        source_path,
        Path(original_name).suffix.lower(),
        max_total_chars=max_total_chars,
    )
    cleaned = clean_blocks(
        blocks,
        remove_extra_spaces=remove_extra_spaces,
        remove_urls_emails=remove_urls_emails,
    )
    drafts = split_blocks(
        cleaned,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separator=chunk_separator,
    )
    if chunking_mode != "parent_child":
        return drafts
    return attach_children(
        drafts,
        child_chunk_size=child_chunk_size,
        child_chunk_separator=child_chunk_separator,
    )


def _run_preview(request: KnowledgeChunkPreviewRequest, settings: KnowledgeSettings) -> KnowledgeChunkPreview:
    drafts = extract_clean_split(
        request.source_path,
        request.original_name,
        chunk_size=request.chunk_size,
        chunk_overlap=request.chunk_overlap,
        chunk_separator=request.chunk_separator,
        remove_extra_spaces=request.remove_extra_spaces,
        remove_urls_emails=request.remove_urls_emails,
        # Same budget as ingestion, so the preview fails where ingestion would.
        max_total_chars=settings.max_segments_per_document * request.chunk_size,
        chunking_mode=request.chunking_mode,
        child_chunk_size=request.child_chunk_size,
        child_chunk_separator=request.child_chunk_separator,
    )
    if not drafts:
        raise KnowledgeError(KNOWLEDGE_PARSE_FAILED, "文件没有可提取的文本")
    return KnowledgeChunkPreview(
        total=len(drafts),
        chunks=tuple(
            KnowledgeChunkPreviewChunk(
                position=draft.position,
                content=draft.content,
                word_count=len(draft.content),
                child_contents=draft.children,
            )
            for draft in drafts[:PREVIEW_CHUNK_LIMIT]
        ),
    )


async def preview_document_chunks(
    request: KnowledgeChunkPreviewRequest,
    settings: KnowledgeSettings,
) -> KnowledgeChunkPreview:
    """Validate like an upload, then extract, clean, and split off-thread."""

    original_name = validated_original_name(request.original_name)
    size_bytes = validated_upload_size(request.size_bytes, settings)
    chunk_size, chunk_overlap, chunk_separator = validated_chunk_settings(request.chunk_size, request.chunk_overlap, request.chunk_separator)
    remove_extra_spaces, remove_urls_emails = validated_preprocessing_rules(request.remove_extra_spaces, request.remove_urls_emails)
    chunking_mode, child_chunk_size, child_chunk_separator = validated_chunking_mode(
        request.chunking_mode,
        request.child_chunk_size,
        request.child_chunk_separator,
        chunk_size=chunk_size,
    )
    validated = KnowledgeChunkPreviewRequest(
        original_name=original_name,
        source_path=request.source_path,
        size_bytes=size_bytes,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        chunk_separator=chunk_separator,
        remove_extra_spaces=remove_extra_spaces,
        remove_urls_emails=remove_urls_emails,
        chunking_mode=chunking_mode,
        child_chunk_size=child_chunk_size,
        child_chunk_separator=child_chunk_separator,
    )
    return await asyncio.to_thread(_run_preview, validated, settings)
