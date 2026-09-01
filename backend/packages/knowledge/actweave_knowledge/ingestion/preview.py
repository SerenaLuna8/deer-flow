"""Stateless P1 extraction and P3 splitting for Gateway chunk previews."""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..asyncio_utils import run_sync_to_completion
from ..contracts import (
    KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_PARSE_FAILED,
    KnowledgeChunkingMode,
    KnowledgeChunkPreview,
    KnowledgeChunkPreviewAttachment,
    KnowledgeChunkPreviewChunk,
    KnowledgeChunkPreviewRequest,
    KnowledgeError,
    KnowledgePreviewAttachment,
    KnowledgePreviewTableSource,
    KnowledgeSettings,
)
from ..documents.service import (
    validated_chunk_settings,
    validated_chunking_mode,
    validated_original_name,
    validated_preprocessing_rules,
    validated_upload_size,
)
from ..extraction.contracts import Document, ExtractionLimits, ExtractSetting, HeaderRule, LocalAttachment, ParseWarning
from ..extraction.registry import ExtractorRegistry, default_registry
from ..extraction.runtime import ParserSlots, run_extraction
from .cleaner import clean_blocks
from .extractor import extract_blocks
from .preview_assets import make_preview_assets
from .profiles import ProcessingParameters, preview_fingerprint, resolve_processing_profile
from .splitter import SegmentDraft, attach_children, split_blocks, split_documents

PREVIEW_CHUNK_LIMIT = 10
PREVIEW_TIMEOUT_SECONDS = 120


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
    """Legacy Worker adapter retained until P3-T5 switches formal ingestion."""

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


def _remove_temp_dir(path: str | Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _parameters(request: KnowledgeChunkPreviewRequest) -> ProcessingParameters:
    if request.processing_profile is not None:
        return ProcessingParameters.model_validate(request.processing_profile)
    chunk_size, chunk_overlap, chunk_separator = validated_chunk_settings(
        request.chunk_size,
        request.chunk_overlap,
        request.chunk_separator,
    )
    remove_extra_spaces, remove_urls_emails = validated_preprocessing_rules(
        request.remove_extra_spaces,
        request.remove_urls_emails,
    )
    mode, child_size, child_separator = validated_chunking_mode(
        request.chunking_mode,
        request.child_chunk_size,
        request.child_chunk_separator,
        chunk_size=chunk_size,
    )
    return ProcessingParameters(
        size=chunk_size,
        overlap=chunk_overlap,
        separator=chunk_separator,
        mode=mode,
        child_size=child_size,
        child_separator=child_separator,
        remove_extra_spaces=remove_extra_spaces,
        remove_urls_emails=remove_urls_emails,
    )


def _warnings(documents: tuple[Document, ...], extraction: tuple[ParseWarning, ...]) -> tuple[ParseWarning, ...]:
    result: list[ParseWarning] = []
    seen: set[str] = set()
    for warning in (*extraction, *(item for document in documents for item in document.warnings)):
        identity = warning.model_dump_json()
        if identity not in seen:
            seen.add(identity)
            result.append(warning)
    return tuple(result)


def _table_sources(documents: tuple[Document, ...], rules: tuple[HeaderRule, ...], extension: str) -> tuple[KnowledgePreviewTableSource, ...]:
    if extension not in {".csv", ".xls", ".xlsx"}:
        return ()
    sheets: list[str | None] = []
    if extension == ".csv":
        sheets.append(None)
    else:
        for document in documents:
            sheet = next((span.location.get("sheet") for span in document.source_spans if isinstance(span.location.get("sheet"), str)), None)
            if sheet is not None and sheet not in sheets:
                sheets.append(sheet)
        for rule in rules:
            if rule.sheet is not None and rule.sheet not in sheets:
                sheets.append(rule.sheet)
    result = []
    for sheet in sheets:
        rule = next((item for item in rules if item.sheet == sheet), next((item for item in rules if item.sheet is None), HeaderRule(sheet=sheet)))
        header = next(
            (document for document in documents if document.kind == "table_header" and next((span.location.get("sheet") for span in document.source_spans if "sheet" in span.location), None) == sheet),
            None,
        )
        if header is None:
            row = None
            cells: tuple[str, ...] = ()
        else:
            source_spans = sorted(
                (span for span in header.source_spans if span.role == "source"),
                key=lambda span: int(span.location.get("column", 0)),
            )
            row = next((int(span.location["row"]) for span in source_spans if "row" in span.location), None)
            cells = tuple(header.page_content[span.start : span.end] for span in source_spans)
        result.append(
            KnowledgePreviewTableSource(
                sheet=sheet,
                header_mode=rule.mode,
                header_row=row,
                header_cells=cells,
            )
        )
    return tuple(result)


async def preview_document_chunks(
    request: KnowledgeChunkPreviewRequest,
    settings: KnowledgeSettings,
    *,
    capability_revision: str,
    parser_slots: ParserSlots,
    guard: Callable[[], Awaitable[None]],
    registry: ExtractorRegistry | None = None,
) -> KnowledgeChunkPreview:
    """Run the real isolated parser and split its immutable result, temp-only."""

    if not isinstance(capability_revision, str) or re.fullmatch(r"[0-9a-f]{64}", capability_revision) is None:
        raise ValueError("invalid capability revision")
    original_name = validated_original_name(request.original_name)
    validated_upload_size(request.size_bytes, settings)
    extension = Path(original_name).suffix.lower()
    parameters = _parameters(request)
    try:
        profile = await run_sync_to_completion(
            resolve_processing_profile,
            settings,
            parameters,
            registry or default_registry(),
            extension=extension,
        )
    except ValueError:
        raise KnowledgeError(KNOWLEDGE_INVALID_REQUEST, "分段参数无效") from None
    work_dir = Path(
        await run_sync_to_completion(
            tempfile.mkdtemp,
            prefix="actweave-knowledge-preview-",
            cleanup_on_cancel=_remove_temp_dir,
        )
    )
    assets: list[LocalAttachment] = []

    async def on_asset(asset: LocalAttachment) -> None:
        if not any(item.attachment.ref == asset.attachment.ref for item in assets):
            assets.append(asset)

    try:
        async with parser_slots:
            result = await run_extraction(
                ExtractSetting(
                    source_path=request.source_path,
                    original_name=original_name,
                    datasource_type="file",
                    profile=profile.parse,
                ),
                work_dir=work_dir,
                limits=ExtractionLimits(max_source_bytes=settings.upload_max_bytes),
                timeout_seconds=PREVIEW_TIMEOUT_SECONDS,
                on_asset=on_asset,
                guard=guard,
            )
        await guard()
        drafts = await run_sync_to_completion(split_documents, result.documents, profile=profile.chunk)
        if not drafts:
            raise KnowledgeError(KNOWLEDGE_PARSE_FAILED, "文件没有可提取的文本")
        visible = drafts[:PREVIEW_CHUNK_LIMIT]
        selected_refs = tuple(occurrence.ref for draft in visible for occurrence in draft.attachments)
        projected, omitted = await run_sync_to_completion(
            make_preview_assets,
            tuple(assets),
            work_dir=work_dir,
            selected_refs=selected_refs,
        )
        await guard()
        return KnowledgeChunkPreview(
            total=len(drafts),
            chunks=tuple(
                KnowledgeChunkPreviewChunk(
                    position=draft.position,
                    content=draft.content,
                    word_count=len(draft.content),
                    child_contents=tuple(child.content for child in draft.children),
                    token_count=draft.token_count,
                    source_spans=draft.source_spans,
                    attachments=tuple(KnowledgeChunkPreviewAttachment(ref=item.ref, alt_text=item.alt_text) for item in draft.attachments),
                )
                for draft in visible
            ),
            preview_fingerprint=preview_fingerprint(
                source_sha256=result.source_sha256,
                extension=extension,
                profile=profile,
                capability_revision=capability_revision,
            ),
            source_sha256=result.source_sha256,
            effective_profile=profile,
            warnings=_warnings(result.documents, result.warnings),
            preview_attachments=tuple(KnowledgePreviewAttachment(**item) for item in projected),
            omitted_preview_attachment_count=omitted,
            table_sources=_table_sources(result.documents, profile.parse.header_rules, extension),
        )
    finally:
        await run_sync_to_completion(shutil.rmtree, work_dir, ignore_errors=True)
