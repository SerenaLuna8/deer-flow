"""Public contracts of the ActWeave Knowledge Package.

Only symbols re-exported from :mod:`actweave_knowledge` are public API. ORM
models, repositories, object-store and provider clients stay internal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Error codes and error type
# ---------------------------------------------------------------------------

KNOWLEDGE_DISABLED = "KNOWLEDGE_DISABLED"
KNOWLEDGE_FORBIDDEN = "KNOWLEDGE_FORBIDDEN"
KNOWLEDGE_NOT_FOUND = "KNOWLEDGE_NOT_FOUND"
KNOWLEDGE_NAME_CONFLICT = "KNOWLEDGE_NAME_CONFLICT"
KNOWLEDGE_CONFLICT = "KNOWLEDGE_CONFLICT"
KNOWLEDGE_INVALID_REQUEST = "KNOWLEDGE_INVALID_REQUEST"
KNOWLEDGE_QUOTA_EXCEEDED = "KNOWLEDGE_QUOTA_EXCEEDED"
KNOWLEDGE_MODEL_UNAVAILABLE = "KNOWLEDGE_MODEL_UNAVAILABLE"
KNOWLEDGE_STORAGE_UNAVAILABLE = "KNOWLEDGE_STORAGE_UNAVAILABLE"
KNOWLEDGE_PARSE_FAILED = "KNOWLEDGE_PARSE_FAILED"
KNOWLEDGE_EMBEDDING_FAILED = "KNOWLEDGE_EMBEDDING_FAILED"
KNOWLEDGE_RERANK_FAILED = "KNOWLEDGE_RERANK_FAILED"
KNOWLEDGE_SEARCH_FAILED = "KNOWLEDGE_SEARCH_FAILED"
KNOWLEDGE_TASK_FAILED = "KNOWLEDGE_TASK_FAILED"


class KnowledgeError(Exception):
    """Business error with a stable code and a displayable message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class KnowledgeMinioSettings(BaseModel):
    """MinIO object-store connection settings (S3 API endpoint, not Console)."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=1, description="S3 API host:port, no scheme")
    bucket: str = Field(min_length=1)
    access_key: str = Field(min_length=1, repr=False)
    # SecretStr keeps the credential out of repr(), str() and model_dump();
    # consumers call ``secret_key.get_secret_value()`` at the MinIO boundary.
    secret_key: SecretStr = Field(min_length=1)
    secure: bool = False

    @field_validator("endpoint")
    @classmethod
    def _endpoint_has_no_scheme(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("endpoint must not be empty")
        if "://" in cleaned:
            raise ValueError("endpoint must be host:port without URL scheme")
        return cleaned


class KnowledgeSettings(BaseModel):
    """Startup configuration for the Knowledge feature.

    Values come from the repository-root ``config.yaml`` ``knowledge`` block.
    A missing block is equivalent to ``enabled=false``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    worker_concurrency: int = Field(default=2, ge=1, le=16)
    task_timeout_seconds: int = Field(default=900, ge=30, le=7200)
    # MinIO's Python SDK buffers a single PUT part in process memory. Keep the
    # configurable ceiling at the 50 MiB product default so one accepted file
    # cannot turn the SDK's one-part path into a multi-GiB allocation.
    upload_max_bytes: int = Field(default=52428800, ge=1, le=50 * 1024**2)
    max_knowledge_bases_per_project: int = Field(default=20, ge=1)
    max_documents_per_knowledge_base: int = Field(default=500, ge=1)
    max_segments_per_document: int = Field(default=5000, ge=1, le=5000)
    minio: KnowledgeMinioSettings | None = None

    @model_validator(mode="after")
    def _require_minio_when_enabled(self) -> KnowledgeSettings:
        if self.enabled and self.minio is None:
            raise ValueError("knowledge.minio is required when knowledge.enabled is true")
        return self


# ---------------------------------------------------------------------------
# Model port (host-owned registry)
# ---------------------------------------------------------------------------

KnowledgeModelType = Literal["embedding", "rerank"]


@dataclass(frozen=True, slots=True)
class KnowledgeEmbeddingMaterial:
    """One embedding model materialized for provider calls, plaintext key included.

    Instances live only in memory for the duration of a call; the plaintext
    ``api_key`` never reaches logs (``repr=False``) or storage.
    """

    model_id: UUID
    base_url: str
    model_name: str
    dimension: int
    max_batch: int
    request_timeout_seconds: int
    api_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class KnowledgeRerankMaterial:
    """One rerank model materialized for provider calls, plaintext key included."""

    model_id: UUID
    base_url: str
    model_name: str
    max_batch: int
    request_timeout_seconds: int
    api_key: str = field(repr=False)


class KnowledgeModelPort(Protocol):
    """Host-provided access to the registry rows Knowledge Bases bind.

    Every method runs inside the caller's transaction (``session``): binding
    paths lock Provider then Model ``FOR SHARE`` so they serialize with the
    registry's ``FOR UPDATE`` write paths, and material resolution validates
    type and ``active`` status before decrypting. Any unresolvable model —
    missing, wrong type, disabled, or undecryptable material — raises
    ``KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE)``. The package never joins
    or imports host ORM tables.
    """

    async def lock_model_for_binding(
        self,
        session: AsyncSession,
        model_id: UUID,
        model_type: KnowledgeModelType,
    ) -> None: ...

    async def embedding_material(
        self,
        session: AsyncSession,
        model_id: UUID,
    ) -> KnowledgeEmbeddingMaterial: ...

    async def rerank_material(
        self,
        session: AsyncSession,
        model_id: UUID,
    ) -> KnowledgeRerankMaterial: ...


# ---------------------------------------------------------------------------
# Knowledge Base DTOs
# ---------------------------------------------------------------------------

KnowledgeBaseStatus = Literal["active", "disabled", "deleting"]

# Search and per-base default share the same ceiling.
KNOWLEDGE_MAX_TOP_K = 20


@dataclass(frozen=True, slots=True)
class KnowledgeBaseCreate:
    name: str
    embedding_model_id: UUID
    description: str = ""
    reranker_model_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeBaseUpdate:
    """Partial update; ``None`` keeps the stored value.

    Rerank rebinding is tri-state: ``reranker_model_id`` set rebinds without a
    rebuild, ``clear_reranker_model=True`` switches reranking off, and neither
    keeps the stored binding. Setting both is invalid.
    """

    name: str | None = None
    description: str | None = None
    status: Literal["active", "disabled"] | None = None
    default_top_k: int | None = None
    default_score_threshold: float | None = None
    reranker_model_id: UUID | None = None
    clear_reranker_model: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgeBaseView:
    id: UUID
    project_id: UUID
    name: str
    description: str
    embedding_model_id: UUID
    reranker_model_id: UUID | None
    status: KnowledgeBaseStatus
    document_count: int
    default_top_k: int
    default_score_threshold: float
    delete_error: str | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Knowledge Document DTOs
# ---------------------------------------------------------------------------

KnowledgeDocumentStatus = Literal["uploading", "queued", "processing", "ready", "failed", "deleting"]

# Escaped form as typed by the user; the splitter decodes \n/\t/\r at use time.
KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR = "\\n\\n"
KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR = "\\n"

KnowledgeChunkingMode = Literal["general", "parent_child"]


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentUpload:
    """One uploaded file staged at ``source_path`` by the host for this request."""

    name: str
    original_name: str
    source_path: Path
    size_bytes: int
    media_type: str | None = None
    chunk_size: int = 1000
    chunk_overlap: int = 100
    chunk_separator: str = KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR
    remove_extra_spaces: bool = False
    remove_urls_emails: bool = False
    chunking_mode: KnowledgeChunkingMode = "general"
    child_chunk_size: int = 500
    child_chunk_separator: str = KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentView:
    id: UUID
    project_id: UUID
    knowledge_base_id: UUID
    name: str
    original_name: str
    media_type: str | None
    size_bytes: int
    status: KnowledgeDocumentStatus
    enabled: bool
    version: int
    chunk_size: int
    chunk_overlap: int
    chunk_separator: str
    remove_extra_spaces: bool
    remove_urls_emails: bool
    chunking_mode: KnowledgeChunkingMode
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


@dataclass(frozen=True, slots=True)
class KnowledgeSegmentView:
    id: UUID
    document_version: int
    position: int
    content: str
    word_count: int
    enabled: bool
    hit_count: int
    source_position: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class KnowledgeSegmentCreate:
    """One manually added segment; embedded with the base's embedding model."""

    content: str


@dataclass(frozen=True, slots=True)
class KnowledgeSegmentUpdate:
    """Partial update; ``None`` keeps the stored value.

    A ``content`` change re-embeds the segment synchronously; ``enabled``
    only flips retrieval visibility and never touches the vector.
    """

    content: str | None = None
    enabled: bool | None = None


# ---------------------------------------------------------------------------
# Metadata field DTOs
# ---------------------------------------------------------------------------

KnowledgeMetadataFieldType = Literal["string", "number", "time"]

KNOWLEDGE_MAX_METADATA_FIELDS_PER_BASE = 20
KNOWLEDGE_MAX_METADATA_NAME_LENGTH = 64
KNOWLEDGE_MAX_METADATA_STRING_LENGTH = 500


@dataclass(frozen=True, slots=True)
class KnowledgeMetadataFieldView:
    """One base-scoped custom metadata field definition.

    Document values live in ``KnowledgeDocumentView.doc_metadata`` keyed by
    ``name``; ``time`` values are epoch seconds so range filters compare
    numerically.
    """

    id: UUID
    knowledge_base_id: UUID
    name: str
    field_type: KnowledgeMetadataFieldType
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Chunk preview DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnowledgeChunkPreviewRequest:
    """Preview input staged like an upload; nothing is stored or queued."""

    original_name: str
    source_path: Path
    size_bytes: int
    chunk_size: int = 1000
    chunk_overlap: int = 100
    chunk_separator: str = KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR
    remove_extra_spaces: bool = False
    remove_urls_emails: bool = False
    chunking_mode: KnowledgeChunkingMode = "general"
    child_chunk_size: int = 500
    child_chunk_separator: str = KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR


@dataclass(frozen=True, slots=True)
class KnowledgeChunkPreviewChunk:
    """One previewed segment; ``child_contents`` is empty in general mode."""

    position: int
    content: str
    word_count: int
    child_contents: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeChunkPreview:
    """First chunks plus the total count the same parameters would ingest."""

    total: int
    chunks: tuple[KnowledgeChunkPreviewChunk, ...]


# ---------------------------------------------------------------------------
# Search DTOs
# ---------------------------------------------------------------------------


KnowledgeQuerySource = Literal["agent", "retrieval_test"]

KnowledgeMetadataFilterOperator = Literal["eq", "contains", "gte", "lte"]

KNOWLEDGE_MAX_METADATA_FILTERS = 10


@dataclass(frozen=True, slots=True)
class KnowledgeMetadataFilter:
    """One manual document-metadata condition; conditions AND together.

    ``eq`` compares by exact JSON value (string or number), ``contains``
    substring-matches string values, and ``gte``/``lte`` compare number and
    time (epoch seconds) values numerically. A document without the key —
    or whose value has a mismatched JSON type — never matches.
    """

    name: str
    operator: KnowledgeMetadataFilterOperator
    value: str | int | float


@dataclass(frozen=True, slots=True)
class KnowledgeSearchRequest:
    """Search input; scope identities come from host context, never request bodies.

    ``top_k``/``score_threshold`` left as ``None`` resolve to the per-base
    defaults stored on the targeted knowledge bases. ``source`` labels the
    query-log row; it never changes ranking. ``metadata_filters`` restrict
    recall to documents matching every condition.
    """

    project_id: UUID
    owner_user_id: UUID
    query: str
    knowledge_base_ids: tuple[UUID, ...] | None = None
    top_k: int | None = None
    score_threshold: float | None = None
    source: KnowledgeQuerySource = "retrieval_test"
    metadata_filters: tuple[KnowledgeMetadataFilter, ...] | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeCitation:
    knowledge_base_id: UUID
    knowledge_base_name: str
    document_id: UUID
    document_name: str
    segment_id: UUID
    segment_position: int
    snippet: str
    score: float
    source_position: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    citations: tuple[KnowledgeCitation, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeQueryView:
    """One query-log row for the retrieval test page's recent-query list."""

    id: UUID
    knowledge_base_ids: tuple[UUID, ...]
    query: str
    source: KnowledgeQuerySource
    result_count: int
    top_score: float | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnowledgeHealth:
    enabled: bool
    database_ok: bool
    storage_ok: bool
    message: str = ""
