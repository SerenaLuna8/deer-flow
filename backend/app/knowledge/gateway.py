"""Knowledge HTTP adapters: admin model management and project model options.

Admin routes reuse the platform system-admin gate; the project route reuses
server-resolved project context with ``shared_assets.read``. Handlers stay
thin: authorization first, then one :class:`KnowledgeModule` call, then a
``{code, message, request_id}`` error body on :class:`KnowledgeError`.
"""

from __future__ import annotations

import asyncio
import contextlib
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
    KnowledgeChunkPreviewRequest,
    KnowledgeCitation,
    KnowledgeDocumentUpload,
    KnowledgeDocumentView,
    KnowledgeError,
    KnowledgeMetadataFieldView,
    KnowledgeMetadataFilter,
    KnowledgeModelConfigurationCreate,
    KnowledgeModelConfigurationUpdate,
    KnowledgeModelConfigurationView,
    KnowledgeModelOption,
    KnowledgeModule,
    KnowledgeQueryView,
    KnowledgeSearchRequest,
    KnowledgeSegmentCreate,
    KnowledgeSegmentUpdate,
    KnowledgeSegmentView,
)
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditAuthorityRejected, SystemAuditContext
from app.final_schema import (
    FinalSchemaProbe,
    FinalSchemaRequired,
    FinalSchemaUnavailable,
)
from app.gateway.deps import get_current_user_from_request, project_session
from app.gateway.routers.admin_operations import (
    AdminOperationsRoute,
    authenticated_system_identity,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext, resolve_project_context
from app.projects.errors import (
    ProjectDatabaseUnavailable,
    ProjectForbidden,
    ProjectNotFound,
)
from app.reliability.error_mapping import reliability_http_exception
from app.reliability.errors import (
    ReliabilityDatabaseUnavailable,
    ReliabilityNotFound,
)
from app.reliability.operations import resolve_current_system_audit_context
from deerflow.trace_context import generate_trace_id, get_current_trace_id, normalize_trace_id

admin_router = APIRouter(
    prefix="/api/admin/knowledge/models",
    tags=["admin-knowledge-models"],
    route_class=AdminOperationsRoute,
)
project_router = APIRouter(
    prefix="/api/projects/{project_id}/knowledge",
    tags=["project-knowledge"],
)

# Capability gaps are a host authorization concern, so this code lives with
# the Gateway adapter rather than the host-agnostic package error catalog.
KNOWLEDGE_FORBIDDEN = "KNOWLEDGE_FORBIDDEN"

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


async def require_knowledge_admin_context(
    identity: Annotated[
        tuple[uuid.UUID, str],
        Depends(authenticated_system_identity),
    ],
    session: Annotated[AsyncSession, Depends(project_session)],
) -> SystemAuditContext:
    """Authorize a platform system admin exactly like other admin settings."""

    try:
        async with session.begin():
            context = await resolve_current_system_audit_context(
                session,
                identity[0],
                identity[1],
            )
            await FinalSchemaProbe().require_ready(session)
            return context
    except AuditAuthorityRejected:
        raise reliability_http_exception(ReliabilityNotFound(identity[1])) from None
    except (DBAPIError, FinalSchemaRequired, FinalSchemaUnavailable, RuntimeError):
        raise reliability_http_exception(ReliabilityDatabaseUnavailable(identity[1])) from None


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


class KnowledgeModelCreateRequest(_StrictModel):
    display_name: str
    base_url: str
    embedding_model: str
    embedding_dimension: int
    embedding_max_batch: int = 64
    reranker_model: str
    reranker_max_batch: int = 32
    request_timeout_seconds: int = 30
    api_key: SecretStr

    @field_validator("api_key")
    @classmethod
    def require_non_empty_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("api_key must not be empty")
        return value


class KnowledgeModelUpdateRequest(_StrictModel):
    display_name: str | None = None
    status: Literal["active", "disabled"] | None = None
    base_url: str | None = None
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    embedding_max_batch: int | None = None
    reranker_model: str | None = None
    reranker_max_batch: int | None = None
    request_timeout_seconds: int | None = None
    api_key: SecretStr | None = None

    @field_validator("api_key")
    @classmethod
    def require_non_empty_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value():
            raise ValueError("api_key must not be empty")
        return value


class KnowledgeModelItemResponse(_StrictModel):
    id: uuid.UUID
    display_name: str
    status: Literal["active", "disabled"]
    base_url: str
    embedding_model: str
    embedding_dimension: int
    embedding_max_batch: int
    reranker_model: str
    reranker_max_batch: int
    request_timeout_seconds: int
    in_use: bool
    created_at: datetime
    updated_at: datetime


class KnowledgeModelListResponse(_StrictModel):
    items: list[KnowledgeModelItemResponse]
    total: int
    page: int
    page_size: int
    request_id: str


class KnowledgeModelMutationResponse(_StrictModel):
    item: KnowledgeModelItemResponse
    request_id: str


class KnowledgeModelDeleteResponse(_StrictModel):
    request_id: str


class KnowledgeModelTestResponse(_StrictModel):
    ok: bool
    message: str
    request_id: str


class KnowledgeModelOptionResponse(_StrictModel):
    id: uuid.UUID
    display_name: str
    embedding_model: str
    embedding_dimension: int
    reranker_model: str


class KnowledgeModelOptionsResponse(_StrictModel):
    items: list[KnowledgeModelOptionResponse]
    request_id: str


class KnowledgeBaseCreateRequest(_StrictModel):
    name: str
    # UUIDs arrive as JSON strings; strict mode would reject the coercion.
    model_configuration_id: Annotated[uuid.UUID, Field(strict=False)]
    description: str = ""


class KnowledgeBaseUpdateRequest(_StrictModel):
    name: str | None = None
    description: str | None = None
    status: Literal["active", "disabled"] | None = None
    default_top_k: int | None = None
    # Integers (e.g. 0) are valid thresholds; strict float would reject them.
    default_score_threshold: Annotated[float, Field(strict=False)] | None = None


class KnowledgeBaseRebuildRequest(_StrictModel):
    """Rebind the base to a model configuration and re-embed every document."""

    # UUIDs arrive as JSON strings; strict mode would reject the coercion.
    model_configuration_id: Annotated[uuid.UUID, Field(strict=False)]


class KnowledgeBaseItemResponse(_StrictModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str
    model_configuration_id: uuid.UUID
    status: Literal["active", "disabled", "deleting"]
    document_count: int
    default_top_k: int
    default_score_threshold: float
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


class KnowledgeDocumentItemResponse(_StrictModel):
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


class KnowledgeChunkPreviewItemResponse(_StrictModel):
    position: int
    content: str
    word_count: int
    child_contents: list[str]


class KnowledgeChunkPreviewResponse(_StrictModel):
    items: list[KnowledgeChunkPreviewItemResponse]
    total: int
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


class KnowledgeSearchResponse(_StrictModel):
    citations: list[KnowledgeCitationResponse]
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


def _item_response(view: KnowledgeModelConfigurationView) -> KnowledgeModelItemResponse:
    return KnowledgeModelItemResponse(
        id=view.id,
        display_name=view.display_name,
        status=view.status,
        base_url=view.base_url,
        embedding_model=view.embedding_model,
        embedding_dimension=view.embedding_dimension,
        embedding_max_batch=view.embedding_max_batch,
        reranker_model=view.reranker_model,
        reranker_max_batch=view.reranker_max_batch,
        request_timeout_seconds=view.request_timeout_seconds,
        in_use=view.in_use,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _option_response(option: KnowledgeModelOption) -> KnowledgeModelOptionResponse:
    return KnowledgeModelOptionResponse(
        id=option.id,
        display_name=option.display_name,
        embedding_model=option.embedding_model,
        embedding_dimension=option.embedding_dimension,
        reranker_model=option.reranker_model,
    )


def _base_response(view: KnowledgeBaseView) -> KnowledgeBaseItemResponse:
    return KnowledgeBaseItemResponse(
        id=view.id,
        project_id=view.project_id,
        name=view.name,
        description=view.description,
        model_configuration_id=view.model_configuration_id,
        status=view.status,
        document_count=view.document_count,
        default_top_k=view.default_top_k,
        default_score_threshold=view.default_score_threshold,
        delete_error=view.delete_error,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _document_response(view: KnowledgeDocumentView) -> KnowledgeDocumentItemResponse:
    return KnowledgeDocumentItemResponse(
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


@admin_router.get("", response_model=KnowledgeModelListResponse)
async def list_knowledge_models(
    context: Annotated[SystemAuditContext, Depends(require_knowledge_admin_context)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> KnowledgeModelListResponse:
    try:
        views, total = await module.list_model_configurations(page=page, page_size=page_size)
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeModelListResponse(
        items=[_item_response(view) for view in views],
        total=total,
        page=page,
        page_size=page_size,
        request_id=context.request_id,
    )


@admin_router.post("", response_model=KnowledgeModelMutationResponse)
async def create_knowledge_model(
    body: KnowledgeModelCreateRequest,
    context: Annotated[SystemAuditContext, Depends(require_knowledge_admin_context)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeModelMutationResponse:
    try:
        view = await module.create_model_configuration(
            KnowledgeModelConfigurationCreate(
                display_name=body.display_name,
                base_url=body.base_url,
                embedding_model=body.embedding_model,
                embedding_dimension=body.embedding_dimension,
                embedding_max_batch=body.embedding_max_batch,
                reranker_model=body.reranker_model,
                reranker_max_batch=body.reranker_max_batch,
                request_timeout_seconds=body.request_timeout_seconds,
                api_key=body.api_key.get_secret_value(),
            )
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeModelMutationResponse(item=_item_response(view), request_id=context.request_id)


@admin_router.patch("/{configuration_id}", response_model=KnowledgeModelMutationResponse)
async def update_knowledge_model(
    configuration_id: uuid.UUID,
    body: KnowledgeModelUpdateRequest,
    context: Annotated[SystemAuditContext, Depends(require_knowledge_admin_context)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeModelMutationResponse:
    try:
        view = await module.update_model_configuration(
            configuration_id,
            KnowledgeModelConfigurationUpdate(
                display_name=body.display_name,
                status=body.status,
                base_url=body.base_url,
                embedding_model=body.embedding_model,
                embedding_dimension=body.embedding_dimension,
                embedding_max_batch=body.embedding_max_batch,
                reranker_model=body.reranker_model,
                reranker_max_batch=body.reranker_max_batch,
                request_timeout_seconds=body.request_timeout_seconds,
                api_key=(body.api_key.get_secret_value() if body.api_key is not None else None),
            ),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeModelMutationResponse(item=_item_response(view), request_id=context.request_id)


@admin_router.delete("/{configuration_id}", response_model=KnowledgeModelDeleteResponse)
async def delete_knowledge_model(
    configuration_id: uuid.UUID,
    context: Annotated[SystemAuditContext, Depends(require_knowledge_admin_context)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeModelDeleteResponse:
    try:
        await module.delete_model_configuration(configuration_id)
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeModelDeleteResponse(request_id=context.request_id)


@admin_router.post("/{configuration_id}/test", response_model=KnowledgeModelTestResponse)
async def test_knowledge_model(
    configuration_id: uuid.UUID,
    context: Annotated[SystemAuditContext, Depends(require_knowledge_admin_context)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeModelTestResponse:
    try:
        result = await module.test_model_configuration(configuration_id)
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeModelTestResponse(ok=result.ok, message=result.message, request_id=context.request_id)


@project_router.get("/model-options", response_model=KnowledgeModelOptionsResponse)
async def list_knowledge_model_options(
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeModelOptionsResponse:
    try:
        options = await module.list_active_model_options()
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeModelOptionsResponse(
        items=[_option_response(option) for option in options],
        request_id=context.request_id,
    )


@project_router.get("/health", response_model=KnowledgeHealthResponse)
async def knowledge_health(
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeHealthResponse:
    try:
        health = await module.health()
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
                model_configuration_id=body.model_configuration_id,
                description=body.description,
            ),
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
        views, total = await module.list_knowledge_bases(context.project_id, page=page, page_size=page_size)
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
        view = await module.get_knowledge_base(context.project_id, base_id)
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeBaseMutationResponse(item=_base_response(view), request_id=context.request_id)


@project_router.patch("/bases/{base_id}", response_model=KnowledgeBaseMutationResponse)
async def update_knowledge_base(
    base_id: uuid.UUID,
    body: KnowledgeBaseUpdateRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeBaseMutationResponse:
    try:
        view = await module.update_knowledge_base(
            context.project_id,
            base_id,
            KnowledgeBaseUpdate(
                name=body.name,
                description=body.description,
                status=body.status,
                default_top_k=body.default_top_k,
                default_score_threshold=body.default_score_threshold,
            ),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeBaseMutationResponse(item=_base_response(view), request_id=context.request_id)


@project_router.post("/bases/{base_id}/rebuild", response_model=KnowledgeBaseMutationResponse)
async def rebuild_knowledge_base(
    base_id: uuid.UUID,
    body: KnowledgeBaseRebuildRequest,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeBaseMutationResponse:
    """Rebind the model configuration and queue every document for re-embedding."""

    try:
        view = await module.rebuild_knowledge_base(
            context.project_id,
            base_id,
            model_configuration_id=body.model_configuration_id,
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeBaseMutationResponse(item=_base_response(view), request_id=context.request_id)


@project_router.get("/bases/{base_id}/metadata-fields", response_model=KnowledgeMetadataFieldListResponse)
async def list_knowledge_metadata_fields(
    base_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeMetadataFieldListResponse:
    try:
        views = await module.list_metadata_fields(context.project_id, base_id)
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
        view = await module.rename_metadata_field(context.project_id, field_id, name=body.name)
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
        await module.delete_metadata_field(context.project_id, field_id)
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
        view = await module.set_document_metadata(context.project_id, document_id, dict(body.values))
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentMutationResponse(item=_document_response(view), request_id=context.request_id)


@project_router.post("/bases/{base_id}/documents", response_model=KnowledgeDocumentMutationResponse)
async def upload_knowledge_document(
    request: Request,
    base_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
    file: Annotated[UploadFile, File()],
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
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                chunk_separator=chunk_separator,
                remove_extra_spaces=remove_extra_spaces,
                remove_urls_emails=remove_urls_emails,
                chunking_mode=chunking_mode,  # type: ignore[arg-type]  # validated by the package
                child_chunk_size=child_chunk_size,
                child_chunk_separator=child_chunk_separator,
            )
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    finally:
        await _remove_request_temp_path(staging_path)
    return KnowledgeChunkPreviewResponse(
        items=[
            KnowledgeChunkPreviewItemResponse(
                position=chunk.position,
                content=chunk.content,
                word_count=chunk.word_count,
                child_contents=list(chunk.child_contents),
            )
            for chunk in preview.chunks
        ],
        total=preview.total,
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
        views, total = await module.list_documents(context.project_id, base_id, page=page, page_size=page_size)
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
        view = await module.get_document(context.project_id, document_id)
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeDocumentMutationResponse(item=_document_response(view), request_id=context.request_id)


@project_router.get("/documents/{document_id}/download")
async def download_knowledge_document(
    document_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_read)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> FileResponse:
    target_path = await _new_request_temp_path()
    try:
        view = await module.download_document(context.project_id, document_id, target_path)
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


@project_router.delete("/bases/{base_id}", response_model=KnowledgeBaseMutationResponse)
async def delete_knowledge_base(
    base_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(require_project_knowledge_edit)],
    module: Annotated[KnowledgeModule, Depends(get_knowledge_module)],
) -> KnowledgeBaseMutationResponse:
    try:
        view = await module.delete_knowledge_base(context.project_id, base_id)
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
        view = await module.delete_document(context.project_id, document_id)
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
        view = await module.retry_document(context.project_id, document_id)
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
        view = await module.rename_document(context.project_id, document_id, body.name)
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
        views = await module.set_documents_enabled(context.project_id, list(body.document_ids), body.enabled)
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
        views = await module.delete_documents(context.project_id, list(body.document_ids))
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
        view = await module.create_segment(context.project_id, document_id, KnowledgeSegmentCreate(content=body.content))
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
        view = await module.delete_segment(context.project_id, segment_id)
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
        views, total = await module.list_document_segments(context.project_id, document_id, page=page, page_size=page_size)
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
                query=body.query,
                knowledge_base_ids=(tuple(body.knowledge_base_ids) if body.knowledge_base_ids is not None else None),
                top_k=body.top_k,
                score_threshold=body.score_threshold,
                source="retrieval_test",
                metadata_filters=(tuple(KnowledgeMetadataFilter(name=item.name, operator=item.operator, value=item.value) for item in body.metadata_filters) if body.metadata_filters is not None else None),
            )
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return KnowledgeSearchResponse(
        citations=[_citation_response(citation) for citation in result.citations],
        request_id=context.request_id,
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
        views, total = await module.list_recent_queries(context.project_id, base_id, page=page, page_size=page_size)
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
    "KnowledgeDocumentItemResponse",
    "KnowledgeDocumentListResponse",
    "KnowledgeDocumentMetadataRequest",
    "KnowledgeDocumentMutationResponse",
    "KnowledgeDocumentRenameRequest",
    "KnowledgeHealthResponse",
    "KnowledgeMetadataFieldCreateRequest",
    "KnowledgeMetadataFieldDeleteResponse",
    "KnowledgeMetadataFieldItemResponse",
    "KnowledgeMetadataFieldListResponse",
    "KnowledgeMetadataFieldMutationResponse",
    "KnowledgeMetadataFieldRenameRequest",
    "KnowledgeMetadataFilterBody",
    "KnowledgeModelCreateRequest",
    "KnowledgeModelDeleteResponse",
    "KnowledgeModelItemResponse",
    "KnowledgeModelListResponse",
    "KnowledgeModelMutationResponse",
    "KnowledgeModelOptionsResponse",
    "KnowledgeModelTestResponse",
    "KnowledgeModelUpdateRequest",
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
    "admin_router",
    "get_knowledge_module",
    "knowledge_http_exception",
    "project_router",
    "require_knowledge_admin_context",
    "require_project_knowledge_edit",
    "require_project_knowledge_read",
]
