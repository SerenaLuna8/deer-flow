"""Knowledge HTTP adapters: project routes over the Knowledge module.

Every route reuses server-resolved project context with ``shared_assets.read``
or ``shared_assets.edit``. Handlers stay thin: authorization first, then one
:class:`KnowledgeModule` call, then a ``{code, message, request_id}`` error
body on :class:`KnowledgeError`. Retrieval model administration lives in
``app.model_registry.gateway``; the project surface here only reads the
registry's active options for binding.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR,
    KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR,
    KNOWLEDGE_DISABLED,
    KNOWLEDGE_EMBEDDING_FAILED,
    KNOWLEDGE_FORBIDDEN,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MODEL_UNAVAILABLE,
    KNOWLEDGE_NAME_CONFLICT,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_PARSE_FAILED,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KNOWLEDGE_RERANK_FAILED,
    KNOWLEDGE_SEARCH_FAILED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KNOWLEDGE_TASK_FAILED,
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgeBaseView,
    KnowledgeChunkPreview,
    KnowledgeChunkPreviewRequest,
    KnowledgeCitation,
    KnowledgeDocumentUpload,
    KnowledgeDocumentView,
    KnowledgeError,
    KnowledgeMetadataBatchPatch,
    KnowledgeMetadataFieldView,
    KnowledgeMetadataFilter,
    KnowledgeModule,
    KnowledgeQueryView,
    KnowledgeReparseRequest,
    KnowledgeSearchDiagnostics,
    KnowledgeSearchRequest,
    KnowledgeSegmentCreate,
    KnowledgeSegmentDetail,
    KnowledgeSegmentUpdate,
    KnowledgeSegmentView,
    KnowledgeTaskProgress,
)
from actweave_knowledge import (
    KnowledgeBaseReparseRequest as KnowledgeBaseReparseContract,
)
from actweave_knowledge.extraction.contracts import ParseWarning, ProcessingProfile
from actweave_knowledge.ingestion.profiles import FileCapabilities, ProcessingParameters
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.deps import get_current_user_from_request, project_session
from app.knowledge.authority import ProjectKnowledgeAuthority
from app.knowledge_settings.service import read_active_summary_model
from app.model_registry.service import (
    RetrievalModelOption,
    list_active_retrieval_model_options,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext, resolve_project_context
from app.projects.errors import (
    ProjectDatabaseUnavailable,
    ProjectForbidden,
    ProjectNotFound,
)
from deerflow.trace_context import generate_trace_id, get_current_trace_id, normalize_trace_id

project_router = APIRouter(
    prefix="/api/projects/{project_id}/knowledge",
    tags=["project-knowledge"],
)

_HTTP_STATUS_BY_CODE = {
    KNOWLEDGE_DISABLED: 404,
    KNOWLEDGE_NOT_FOUND: 404,
    KNOWLEDGE_FORBIDDEN: 403,
    KNOWLEDGE_NAME_CONFLICT: 409,
    KNOWLEDGE_CONFLICT: 409,
    KNOWLEDGE_INVALID_REQUEST: 422,
    KNOWLEDGE_QUOTA_EXCEEDED: 429,
    KNOWLEDGE_PARSE_FAILED: 422,
    KNOWLEDGE_MODEL_UNAVAILABLE: 503,
    KNOWLEDGE_STORAGE_UNAVAILABLE: 503,
    KNOWLEDGE_EMBEDDING_FAILED: 502,
    KNOWLEDGE_RERANK_FAILED: 502,
    KNOWLEDGE_SEARCH_FAILED: 502,
    KNOWLEDGE_TASK_FAILED: 502,
}


def knowledge_http_exception(error: KnowledgeError, request_id: str) -> HTTPException:
    return HTTPException(
        status_code=_HTTP_STATUS_BY_CODE.get(error.code, 500),
        detail={
            "code": error.code,
            "message": error.message,
            "request_id": request_id,
        },
    )


def _knowledge_disabled_exception(request_id: str) -> HTTPException:
    return knowledge_http_exception(
        KnowledgeError(KNOWLEDGE_DISABLED, "Knowledge 功能未启用"),
        request_id,
    )


def _request_id(request: Request) -> str:
    return get_current_trace_id() or normalize_trace_id(request.headers.get("x-trace-id")) or generate_trace_id()


def get_knowledge_module(request: Request) -> KnowledgeModule:
    """Return the Gateway-owned module; a missing module means feature off."""

    module = getattr(request.app.state, "knowledge_module", None)
    if module is None:
        raise _knowledge_disabled_exception(_request_id(request))
    return module


def _knowledge_read_authority(context: ProjectContext) -> ProjectKnowledgeAuthority:
    return ProjectKnowledgeAuthority(context, Capability.SHARED_ASSETS_READ)


def _knowledge_edit_authority(context: ProjectContext) -> ProjectKnowledgeAuthority:
    return ProjectKnowledgeAuthority(context, Capability.SHARED_ASSETS_EDIT)


async def _authenticated_identity(
    request: Request,
    user=Depends(get_current_user_from_request),  # noqa: ANN001 - host auth user shape
) -> tuple[uuid.UUID, str]:
    try:
        user_id = uuid.UUID(str(user.id))
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=404,
            detail={
                "code": KNOWLEDGE_NOT_FOUND,
                "message": "资源不存在",
                "request_id": _request_id(request),
            },
        ) from None
    return user_id, _request_id(request)


async def require_project_knowledge_read(
    project_id: uuid.UUID,
    identity: Annotated[tuple[uuid.UUID, str], Depends(_authenticated_identity)],
    session: Annotated[AsyncSession, Depends(project_session)],
) -> ProjectContext:
    """Resolve trusted project context and require ``shared_assets.read``.

    Outsiders and missing projects collapse to ``KNOWLEDGE_NOT_FOUND``; a
    current member lacking the capability receives ``KNOWLEDGE_FORBIDDEN``
    (403), matching the platform authorization default.
    """

    user_id, request_id = identity
    try:
        context = await resolve_project_context(session, user_id, project_id, request_id)
        context.require(Capability.SHARED_ASSETS_READ)
        return context
    except ProjectNotFound:
        raise knowledge_http_exception(
            KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在"),
            request_id,
        ) from None
    except ProjectForbidden:
        raise knowledge_http_exception(
            KnowledgeError(KNOWLEDGE_FORBIDDEN, "没有权限执行此操作"),
            request_id,
        ) from None
    except ProjectDatabaseUnavailable:
        raise knowledge_http_exception(
            KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用"),
            request_id,
        ) from None


async def require_project_knowledge_edit(
    project_id: uuid.UUID,
    identity: Annotated[tuple[uuid.UUID, str], Depends(_authenticated_identity)],
    session: Annotated[AsyncSession, Depends(project_session)],
) -> ProjectContext:
    """Resolve trusted project context and require ``shared_assets.edit``."""

    user_id, request_id = identity
    try:
        context = await resolve_project_context(session, user_id, project_id, request_id)
        context.require(Capability.SHARED_ASSETS_EDIT)
        return context
    except ProjectNotFound:
        raise knowledge_http_exception(
            KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在"),
            request_id,
        ) from None
    except ProjectForbidden:
        raise knowledge_http_exception(
            KnowledgeError(KNOWLEDGE_FORBIDDEN, "没有权限执行此操作"),
            request_id,
        ) from None
    except ProjectDatabaseUnavailable:
        raise knowledge_http_exception(
            KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用"),
            request_id,
        ) from None


# -- request temp files -------------------------------------------------------

_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024
_MULTIPART_OVERHEAD_ALLOWANCE_BYTES = 1024 * 1024


async def _new_request_temp_path(suffix: str = "") -> Path:
    """Create an empty per-request temp file and return its path."""

    def _create() -> str:
        handle = tempfile.NamedTemporaryFile(prefix="actweave-knowledge-", suffix=suffix, delete=False)
        handle.close()
        return handle.name

    return Path(await asyncio.to_thread(_create))


async def _remove_request_temp_path(path: Path) -> None:
    def _remove() -> None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    await asyncio.to_thread(_remove)


class _TempFileResponse(FileResponse):
    """A FileResponse that always removes its temp file when the send ends.

    Starlette runs ``background`` only after a fully successful stream; a
    client abort mid-body or a malformed/unsatisfiable ``Range`` early-exit
    skips it, which would leak one full document copy per aborted download.
    """

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Idempotent: removal of an already-removed file is a no-op.
            await _remove_request_temp_path(Path(self.path))


async def _stage_upload_to_temp(file: UploadFile, max_bytes: int, request_id: str) -> tuple[Path, int]:
    """Write the request body to a temp file, enforcing the size cap while copying.

    The temp file is removed on every failure path (including request
    cancellation); on success the caller owns the cleanup.
    """

    path = await _new_request_temp_path()
    size = 0
    try:
        handle = await asyncio.to_thread(path.open, "wb")
        try:
            while chunk := await file.read(_UPLOAD_READ_CHUNK_BYTES):
                size += len(chunk)
                if size > max_bytes:
                    raise knowledge_http_exception(
                        KnowledgeError(KNOWLEDGE_INVALID_REQUEST, f"文件大小超过上限 {max_bytes} 字节"),
                        request_id,
                    )
                await asyncio.to_thread(handle.write, chunk)
        finally:
            await asyncio.to_thread(handle.close)
    except BaseException:
        await _remove_request_temp_path(path)
        raise
    return path, size


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class KnowledgeModelOptionResponse(_StrictModel):
    """One active registry model a member may bind to a Knowledge Base."""

    id: uuid.UUID
    provider_name: str
    model_name: str
    embedding_dimension: int | None


class KnowledgeSummaryModelResponse(_StrictModel):
    model_name: str
    display_name: str


class KnowledgeModelOptionsResponse(_StrictModel):
    embedding_models: list[KnowledgeModelOptionResponse]
    reranker_models: list[KnowledgeModelOptionResponse]
    summary_model: KnowledgeSummaryModelResponse | None
    request_id: str


class KnowledgeBaseCreateRequest(_StrictModel):
    name: str
    # UUIDs arrive as JSON strings; strict mode would reject the coercion.
    embedding_model_id: Annotated[uuid.UUID, Field(strict=False)] | None = None
    reranker_model_id: Annotated[uuid.UUID, Field(strict=False)] | None = None
    description: str = ""
    # hybrid adds the lexical recall route; semantic is the default.
    retrieval_mode: Literal["semantic", "hybrid"] = "semantic"


class KnowledgeBaseUpdateRequest(_StrictModel):
    name: str | None = None
    description: str | None = None
    status: Literal["active", "disabled"] | None = None
    default_top_k: int | None = None
    # Integers (e.g. 0) are valid thresholds; strict float would reject them.
    default_score_threshold: Annotated[float, Field(strict=False)] | None = None
    # Optional relative cutoff (fraction of the base's best native score):
    # set a value in (0, 1], or clear it to switch the cut off.
    default_relative_cutoff: Annotated[float, Field(strict=False)] | None = None
    clear_relative_cutoff: bool = False
    # Only an empty, unconfigured base can receive its first embedding binding.
    embedding_model_id: Annotated[uuid.UUID, Field(strict=False)] | None = None
    # Optional reranker rebinding: set an ID, or clear the binding entirely.
    reranker_model_id: Annotated[uuid.UUID, Field(strict=False)] | None = None
    clear_reranker_model: bool = False
    retrieval_mode: Literal["semantic", "hybrid"] | None = None
    summary_index_enabled: bool | None = None


class KnowledgeBaseRebuildRequest(_StrictModel):
    """Rebind the base to an embedding model and re-embed every document."""

    # UUIDs arrive as JSON strings; strict mode would reject the coercion.
    embedding_model_id: Annotated[uuid.UUID, Field(strict=False)]


class KnowledgeBaseItemResponse(_StrictModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str
    embedding_model_id: uuid.UUID | None
    reranker_model_id: uuid.UUID | None
    retrieval_mode: Literal["semantic", "hybrid"]
    summary_index_enabled: bool
    status: Literal["active", "disabled", "deleting"]
    document_count: int
    default_top_k: int
    default_score_threshold: float
    default_relative_cutoff: float | None
    # Base-wide chunking mode shared by every document; null while the base
    # holds no live document (the next upload determines it).
    chunking_mode: Literal["general", "parent_child"] | None
    delete_error: str | None
    created_at: datetime
    updated_at: datetime


class KnowledgeBaseListResponse(_StrictModel):
    items: list[KnowledgeBaseItemResponse]
    total: int
    page: int
    page_size: int
    request_id: str


class KnowledgeBaseMutationResponse(_StrictModel):
    item: KnowledgeBaseItemResponse
    request_id: str


class KnowledgeSummaryBackfillResponse(_StrictModel):
    accepted_document_count: int
    skipped_document_ids: list[uuid.UUID]


class KnowledgeBaseUpdateResponse(KnowledgeBaseMutationResponse):
    summary_backfill: KnowledgeSummaryBackfillResponse | None


class KnowledgeBaseRebuildResponse(_StrictModel):
    """Re-embed admission outcome: the rebound base plus per-document counts."""

    item: KnowledgeBaseItemResponse
    accepted_document_count: int
    # Never-published failed documents stay failed; re-parsing the original
    # file is a separate explicit action, so the UI can list them.
    skipped_document_ids: list[uuid.UUID]
    request_id: str


class KnowledgeBaseRelexResponse(_StrictModel):
    """Lexical re-derivation admission outcome; documents stay ready."""

    accepted_document_count: int
    up_to_date_document_count: int
    skipped_document_ids: list[uuid.UUID]
    request_id: str


class KnowledgeTaskProgressResponse(_StrictModel):
    """Progress of the open indexing task bound to the document's current
    generation; ``total_units`` stays null while no verifiable total exists."""

    kind: Literal["ingest_document", "reembed_document", "summarize_document", "relex_document"]
    status: Literal["queued", "running", "retry_wait", "failed"]
    stage: Literal[
        "queued",
        "reading_source",
        "extracting_splitting",
        "loading_segments",
        "summarizing",
        "embedding",
        "publishing",
        "done",
    ]
    completed_units: int
    total_units: int | None
    attempt_count: int
    max_attempts: int
    target_version: int
    next_attempt_at: datetime | None


class KnowledgeDocumentItemResponse(_StrictModel):
    parsing_profile: ProcessingProfile | None
    parse_warnings: list[ParseWarning]
    chunk_size_unit: Literal["character", "token"]
    tokenizer_profile_id: str | None
    id: uuid.UUID
    project_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    name: str
    original_name: str
    media_type: str | None
    size_bytes: int
    status: Literal["uploading", "queued", "processing", "ready", "failed", "deleting"]
    enabled: bool
    version: int
    chunk_size: int
    chunk_overlap: int
    chunk_separator: str
    remove_extra_spaces: bool
    remove_urls_emails: bool
    chunking_mode: Literal["general", "parent_child"]
    child_chunk_size: int
    child_chunk_separator: str
    segment_count: int
    word_count: int
    hit_count: int
    doc_metadata: dict[str, Any]
    error_message: str | None
    delete_error: str | None
    task_progress: KnowledgeTaskProgressResponse | None
    content_initialized: bool
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentListResponse(_StrictModel):
    items: list[KnowledgeDocumentItemResponse]
    total: int
    page: int
    page_size: int
    request_id: str


class KnowledgeDocumentMutationResponse(_StrictModel):
    item: KnowledgeDocumentItemResponse
    request_id: str


class KnowledgeDocumentAttachmentItemResponse(_StrictModel):
    attachment_id: uuid.UUID
    ref: str
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    width: int
    height: int


class KnowledgeDocumentAttachmentListResponse(_StrictModel):
    items: list[KnowledgeDocumentAttachmentItemResponse]
    document_version: int
    request_id: str


class KnowledgeDocumentRenameRequest(_StrictModel):
    name: str


class KnowledgeMetadataFieldCreateRequest(_StrictModel):
    name: str
    field_type: Literal["string", "number", "time"]


class KnowledgeMetadataFieldRenameRequest(_StrictModel):
    name: str


class KnowledgeMetadataFieldItemResponse(_StrictModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    name: str
    field_type: Literal["string", "number", "time"]
    created_at: datetime
    updated_at: datetime


class KnowledgeMetadataFieldListResponse(_StrictModel):
    items: list[KnowledgeMetadataFieldItemResponse]
    request_id: str


class KnowledgeMetadataFieldMutationResponse(_StrictModel):
    item: KnowledgeMetadataFieldItemResponse
    request_id: str


class KnowledgeMetadataFieldDeleteResponse(_StrictModel):
    request_id: str


class KnowledgeDocumentMetadataRequest(_StrictModel):
    """Partial metadata update: ``null`` removes the key, others set it.

    Keys must match the base's defined field names; type and bound checks
    live in the package.
    """

    # Numbers arrive as JSON ints or floats; strict mode keeps their type.
    values: dict[str, str | int | float | None]


class KnowledgeDocumentsMetadataPatchRequest(_StrictModel):
    """One bounded common patch for documents of one base (all-or-nothing).

    Untouched keys stay, ``null`` removes the key, builtin field names are
    rejected; document/field caps and type checks live in the package.
    """

    document_ids: list[Annotated[uuid.UUID, Field(strict=False)]]
    values: dict[str, str | int | float | None]


class KnowledgeFilterFieldItemResponse(_StrictModel):
    kind: Literal["custom", "builtin"]
    name: str
    field_type: Literal["string", "number", "time"]
    operators: list[Literal["eq", "contains", "gte", "lte"]]
    writable: bool


class KnowledgeBaseFilterFieldsResponse(_StrictModel):
    knowledge_base_id: uuid.UUID
    fields: list[KnowledgeFilterFieldItemResponse]


class KnowledgeFilterFieldsResponse(_StrictModel):
    bases: list[KnowledgeBaseFilterFieldsResponse]
    request_id: str


_BATCH_DOCUMENT_IDS = Annotated[
    list[Annotated[uuid.UUID, Field(strict=False)]],
    Field(min_length=1, max_length=100),
]


class KnowledgeDocumentBatchStatusRequest(_StrictModel):
    document_ids: _BATCH_DOCUMENT_IDS
    enabled: bool


class KnowledgeDocumentBatchDeleteRequest(_StrictModel):
    document_ids: _BATCH_DOCUMENT_IDS


class KnowledgeDocumentBatchResponse(_StrictModel):
    items: list[KnowledgeDocumentItemResponse]
    request_id: str


class KnowledgePreviewSourceSpanResponse(_StrictModel):
    block_id: str
    start: int
    end: int
    location: dict[str, str | int]
    role: Literal["source", "context_prefix"]


class KnowledgePreviewLogicalAttachmentResponse(_StrictModel):
    ref: str
    alt_text: str


class KnowledgePreviewAttachmentResponse(_StrictModel):
    ref: str
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    data_base64: str


class KnowledgePreviewTableSourceResponse(_StrictModel):
    sheet: str | None
    header_mode: Literal["auto", "none", "explicit"]
    header_row: int | None
    header_cells: list[str]


class KnowledgeChunkPreviewItemResponse(_StrictModel):
    position: int
    content: str
    word_count: int
    child_contents: list[str]
    token_count: int
    source_spans: list[KnowledgePreviewSourceSpanResponse]
    attachments: list[KnowledgePreviewLogicalAttachmentResponse]


class KnowledgeChunkPreviewResponse(_StrictModel):
    items: list[KnowledgeChunkPreviewItemResponse]
    total: int
    preview_fingerprint: str
    source_sha256: str
    effective_profile: ProcessingProfile
    warnings: list[ParseWarning]
    preview_attachments: list[KnowledgePreviewAttachmentResponse]
    omitted_preview_attachment_count: int
    table_sources: list[KnowledgePreviewTableSourceResponse]
    request_id: str


def _preview_response_payload(preview: KnowledgeChunkPreview) -> dict[str, Any]:
    if preview.effective_profile is None:
        raise RuntimeError("preview response is missing its effective profile")
    return {
        "items": [
            KnowledgeChunkPreviewItemResponse(
                position=chunk.position,
                content=chunk.content,
                word_count=chunk.word_count,
                child_contents=list(chunk.child_contents),
                token_count=chunk.token_count,
                source_spans=[KnowledgePreviewSourceSpanResponse(**span.model_dump(mode="json")) for span in chunk.source_spans],
                attachments=[
                    KnowledgePreviewLogicalAttachmentResponse(
                        ref=attachment.ref,
                        alt_text=attachment.alt_text,
                    )
                    for attachment in chunk.attachments
                ],
            )
            for chunk in preview.chunks
        ],
        "total": preview.total,
        "preview_fingerprint": preview.preview_fingerprint,
        "source_sha256": preview.source_sha256,
        "effective_profile": preview.effective_profile,
        "warnings": list(preview.warnings),
        "preview_attachments": [
            KnowledgePreviewAttachmentResponse(
                ref=attachment.ref,
                media_type=attachment.media_type,
                data_base64=attachment.data_base64,
            )
            for attachment in preview.preview_attachments
        ],
        "omitted_preview_attachment_count": preview.omitted_preview_attachment_count,
        "table_sources": [
            KnowledgePreviewTableSourceResponse(
                sheet=table.sheet,
                header_mode=table.header_mode,
                header_row=table.header_row,
                header_cells=list(table.header_cells),
            )
            for table in preview.table_sources
        ],
    }


_LEGACY_PROCESSING_FIELDS = {
    "chunk_size": "size",
    "chunk_overlap": "overlap",
    "chunk_separator": "separator",
    "chunking_mode": "mode",
    "child_chunk_size": "child_size",
    "child_chunk_separator": "child_separator",
    "remove_extra_spaces": "remove_extra_spaces",
    "remove_urls_emails": "remove_urls_emails",
}


def processing_parameters(legacy: dict, submitted: dict | ProcessingParameters | None) -> ProcessingParameters:
    """Merge only explicitly supplied legacy fields, rejecting conflicting values."""
    try:
        user = ProcessingParameters.model_validate(submitted or {})
        new = user.model_dump(exclude_unset=True)
        merged = {_LEGACY_PROCESSING_FIELDS[key]: value for key, value in legacy.items() if key in _LEGACY_PROCESSING_FIELDS}
        if any(key in new and new[key] != value for key, value in merged.items()):
            raise ValueError("conflicting processing parameters")
        merged.update(new)
        return ProcessingParameters.model_validate(merged)
    except (ValueError, ValidationError):
        raise KnowledgeError(KNOWLEDGE_INVALID_REQUEST, "分段参数无效或与 processing_profile 冲突") from None


def multipart_processing_options(*, raw_profile: str | None, expected_fingerprint: str | None, form_keys: set[str], legacy_values: dict[str, object]) -> tuple[ProcessingParameters | None, str | None]:
    """Apply one strict multipart profile/fingerprint policy for both routes."""

    parameters = None
    if raw_profile is not None:
        try:
            submitted = json.loads(raw_profile)
            if not isinstance(submitted, dict):
                raise ValueError("processing_profile must be an object")
            parameters = processing_parameters({key: value for key, value in legacy_values.items() if key in form_keys}, submitted)
        except (ValueError, KnowledgeError):
            raise KnowledgeError(KNOWLEDGE_INVALID_REQUEST, "分段参数无效或冲突") from None
    if expected_fingerprint is not None and re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint) is None:
        raise KnowledgeError(KNOWLEDGE_INVALID_REQUEST, "预览指纹无效")
    return parameters, expected_fingerprint


class KnowledgeDocumentReparseRequest(_StrictModel):
    """Explicit re-parse of the stored original file; never a model change."""

    expected_version: int
    processing_profile: ProcessingParameters | None = None
    expected_preview_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    chunk_size: int = 1000
    chunk_overlap: int = 100
    chunk_separator: str = KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR
    remove_extra_spaces: bool = False
    remove_urls_emails: bool = False
    chunking_mode: Literal["general", "parent_child"] = "general"
    child_chunk_size: int = 500
    child_chunk_separator: str = KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR

    @model_validator(mode="after")
    def check_processing_parameters(self):
        try:
            processing_parameters(self.model_dump(include=self.model_fields_set & _LEGACY_PROCESSING_FIELDS.keys()), self.processing_profile)
        except KnowledgeError:
            raise ValueError("conflicting processing parameters") from None
        return self


class KnowledgeBaseReparseRequest(_StrictModel):
    """Base-wide re-parse with one parameter set; the only way to switch the base's chunking mode."""

    processing_profile: ProcessingParameters | None = None
    chunk_size: int = 1000
    chunk_overlap: int = 100
    chunk_separator: str = KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR
    remove_extra_spaces: bool = False
    remove_urls_emails: bool = False
    chunking_mode: Literal["general", "parent_child"] = "general"
    child_chunk_size: int = 500
    child_chunk_separator: str = KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR

    @model_validator(mode="after")
    def check_processing_parameters(self):
        try:
            processing_parameters(self.model_dump(include=self.model_fields_set & _LEGACY_PROCESSING_FIELDS.keys()), self.processing_profile)
        except KnowledgeError:
            raise ValueError("conflicting processing parameters") from None
        return self


class KnowledgeReparsePreviewResponse(_StrictModel):
    document_version: int
    items: list[KnowledgeChunkPreviewItemResponse]
    total: int
    preview_fingerprint: str
    source_sha256: str
    effective_profile: ProcessingProfile
    warnings: list[ParseWarning]
    preview_attachments: list[KnowledgePreviewAttachmentResponse]
    omitted_preview_attachment_count: int
    table_sources: list[KnowledgePreviewTableSourceResponse]
    request_id: str


class KnowledgeSegmentItemResponse(_StrictModel):
    id: uuid.UUID
    document_version: int
    position: int
    content: str
    word_count: int
    enabled: bool
    hit_count: int
    source_position: dict[str, Any]
    created_at: datetime
    token_count: int
    source_spans: list[KnowledgePreviewSourceSpanResponse]


class KnowledgeSegmentListResponse(_StrictModel):
    items: list[KnowledgeSegmentItemResponse]
    total: int
    page: int
    page_size: int
    request_id: str


class KnowledgeSegmentCreateRequest(_StrictModel):
    content: str


class KnowledgeSegmentUpdateRequest(_StrictModel):
    content: str | None = None
    enabled: bool | None = None


class KnowledgeSegmentMutationResponse(_StrictModel):
    item: KnowledgeSegmentItemResponse
    request_id: str


class KnowledgeMetadataFilterBody(_StrictModel):
    """One manual metadata condition; the package owns value/type rules."""

    name: str
    operator: Literal["eq", "contains", "gte", "lte"]
    value: str | int | float
    # builtin targets the read-only document authority fields; custom (the
    # default) targets the base's defined doc_metadata fields.
    field_kind: Literal["custom", "builtin"] = "custom"


class KnowledgeSearchRequestBody(_StrictModel):
    """Search input for the retrieval test panel.

    ``knowledge_base_ids`` is bounded: omit it (or send null) to search every
    active base. An explicit empty list is rejected rather than silently
    searching nothing, and the upper bound keeps the recall SQL's ``IN`` bind
    list far below PostgreSQL's parameter ceiling.

    ``top_k`` and ``score_threshold`` may be omitted (or null) to use each
    base's configured defaults (0 disables the score filter); range and type
    rules live in the package. The Agent tool never exposes ``score_threshold``
    to the model — that restriction is the tool's, not the HTTP API's.
    """

    query: str
    # UUIDs arrive as JSON strings; strict mode would reject the coercion.
    knowledge_base_ids: (
        Annotated[
            list[Annotated[uuid.UUID, Field(strict=False)]],
            Field(min_length=1, max_length=100),
        ]
        | None
    ) = None
    top_k: int | None = None
    # Integers (e.g. 0) are valid thresholds; strict float would reject them.
    score_threshold: Annotated[float, Field(strict=False)] | None = None
    metadata_filters: (
        Annotated[
            list[KnowledgeMetadataFilterBody],
            Field(min_length=1, max_length=10),
        ]
        | None
    ) = None
    # Per-call route override for the test panel; omit to follow each base's
    # configured retrieval_mode.
    retrieval_mode: Literal["semantic", "hybrid"] | None = None
    # Per-call relative cutoff override (fraction of each base's best native
    # score); omit to follow each base's default_relative_cutoff.
    relative_score_cutoff: Annotated[float, Field(strict=False)] | None = None
    # Adds the bounded safe diagnostics to this one response; never persisted.
    debug: bool = False


class KnowledgeCitationResponse(_StrictModel):
    knowledge_base_id: uuid.UUID
    knowledge_base_name: str
    document_id: uuid.UUID
    document_name: str
    segment_id: uuid.UUID
    segment_position: int
    snippet: str
    score: float
    source_position: dict[str, Any]
    # New writes always provide these; the fields stay optional so historical
    # citations without them still render as short quotes.
    document_version: int | None = None
    content_digest: str | None = None
    score_kind: Literal["cosine", "rerank", "rank_fusion"] | None = None


class KnowledgeMatchedChildResponse(_StrictModel):
    child_id: uuid.UUID
    position: int
    route: Literal["semantic", "lexical"]
    score: float


class KnowledgeHitDiagnosticsResponse(_StrictModel):
    """Per-hit safe evidence: ids and scores only, no passage or child text."""

    segment_id: uuid.UUID
    local_score: float
    local_score_kind: Literal["cosine", "rerank"]
    score_domain: str
    ranking_method: Literal["cosine", "rerank", "rank_fusion"]
    ranking_score: float
    matched_children: list[KnowledgeMatchedChildResponse]
    matched_via: Literal["segment", "child", "summary"]


class KnowledgeRouteCountsResponse(_StrictModel):
    semantic_candidates: int
    lexical_candidates: int
    summary_candidates: int
    query_embedding_cache_hits: int
    query_embedding_cache_misses: int
    parents_deduplicated: int
    threshold_filtered: int
    relative_filtered: int
    lexical_threshold_exempt: int
    stale_filtered: int
    returned: int


class KnowledgeSearchTimingsResponse(_StrictModel):
    query_embedding_ms: float
    recall_ms: float
    rerank_ms: float
    final_validation_ms: float


class KnowledgeSearchDiagnosticsResponse(_StrictModel):
    strategy_version: str
    lexical_version: int
    target_base_count: int
    effective_top_k: int
    per_base_route_budget: int
    retrieval_mode: Literal["semantic", "hybrid"]
    counts: KnowledgeRouteCountsResponse
    timings: KnowledgeSearchTimingsResponse
    model_ids: list[uuid.UUID]
    ranking_method: Literal["cosine", "rerank", "rank_fusion"] | None
    empty_reason: Literal["not_ready", "no_candidates", "filtered_out", "stale_candidates"] | None
    heterogeneous_without_lexical_evidence: bool
    lexical_query_token_count: int
    lexical_query_truncated: bool
    hit_diagnostics: list[KnowledgeHitDiagnosticsResponse]


class KnowledgeSearchResponse(_StrictModel):
    citations: list[KnowledgeCitationResponse]
    # Present only when the request asked for debug diagnostics.
    diagnostics: KnowledgeSearchDiagnosticsResponse | None = None
    request_id: str


class KnowledgeSegmentChildResponse(_StrictModel):
    id: uuid.UUID
    position: int
    content: str
    word_count: int


class KnowledgeSegmentAttachmentResponse(_StrictModel):
    attachment_id: uuid.UUID
    ref: str
    alt_text: str
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    width: int
    height: int


class KnowledgeSegmentSummaryResponse(_StrictModel):
    content: str
    created_at: datetime


class KnowledgeSegmentDetailResponse(_StrictModel):
    segment: KnowledgeSegmentItemResponse
    knowledge_base_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    content_state: Literal["current", "stale"]
    stored_content_version: int
    current_document_version: int
    children_total: int
    child_page: int
    children: list[KnowledgeSegmentChildResponse]
    attachments: list[KnowledgeSegmentAttachmentResponse]
    summary: KnowledgeSegmentSummaryResponse | None
    request_id: str


class KnowledgeQueryItemResponse(_StrictModel):
    id: uuid.UUID
    knowledge_base_ids: list[uuid.UUID]
    query: str
    source: Literal["agent", "retrieval_test"]
    result_count: int
    top_score: float | None
    created_at: datetime


class KnowledgeQueryListResponse(_StrictModel):
    items: list[KnowledgeQueryItemResponse]
    total: int
    page: int
    page_size: int
    request_id: str


class KnowledgeHealthResponse(_StrictModel):
    enabled: bool
    database_ok: bool
    storage_ok: bool
    message: str
    request_id: str


def _option_response(option: RetrievalModelOption) -> KnowledgeModelOptionResponse:
    return KnowledgeModelOptionResponse(
        id=option.id,
        provider_name=option.provider_name,
        model_name=option.model_name,
        embedding_dimension=option.embedding_dimension,
    )


def _base_response(view: KnowledgeBaseView) -> KnowledgeBaseItemResponse:
    return KnowledgeBaseItemResponse(
        id=view.id,
        project_id=view.project_id,
        name=view.name,
        description=view.description,
        embedding_model_id=view.embedding_model_id,
        reranker_model_id=view.reranker_model_id,
        retrieval_mode=view.retrieval_mode,
        summary_index_enabled=view.summary_index_enabled,
        status=view.status,
        document_count=view.document_count,
        default_top_k=view.default_top_k,
        default_score_threshold=view.default_score_threshold,
        default_relative_cutoff=view.default_relative_cutoff,
        chunking_mode=view.chunking_mode,
        delete_error=view.delete_error,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _task_progress_response(progress: KnowledgeTaskProgress | None) -> KnowledgeTaskProgressResponse | None:
    if progress is None:
        return None
    return KnowledgeTaskProgressResponse(
        kind=progress.kind,
        status=progress.status,
        stage=progress.stage,
        completed_units=progress.completed_units,
        total_units=progress.total_units,
        attempt_count=progress.attempt_count,
        max_attempts=progress.max_attempts,
        target_version=progress.target_version,
        next_attempt_at=progress.next_attempt_at,
    )


def _document_response(view: KnowledgeDocumentView) -> KnowledgeDocumentItemResponse:
    return KnowledgeDocumentItemResponse(
        parsing_profile=view.parsing_profile,
        parse_warnings=list(view.parse_warnings),
        chunk_size_unit=view.parsing_profile.chunk.unit if view.parsing_profile else "character",
        tokenizer_profile_id=view.parsing_profile.chunk.tokenizer_profile_id if view.parsing_profile else None,
        id=view.id,
        project_id=view.project_id,
        knowledge_base_id=view.knowledge_base_id,
        name=view.name,
        original_name=view.original_name,
        media_type=view.media_type,
        size_bytes=view.size_bytes,
        status=view.status,
        enabled=view.enabled,
        version=view.version,
        chunk_size=view.chunk_size,
        chunk_overlap=view.chunk_overlap,
        chunk_separator=view.chunk_separator,
        remove_extra_spaces=view.remove_extra_spaces,
        remove_urls_emails=view.remove_urls_emails,
        chunking_mode=view.chunking_mode,
        child_chunk_size=view.child_chunk_size,
        child_chunk_separator=view.child_chunk_separator,
        segment_count=view.segment_count,
        word_count=view.word_count,
        hit_count=view.hit_count,
        doc_metadata=view.doc_metadata,
        error_message=view.error_message,
        delete_error=view.delete_error,
        task_progress=_task_progress_response(view.task_progress),
        content_initialized=view.content_initialized,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _metadata_field_response(view: KnowledgeMetadataFieldView) -> KnowledgeMetadataFieldItemResponse:
    return KnowledgeMetadataFieldItemResponse(
        id=view.id,
        knowledge_base_id=view.knowledge_base_id,
        name=view.name,
        field_type=view.field_type,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


@project_router.get("/model-options", response_model=KnowledgeModelOptionsResponse)
async def list_knowledge_model_options(
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    session: Annotated[AsyncSession, Depends(project_session)],
) -> KnowledgeModelOptionsResponse:
    """Active registry models members may bind; admin metadata stays hidden."""

    del module  # Resolved only to 404 when the Knowledge feature is disabled.
    authority = _knowledge_read_authority(context)
    try:
        async with session.begin():
            # Same-transaction membership re-check: a member revoked between
            # context issuance and use must not keep reading this surface.
            await authority.revalidate(session)
            embedding, rerank = await list_active_retrieval_model_options(session)
            try:
                summary_model = await read_active_summary_model(session)
            except KnowledgeError as error:
                if error.code != KNOWLEDGE_MODEL_UNAVAILABLE:
                    raise
                # An invalid optional summary model must not hide working
                # embedding/reranker choices from project members.
                summary_model = None
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    except SQLAlchemyError:
        raise knowledge_http_exception(
            KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用"),
            context.request_id,
        ) from None
    return KnowledgeModelOptionsResponse(
        embedding_models=[_option_response(option) for option in embedding],
        reranker_models=[_option_response(option) for option in rerank],
        summary_model=KnowledgeSummaryModelResponse(model_name=summary_model.model_name, display_name=summary_model.display_name) if summary_model is not None else None,
        request_id=context.request_id,
    )


@project_router.get("/file-capabilities", response_model=FileCapabilities)
async def knowledge_file_capabilities(
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> FileCapabilities:
    try:
        return await module.file_capabilities(authority=_knowledge_read_authority(context))
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None


@project_router.get("/health", response_model=KnowledgeHealthResponse)
async def knowledge_health(
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeHealthResponse:
    try:
        health = await module.health(authority=_knowledge_read_authority(context))
    except KnowledgeError as error:  # pragma: no cover - health() reports, not raises
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeHealthResponse(
        enabled=health.enabled,
        database_ok=health.database_ok,
        storage_ok=health.storage_ok,
        message=health.message,
        request_id=context.request_id,
    )


@project_router.post("/bases", response_model=KnowledgeBaseMutationResponse)
async def create_knowledge_base(
    body: KnowledgeBaseCreateRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeBaseMutationResponse:
    try:
        view = await module.create_knowledge_base(
            context.project_id,
            KnowledgeBaseCreate(
                name=body.name,
                embedding_model_id=body.embedding_model_id,
                reranker_model_id=body.reranker_model_id,
                description=body.description,
                retrieval_mode=body.retrieval_mode,
            ),
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeBaseMutationResponse(item=_base_response(view), request_id=context.request_id)


@project_router.get("/bases", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases(
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> KnowledgeBaseListResponse:
    try:
        views, total = await module.list_knowledge_bases(
            context.project_id,
            page=page,
            page_size=page_size,
            authority=_knowledge_read_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeBaseListResponse(
        items=[_base_response(view) for view in views],
        total=total,
        page=page,
        page_size=page_size,
        request_id=context.request_id,
    )


@project_router.get("/bases/{base_id}", response_model=KnowledgeBaseMutationResponse)
async def get_knowledge_base(
    base_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeBaseMutationResponse:
    try:
        view = await module.get_knowledge_base(
            context.project_id,
            base_id,
            authority=_knowledge_read_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeBaseMutationResponse(item=_base_response(view), request_id=context.request_id)


@project_router.patch("/bases/{base_id}", response_model=KnowledgeBaseUpdateResponse)
async def update_knowledge_base(
    base_id: uuid.UUID,
    body: KnowledgeBaseUpdateRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeBaseUpdateResponse:
    try:
        result = await module.update_knowledge_base(
            context.project_id,
            base_id,
            KnowledgeBaseUpdate(
                name=body.name,
                description=body.description,
                status=body.status,
                default_top_k=body.default_top_k,
                default_score_threshold=body.default_score_threshold,
                default_relative_cutoff=body.default_relative_cutoff,
                clear_relative_cutoff=body.clear_relative_cutoff,
                embedding_model_id=body.embedding_model_id,
                reranker_model_id=body.reranker_model_id,
                clear_reranker_model=body.clear_reranker_model,
                retrieval_mode=body.retrieval_mode,
                summary_index_enabled=body.summary_index_enabled,
            ),
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeBaseUpdateResponse(
        item=_base_response(result.base),
        summary_backfill=KnowledgeSummaryBackfillResponse(
            accepted_document_count=result.summary_backfill.accepted_document_count,
            skipped_document_ids=list(result.summary_backfill.skipped_document_ids),
        )
        if result.summary_backfill is not None
        else None,
        request_id=context.request_id,
    )


@project_router.post("/bases/{base_id}/rebuild", response_model=KnowledgeBaseRebuildResponse)
async def rebuild_knowledge_base(
    base_id: uuid.UUID,
    body: KnowledgeBaseRebuildRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeBaseRebuildResponse:
    """Rebind the embedding model and re-embed the current content."""

    try:
        result = await module.rebuild_knowledge_base(
            context.project_id,
            base_id,
            embedding_model_id=body.embedding_model_id,
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeBaseRebuildResponse(
        item=_base_response(result.base),
        accepted_document_count=result.accepted_document_count,
        skipped_document_ids=list(result.skipped_document_ids),
        request_id=context.request_id,
    )


@project_router.post("/bases/{base_id}/reparse", response_model=KnowledgeBaseRebuildResponse)
async def reparse_knowledge_base(
    base_id: uuid.UUID,
    body: KnowledgeBaseReparseRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeBaseRebuildResponse:
    """Re-parse every document with one parameter set; switches the base's chunking mode."""

    try:
        result = await module.reparse_knowledge_base(
            context.project_id,
            base_id,
            KnowledgeBaseReparseContract(
                processing_profile=processing_parameters(body.model_dump(include=body.model_fields_set & _LEGACY_PROCESSING_FIELDS.keys()), body.processing_profile) if body.processing_profile is not None else None,
                chunk_size=body.chunk_size,
                chunk_overlap=body.chunk_overlap,
                chunk_separator=body.chunk_separator,
                remove_extra_spaces=body.remove_extra_spaces,
                remove_urls_emails=body.remove_urls_emails,
                chunking_mode=body.chunking_mode,
                child_chunk_size=body.child_chunk_size,
                child_chunk_separator=body.child_chunk_separator,
            ),
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeBaseRebuildResponse(
        item=_base_response(result.base),
        accepted_document_count=result.accepted_document_count,
        skipped_document_ids=list(result.skipped_document_ids),
        request_id=context.request_id,
    )


@project_router.post("/bases/{base_id}/relex", response_model=KnowledgeBaseRelexResponse)
async def relex_knowledge_base(
    base_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeBaseRelexResponse:
    """Rebuild stale lexical derivations from stored text; no re-parse, no re-embed."""

    try:
        result = await module.relex_knowledge_base(
            context.project_id,
            base_id,
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeBaseRelexResponse(
        accepted_document_count=result.accepted_document_count,
        up_to_date_document_count=result.up_to_date_document_count,
        skipped_document_ids=list(result.skipped_document_ids),
        request_id=context.request_id,
    )


@project_router.get("/filter-fields", response_model=KnowledgeFilterFieldsResponse)
async def list_knowledge_filter_fields(
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    base_ids: Annotated[list[uuid.UUID] | None, Query()] = None,
) -> KnowledgeFilterFieldsResponse:
    """Discover the filterable builtin/custom fields per active base.

    Definitions only — never values scanned from documents. A scope wider
    than the discovery budget is refused with a hint to narrow ``base_ids``.
    """

    try:
        bases = await module.list_filter_fields(
            context.project_id,
            base_ids,
            authority=_knowledge_read_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeFilterFieldsResponse(
        bases=[
            KnowledgeBaseFilterFieldsResponse(
                knowledge_base_id=entry.knowledge_base_id,
                fields=[
                    KnowledgeFilterFieldItemResponse(
                        kind=field.kind,
                        name=field.name,
                        field_type=field.field_type,
                        operators=list(field.operators),
                        writable=field.writable,
                    )
                    for field in entry.fields
                ],
            )
            for entry in bases
        ],
        request_id=context.request_id,
    )


@project_router.get("/bases/{base_id}/metadata-fields", response_model=KnowledgeMetadataFieldListResponse)
async def list_knowledge_metadata_fields(
    base_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeMetadataFieldListResponse:
    try:
        views = await module.list_metadata_fields(
            context.project_id,
            base_id,
            authority=_knowledge_read_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeMetadataFieldListResponse(
        items=[_metadata_field_response(view) for view in views],
        request_id=context.request_id,
    )


@project_router.post("/bases/{base_id}/metadata-fields", response_model=KnowledgeMetadataFieldMutationResponse)
async def create_knowledge_metadata_field(
    base_id: uuid.UUID,
    body: KnowledgeMetadataFieldCreateRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeMetadataFieldMutationResponse:
    try:
        view = await module.create_metadata_field(
            context.project_id,
            base_id,
            name=body.name,
            field_type=body.field_type,
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeMetadataFieldMutationResponse(item=_metadata_field_response(view), request_id=context.request_id)


@project_router.patch("/metadata-fields/{field_id}", response_model=KnowledgeMetadataFieldMutationResponse)
async def rename_knowledge_metadata_field(
    field_id: uuid.UUID,
    body: KnowledgeMetadataFieldRenameRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeMetadataFieldMutationResponse:
    """Rename the field; document metadata keys follow in the same transaction."""

    try:
        view = await module.rename_metadata_field(
            context.project_id,
            field_id,
            name=body.name,
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeMetadataFieldMutationResponse(item=_metadata_field_response(view), request_id=context.request_id)


@project_router.delete("/metadata-fields/{field_id}", response_model=KnowledgeMetadataFieldDeleteResponse)
async def delete_knowledge_metadata_field(
    field_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeMetadataFieldDeleteResponse:
    """Drop the field and strip its key from the base's documents."""

    try:
        await module.delete_metadata_field(
            context.project_id,
            field_id,
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeMetadataFieldDeleteResponse(request_id=context.request_id)


@project_router.patch("/documents/{document_id}/metadata", response_model=KnowledgeDocumentMutationResponse)
async def set_knowledge_document_metadata(
    document_id: uuid.UUID,
    body: KnowledgeDocumentMetadataRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeDocumentMutationResponse:
    try:
        view = await module.set_document_metadata(
            context.project_id,
            document_id,
            dict(body.values),
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentMutationResponse(item=_document_response(view), request_id=context.request_id)


@project_router.patch("/bases/{base_id}/documents/metadata", response_model=KnowledgeDocumentBatchResponse)
async def set_knowledge_documents_metadata(
    base_id: uuid.UUID,
    body: KnowledgeDocumentsMetadataPatchRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeDocumentBatchResponse:
    """Apply one common metadata patch to documents of one base, all-or-nothing."""

    try:
        views = await module.set_documents_metadata(
            context.project_id,
            base_id,
            KnowledgeMetadataBatchPatch(
                document_ids=tuple(body.document_ids),
                values=dict(body.values),
            ),
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentBatchResponse(
        items=[_document_response(view) for view in views],
        request_id=context.request_id,
    )


@project_router.post("/bases/{base_id}/documents", response_model=KnowledgeDocumentMutationResponse)
async def upload_knowledge_document(
    request: Request,
    base_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    file: Annotated[UploadFile, File()],
    processing_profile: Annotated[str | None, Form()] = None,
    expected_preview_fingerprint: Annotated[str | None, Form()] = None,
    name: Annotated[str | None, Form()] = None,
    chunk_size: Annotated[int, Form()] = 1000,
    chunk_overlap: Annotated[int, Form()] = 100,
    chunk_separator: Annotated[str, Form()] = KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR,
    remove_extra_spaces: Annotated[bool, Form()] = False,
    remove_urls_emails: Annotated[bool, Form()] = False,
    chunking_mode: Annotated[str, Form()] = "general",
    child_chunk_size: Annotated[int, Form()] = 500,
    child_chunk_separator: Annotated[str, Form()] = KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR,
) -> KnowledgeDocumentMutationResponse:
    # Cheap pre-check: an honestly declared oversized body is rejected before
    # any of it is copied. The allowance covers multipart framing and fields;
    # the authoritative cap is still enforced while copying the file part.
    declared_length = request.headers.get("content-length")
    if declared_length is not None and declared_length.isdigit() and int(declared_length) > module.settings.upload_max_bytes + _MULTIPART_OVERHEAD_ALLOWANCE_BYTES:
        raise knowledge_http_exception(
            KnowledgeError(KNOWLEDGE_INVALID_REQUEST, f"文件大小超过上限 {module.settings.upload_max_bytes} 字节"),
            context.request_id,
        )
    original_name = file.filename or ""
    display_name = name.strip() if name and name.strip() else original_name
    form = await request.form()
    try:
        parameters, expected_preview_fingerprint = multipart_processing_options(
            raw_profile=processing_profile,
            expected_fingerprint=expected_preview_fingerprint,
            form_keys=set(form),
            legacy_values={
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
                "chunk_separator": chunk_separator,
                "remove_extra_spaces": remove_extra_spaces,
                "remove_urls_emails": remove_urls_emails,
                "chunking_mode": chunking_mode,
                "child_chunk_size": child_chunk_size,
                "child_chunk_separator": child_chunk_separator,
            },
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    staging_path, size_bytes = await _stage_upload_to_temp(
        file,
        module.settings.upload_max_bytes,
        context.request_id,
    )
    try:
        view = await module.upload_document(
            context.project_id,
            base_id,
            KnowledgeDocumentUpload(
                name=display_name,
                original_name=original_name,
                source_path=staging_path,
                size_bytes=size_bytes,
                processing_profile=parameters,
                expected_preview_fingerprint=expected_preview_fingerprint,
                media_type=file.content_type,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                chunk_separator=chunk_separator,
                remove_extra_spaces=remove_extra_spaces,
                remove_urls_emails=remove_urls_emails,
                chunking_mode=chunking_mode,  # type: ignore[arg-type]  # validated by the package
                child_chunk_size=child_chunk_size,
                child_chunk_separator=child_chunk_separator,
            ),
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    finally:
        await _remove_request_temp_path(staging_path)
    return KnowledgeDocumentMutationResponse(item=_document_response(view), request_id=context.request_id)


@project_router.post("/chunk-preview", response_model=KnowledgeChunkPreviewResponse)
async def preview_knowledge_chunks(
    request: Request,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    file: Annotated[UploadFile, File()],
    processing_profile: Annotated[str | None, Form()] = None,
    expected_preview_fingerprint: Annotated[str | None, Form()] = None,
    chunk_size: Annotated[int, Form()] = 1000,
    chunk_overlap: Annotated[int, Form()] = 100,
    chunk_separator: Annotated[str, Form()] = KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR,
    remove_extra_spaces: Annotated[bool, Form()] = False,
    remove_urls_emails: Annotated[bool, Form()] = False,
    chunking_mode: Annotated[str, Form()] = "general",
    child_chunk_size: Annotated[int, Form()] = 500,
    child_chunk_separator: Annotated[str, Form()] = KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR,
) -> KnowledgeChunkPreviewResponse:
    """Synchronous extract → clean → split; writes no rows, objects, or tasks."""

    declared_length = request.headers.get("content-length")
    if declared_length is not None and declared_length.isdigit() and int(declared_length) > module.settings.upload_max_bytes + _MULTIPART_OVERHEAD_ALLOWANCE_BYTES:
        raise knowledge_http_exception(
            KnowledgeError(KNOWLEDGE_INVALID_REQUEST, f"文件大小超过上限 {module.settings.upload_max_bytes} 字节"),
            context.request_id,
        )
    form = await request.form()
    try:
        parameters, expected_preview_fingerprint = multipart_processing_options(
            raw_profile=processing_profile,
            expected_fingerprint=expected_preview_fingerprint,
            form_keys=set(form),
            legacy_values={
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
                "chunk_separator": chunk_separator,
                "remove_extra_spaces": remove_extra_spaces,
                "remove_urls_emails": remove_urls_emails,
                "chunking_mode": chunking_mode,
                "child_chunk_size": child_chunk_size,
                "child_chunk_separator": child_chunk_separator,
            },
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    staging_path, size_bytes = await _stage_upload_to_temp(
        file,
        module.settings.upload_max_bytes,
        context.request_id,
    )
    try:
        preview = await module.preview_document_chunks(
            KnowledgeChunkPreviewRequest(
                original_name=file.filename or "",
                source_path=staging_path,
                size_bytes=size_bytes,
                processing_profile=parameters,
                expected_preview_fingerprint=expected_preview_fingerprint,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                chunk_separator=chunk_separator,
                remove_extra_spaces=remove_extra_spaces,
                remove_urls_emails=remove_urls_emails,
                chunking_mode=chunking_mode,  # type: ignore[arg-type]  # validated by the package
                child_chunk_size=child_chunk_size,
                child_chunk_separator=child_chunk_separator,
            ),
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    finally:
        await _remove_request_temp_path(staging_path)
    return KnowledgeChunkPreviewResponse(
        **_preview_response_payload(preview),
        request_id=context.request_id,
    )


@project_router.get("/bases/{base_id}/documents", response_model=KnowledgeDocumentListResponse)
async def list_knowledge_documents(
    base_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> KnowledgeDocumentListResponse:
    try:
        views, total = await module.list_documents(
            context.project_id,
            base_id,
            page=page,
            page_size=page_size,
            authority=_knowledge_read_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentListResponse(
        items=[_document_response(view) for view in views],
        total=total,
        page=page,
        page_size=page_size,
        request_id=context.request_id,
    )


@project_router.get("/documents/{document_id}", response_model=KnowledgeDocumentMutationResponse)
async def get_knowledge_document(
    document_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeDocumentMutationResponse:
    try:
        view = await module.get_document(
            context.project_id,
            document_id,
            authority=_knowledge_read_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentMutationResponse(item=_document_response(view), request_id=context.request_id)


@project_router.get(
    "/documents/{document_id}/attachments",
    response_model=KnowledgeDocumentAttachmentListResponse,
)
async def list_knowledge_document_attachments(
    document_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeDocumentAttachmentListResponse:
    try:
        attachments, document_version = await module.list_document_attachments(
            context.project_id,
            document_id,
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentAttachmentListResponse(
        items=[
            KnowledgeDocumentAttachmentItemResponse(
                attachment_id=attachment.attachment_id,
                ref=attachment.ref,
                media_type=attachment.media_type,
                width=attachment.width,
                height=attachment.height,
            )
            for attachment in attachments
        ],
        document_version=document_version,
        request_id=context.request_id,
    )


@project_router.get("/documents/{document_id}/download")
async def download_knowledge_document(
    document_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> FileResponse:
    target_path = await _new_request_temp_path()
    try:
        view = await module.download_document(
            context.project_id,
            document_id,
            target_path,
            authority=_knowledge_read_authority(context),
        )
    except KnowledgeError as error:
        await _remove_request_temp_path(target_path)
        raise knowledge_http_exception(error, context.request_id) from None
    except BaseException:
        await _remove_request_temp_path(target_path)
        raise
    return _TempFileResponse(
        path=target_path,
        filename=view.original_name,
        media_type=view.media_type or "application/octet-stream",
    )


@project_router.get("/documents/{document_id}/segments/{segment_id}/attachments/{attachment_id}")
async def download_knowledge_segment_attachment(
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    attachment_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    expected_document_version: Annotated[int, Query(ge=1)],
    expected_content_digest: Annotated[str, Query(pattern="^[0-9a-f]{64}$")],
) -> FileResponse:
    target_path = await _new_request_temp_path()
    try:
        metadata = await module.download_segment_attachment(
            context.project_id,
            document_id,
            segment_id,
            attachment_id,
            target_path,
            expected_document_version=expected_document_version,
            expected_content_digest=expected_content_digest,
            authority=_knowledge_read_authority(context),
        )
    except KnowledgeError as error:
        await _remove_request_temp_path(target_path)
        raise knowledge_http_exception(error, context.request_id) from None
    except BaseException:
        await _remove_request_temp_path(target_path)
        raise
    return _TempFileResponse(
        path=target_path,
        media_type=metadata.media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@project_router.delete("/bases/{base_id}", response_model=KnowledgeBaseMutationResponse)
async def delete_knowledge_base(
    base_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeBaseMutationResponse:
    try:
        view = await module.delete_knowledge_base(
            context.project_id,
            base_id,
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeBaseMutationResponse(item=_base_response(view), request_id=context.request_id)


@project_router.delete("/documents/{document_id}", response_model=KnowledgeDocumentMutationResponse)
async def delete_knowledge_document(
    document_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeDocumentMutationResponse:
    try:
        view = await module.delete_document(
            context.project_id,
            document_id,
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentMutationResponse(item=_document_response(view), request_id=context.request_id)


@project_router.post("/documents/{document_id}/retry", response_model=KnowledgeDocumentMutationResponse)
async def retry_knowledge_document(
    document_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeDocumentMutationResponse:
    try:
        view = await module.retry_document(
            context.project_id,
            document_id,
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentMutationResponse(item=_document_response(view), request_id=context.request_id)


def _reparse_request(body: KnowledgeDocumentReparseRequest) -> KnowledgeReparseRequest:
    return KnowledgeReparseRequest(
        expected_version=body.expected_version,
        processing_profile=processing_parameters(body.model_dump(include=body.model_fields_set & _LEGACY_PROCESSING_FIELDS.keys()), body.processing_profile) if body.processing_profile is not None else None,
        expected_preview_fingerprint=body.expected_preview_fingerprint,
        chunk_size=body.chunk_size,
        chunk_overlap=body.chunk_overlap,
        chunk_separator=body.chunk_separator,
        remove_extra_spaces=body.remove_extra_spaces,
        remove_urls_emails=body.remove_urls_emails,
        chunking_mode=body.chunking_mode,
        child_chunk_size=body.child_chunk_size,
        child_chunk_separator=body.child_chunk_separator,
    )


@project_router.post("/documents/{document_id}/reparse-preview", response_model=KnowledgeReparsePreviewResponse)
async def preview_knowledge_document_reparse(
    document_id: uuid.UUID,
    body: KnowledgeDocumentReparseRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeReparsePreviewResponse:
    """Server-side preview from the stored original; writes nothing."""

    try:
        previewed = await module.preview_document_reparse(
            context.project_id,
            document_id,
            _reparse_request(body),
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeReparsePreviewResponse(
        document_version=previewed.document_version,
        **_preview_response_payload(previewed.preview),
        request_id=context.request_id,
    )


@project_router.post("/documents/{document_id}/reparse", response_model=KnowledgeDocumentMutationResponse)
async def reparse_knowledge_document(
    document_id: uuid.UUID,
    body: KnowledgeDocumentReparseRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeDocumentMutationResponse:
    try:
        view = await module.reparse_document(
            context.project_id,
            document_id,
            _reparse_request(body),
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentMutationResponse(item=_document_response(view), request_id=context.request_id)


@project_router.patch("/documents/{document_id}", response_model=KnowledgeDocumentMutationResponse)
async def rename_knowledge_document(
    document_id: uuid.UUID,
    body: KnowledgeDocumentRenameRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeDocumentMutationResponse:
    try:
        view = await module.rename_document(
            context.project_id,
            document_id,
            body.name,
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentMutationResponse(item=_document_response(view), request_id=context.request_id)


@project_router.post("/documents/batch-status", response_model=KnowledgeDocumentBatchResponse)
async def set_knowledge_documents_enabled(
    body: KnowledgeDocumentBatchStatusRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeDocumentBatchResponse:
    try:
        views = await module.set_documents_enabled(
            context.project_id,
            list(body.document_ids),
            body.enabled,
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentBatchResponse(
        items=[_document_response(view) for view in views],
        request_id=context.request_id,
    )


@project_router.post("/documents/batch-delete", response_model=KnowledgeDocumentBatchResponse)
async def delete_knowledge_documents(
    body: KnowledgeDocumentBatchDeleteRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeDocumentBatchResponse:
    try:
        views = await module.delete_documents(
            context.project_id,
            list(body.document_ids),
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentBatchResponse(
        items=[_document_response(view) for view in views],
        request_id=context.request_id,
    )


@project_router.post("/documents/{document_id}/segments", response_model=KnowledgeSegmentMutationResponse)
async def create_knowledge_segment(
    document_id: uuid.UUID,
    body: KnowledgeSegmentCreateRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeSegmentMutationResponse:
    try:
        view = await module.create_segment(
            context.project_id,
            document_id,
            KnowledgeSegmentCreate(content=body.content),
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeSegmentMutationResponse(item=_segment_response(view), request_id=context.request_id)


@project_router.patch("/segments/{segment_id}", response_model=KnowledgeSegmentMutationResponse)
async def update_knowledge_segment(
    segment_id: uuid.UUID,
    body: KnowledgeSegmentUpdateRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeSegmentMutationResponse:
    try:
        view = await module.update_segment(
            context.project_id,
            segment_id,
            KnowledgeSegmentUpdate(content=body.content, enabled=body.enabled),
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeSegmentMutationResponse(item=_segment_response(view), request_id=context.request_id)


@project_router.delete("/segments/{segment_id}", response_model=KnowledgeDocumentMutationResponse)
async def delete_knowledge_segment(
    segment_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeDocumentMutationResponse:
    """Remove one segment; the response carries the updated parent document."""

    try:
        view = await module.delete_segment(
            context.project_id,
            segment_id,
            authority=_knowledge_edit_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentMutationResponse(item=_document_response(view), request_id=context.request_id)


@project_router.get("/documents/{document_id}/segments", response_model=KnowledgeSegmentListResponse)
async def list_knowledge_document_segments(
    document_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> KnowledgeSegmentListResponse:
    try:
        views, total = await module.list_document_segments(
            context.project_id,
            document_id,
            page=page,
            page_size=page_size,
            authority=_knowledge_read_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeSegmentListResponse(
        items=[_segment_response(view) for view in views],
        total=total,
        page=page,
        page_size=page_size,
        request_id=context.request_id,
    )


def _segment_response(view: KnowledgeSegmentView) -> KnowledgeSegmentItemResponse:
    return KnowledgeSegmentItemResponse(
        id=view.id,
        document_version=view.document_version,
        position=view.position,
        content=view.content,
        word_count=view.word_count,
        enabled=view.enabled,
        hit_count=view.hit_count,
        source_position=view.source_position,
        created_at=view.created_at,
        token_count=view.token_count,
        source_spans=[KnowledgePreviewSourceSpanResponse(**span.model_dump(mode="json")) for span in view.source_spans],
    )


@project_router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(
    body: KnowledgeSearchRequestBody,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeSearchResponse:
    """Two-stage retrieval test endpoint; all business rules live in the module."""

    try:
        result = await module.search(
            KnowledgeSearchRequest(
                project_id=context.project_id,
                owner_user_id=context.user_id,
                query=body.query,
                knowledge_base_ids=(tuple(body.knowledge_base_ids) if body.knowledge_base_ids is not None else None),
                top_k=body.top_k,
                score_threshold=body.score_threshold,
                retrieval_mode=body.retrieval_mode,
                relative_score_cutoff=body.relative_score_cutoff,
                source="retrieval_test",
                metadata_filters=(tuple(KnowledgeMetadataFilter(name=item.name, operator=item.operator, value=item.value, field_kind=item.field_kind) for item in body.metadata_filters) if body.metadata_filters is not None else None),
                debug=body.debug,
            ),
            authority=_knowledge_read_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeSearchResponse(
        citations=[_citation_response(citation) for citation in result.citations],
        diagnostics=(_search_diagnostics_response(result.diagnostics) if result.diagnostics is not None else None),
        request_id=context.request_id,
    )


@project_router.get(
    "/bases/{base_id}/documents/{document_id}/segments/{segment_id}",
    response_model=KnowledgeSegmentDetailResponse,
)
async def get_knowledge_segment_detail(
    base_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    expected_document_version: Annotated[int | None, Query(ge=1)] = None,
    expected_content_digest: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    child_page: Annotated[int, Query(ge=1)] = 1,
) -> KnowledgeSegmentDetailResponse:
    """Authoritative single-segment read; hit expectations turn drift into 409."""

    try:
        detail = await module.get_segment_detail(
            context.project_id,
            base_id,
            document_id,
            segment_id,
            expected_document_version=expected_document_version,
            expected_content_digest=expected_content_digest,
            child_page=child_page,
            authority=_knowledge_read_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return _segment_detail_response(detail, context.request_id)


@project_router.get("/bases/{base_id}/documents/{document_id}/segments/{segment_id}/attachments/{attachment_id}")
async def download_knowledge_citation_attachment(
    base_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    attachment_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    expected_document_version: Annotated[int, Query(ge=1)],
    expected_content_digest: Annotated[str, Query(pattern="^[0-9a-f]{64}$")],
) -> FileResponse:
    target_path = await _new_request_temp_path()
    try:
        metadata = await module.download_citation_attachment(
            context.project_id,
            base_id,
            document_id,
            segment_id,
            attachment_id,
            target_path,
            expected_document_version=expected_document_version,
            expected_content_digest=expected_content_digest,
            authority=_knowledge_read_authority(context),
        )
    except KnowledgeError as error:
        await _remove_request_temp_path(target_path)
        raise knowledge_http_exception(error, context.request_id) from None
    except BaseException:
        await _remove_request_temp_path(target_path)
        raise
    return _TempFileResponse(
        path=target_path,
        media_type=metadata.media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@project_router.get("/bases/{base_id}/queries", response_model=KnowledgeQueryListResponse)
async def list_knowledge_base_queries(
    base_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> KnowledgeQueryListResponse:
    """Recent retrieval queries that targeted this base, newest first."""

    try:
        views, total = await module.list_recent_queries(
            context.project_id,
            context.user_id,
            base_id,
            page=page,
            page_size=page_size,
            authority=_knowledge_read_authority(context),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeQueryListResponse(
        items=[_query_response(view) for view in views],
        total=total,
        page=page,
        page_size=page_size,
        request_id=context.request_id,
    )


def _query_response(view: KnowledgeQueryView) -> KnowledgeQueryItemResponse:
    return KnowledgeQueryItemResponse(
        id=view.id,
        knowledge_base_ids=list(view.knowledge_base_ids),
        query=view.query,
        source=view.source,
        result_count=view.result_count,
        top_score=view.top_score,
        created_at=view.created_at,
    )


def _citation_response(citation: KnowledgeCitation) -> KnowledgeCitationResponse:
    return KnowledgeCitationResponse(
        knowledge_base_id=citation.knowledge_base_id,
        knowledge_base_name=citation.knowledge_base_name,
        document_id=citation.document_id,
        document_name=citation.document_name,
        segment_id=citation.segment_id,
        segment_position=citation.segment_position,
        snippet=citation.snippet,
        score=citation.score,
        source_position=citation.source_position,
        document_version=citation.document_version,
        content_digest=citation.content_digest,
        score_kind=citation.score_kind,
    )


def _search_diagnostics_response(diagnostics: KnowledgeSearchDiagnostics) -> KnowledgeSearchDiagnosticsResponse:
    return KnowledgeSearchDiagnosticsResponse(
        strategy_version=diagnostics.strategy_version,
        lexical_version=diagnostics.lexical_version,
        target_base_count=diagnostics.target_base_count,
        effective_top_k=diagnostics.effective_top_k,
        per_base_route_budget=diagnostics.per_base_route_budget,
        retrieval_mode=diagnostics.retrieval_mode,
        counts=KnowledgeRouteCountsResponse(
            semantic_candidates=diagnostics.counts.semantic_candidates,
            lexical_candidates=diagnostics.counts.lexical_candidates,
            summary_candidates=diagnostics.counts.summary_candidates,
            query_embedding_cache_hits=diagnostics.counts.query_embedding_cache_hits,
            query_embedding_cache_misses=diagnostics.counts.query_embedding_cache_misses,
            parents_deduplicated=diagnostics.counts.parents_deduplicated,
            threshold_filtered=diagnostics.counts.threshold_filtered,
            relative_filtered=diagnostics.counts.relative_filtered,
            lexical_threshold_exempt=diagnostics.counts.lexical_threshold_exempt,
            stale_filtered=diagnostics.counts.stale_filtered,
            returned=diagnostics.counts.returned,
        ),
        timings=KnowledgeSearchTimingsResponse(
            query_embedding_ms=diagnostics.timings.query_embedding_ms,
            recall_ms=diagnostics.timings.recall_ms,
            rerank_ms=diagnostics.timings.rerank_ms,
            final_validation_ms=diagnostics.timings.final_validation_ms,
        ),
        model_ids=list(diagnostics.model_ids),
        ranking_method=diagnostics.ranking_method,
        empty_reason=diagnostics.empty_reason,
        heterogeneous_without_lexical_evidence=diagnostics.heterogeneous_without_lexical_evidence,
        lexical_query_token_count=diagnostics.lexical_query_token_count,
        lexical_query_truncated=diagnostics.lexical_query_truncated,
        hit_diagnostics=[
            KnowledgeHitDiagnosticsResponse(
                segment_id=entry.segment_id,
                local_score=entry.local_score,
                local_score_kind=entry.local_score_kind,
                score_domain=entry.score_domain,
                ranking_method=entry.ranking_method,
                ranking_score=entry.ranking_score,
                matched_via=entry.matched_via,
                matched_children=[
                    KnowledgeMatchedChildResponse(
                        child_id=child.child_id,
                        position=child.position,
                        route=child.route,
                        score=child.score,
                    )
                    for child in entry.matched_children
                ],
            )
            for entry in diagnostics.hit_diagnostics
        ],
    )


def _segment_detail_response(detail: KnowledgeSegmentDetail, request_id: str) -> KnowledgeSegmentDetailResponse:
    return KnowledgeSegmentDetailResponse(
        segment=_segment_response(detail.segment),
        knowledge_base_id=detail.knowledge_base_id,
        document_id=detail.document_id,
        document_name=detail.document_name,
        content_state=detail.content_state,
        stored_content_version=detail.stored_content_version,
        current_document_version=detail.current_document_version,
        children_total=detail.children_total,
        child_page=detail.child_page,
        summary=KnowledgeSegmentSummaryResponse(content=detail.summary.content, created_at=detail.summary.created_at) if detail.summary is not None else None,
        children=[
            KnowledgeSegmentChildResponse(
                id=child.id,
                position=child.position,
                content=child.content,
                word_count=child.word_count,
            )
            for child in detail.children
        ],
        attachments=[
            KnowledgeSegmentAttachmentResponse(
                attachment_id=attachment.attachment_id,
                ref=attachment.ref,
                alt_text=attachment.alt_text,
                media_type=attachment.media_type,
                width=attachment.width,
                height=attachment.height,
            )
            for attachment in detail.attachments
        ],
        request_id=request_id,
    )


__all__ = [
    "KnowledgeBaseCreateRequest",
    "KnowledgeBaseItemResponse",
    "KnowledgeBaseListResponse",
    "KnowledgeBaseMutationResponse",
    "KnowledgeBaseRebuildRequest",
    "KnowledgeBaseUpdateRequest",
    "KnowledgeDocumentBatchDeleteRequest",
    "KnowledgeDocumentBatchResponse",
    "KnowledgeDocumentBatchStatusRequest",
    "KnowledgeDocumentAttachmentItemResponse",
    "KnowledgeDocumentAttachmentListResponse",
    "KnowledgeDocumentItemResponse",
    "KnowledgeDocumentListResponse",
    "KnowledgeBaseFilterFieldsResponse",
    "KnowledgeDocumentMetadataRequest",
    "KnowledgeDocumentMutationResponse",
    "KnowledgeDocumentRenameRequest",
    "KnowledgeDocumentsMetadataPatchRequest",
    "KnowledgeFilterFieldItemResponse",
    "KnowledgeFilterFieldsResponse",
    "KnowledgeHealthResponse",
    "KnowledgeMetadataFieldCreateRequest",
    "KnowledgeMetadataFieldDeleteResponse",
    "KnowledgeMetadataFieldItemResponse",
    "KnowledgeMetadataFieldListResponse",
    "KnowledgeMetadataFieldMutationResponse",
    "KnowledgeMetadataFieldRenameRequest",
    "KnowledgeMetadataFilterBody",
    "KnowledgeModelOptionResponse",
    "KnowledgeModelOptionsResponse",
    "KnowledgeQueryItemResponse",
    "KnowledgeQueryListResponse",
    "KnowledgeSearchRequestBody",
    "KnowledgeSearchResponse",
    "KNOWLEDGE_FORBIDDEN",
    "KnowledgeSegmentCreateRequest",
    "KnowledgeSegmentItemResponse",
    "KnowledgeSegmentListResponse",
    "KnowledgeSegmentMutationResponse",
    "KnowledgeSegmentUpdateRequest",
    "get_knowledge_module",
    "knowledge_http_exception",
    "project_router",
    "require_project_knowledge_edit",
    "require_project_knowledge_read",
]
