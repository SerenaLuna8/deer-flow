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

# ---------------------------------------------------------------------------
# Error codes and error type
# ---------------------------------------------------------------------------

KNOWLEDGE_DISABLED = "KNOWLEDGE_DISABLED"
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
    upload_max_bytes: int = Field(default=52428800, ge=1)
    max_knowledge_bases_per_project: int = Field(default=20, ge=1)
    max_documents_per_knowledge_base: int = Field(default=500, ge=1)
    max_segments_per_document: int = Field(default=5000, ge=1)
    minio: KnowledgeMinioSettings | None = None

    @model_validator(mode="after")
    def _require_minio_when_enabled(self) -> KnowledgeSettings:
        if self.enabled and self.minio is None:
            raise ValueError("knowledge.minio is required when knowledge.enabled is true")
        return self


# ---------------------------------------------------------------------------
# Secret port
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnowledgeProtectedSecret:
    """Encrypted API-key material held by a model configuration row."""

    nonce: bytes
    ciphertext: bytes


class KnowledgeSecretPort(Protocol):
    """Host-provided encryption for the shared model-configuration API key."""

    def protect_api_key(self, configuration_id: UUID, api_key: str) -> KnowledgeProtectedSecret: ...

    def materialize_api_key(self, configuration_id: UUID, secret: KnowledgeProtectedSecret) -> str: ...


# ---------------------------------------------------------------------------
# Model configuration DTOs
# ---------------------------------------------------------------------------

KnowledgeModelConfigurationStatus = Literal["active", "disabled"]


@dataclass(frozen=True, slots=True)
class KnowledgeModelConfigurationCreate:
    display_name: str
    base_url: str
    embedding_model: str
    embedding_dimension: int
    reranker_model: str
    api_key: str = field(repr=False)
    embedding_max_batch: int = 64
    reranker_max_batch: int = 32
    request_timeout_seconds: int = 30


@dataclass(frozen=True, slots=True)
class KnowledgeModelConfigurationUpdate:
    """Partial update; ``None`` keeps the stored value."""

    display_name: str | None = None
    status: KnowledgeModelConfigurationStatus | None = None
    base_url: str | None = None
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    embedding_max_batch: int | None = None
    reranker_model: str | None = None
    reranker_max_batch: int | None = None
    request_timeout_seconds: int | None = None
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class KnowledgeModelConfigurationView:
    id: UUID
    display_name: str
    status: KnowledgeModelConfigurationStatus
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


@dataclass(frozen=True, slots=True)
class KnowledgeModelOption:
    id: UUID
    display_name: str
    embedding_model: str
    embedding_dimension: int
    reranker_model: str


@dataclass(frozen=True, slots=True)
class KnowledgeModelConnectionResult:
    ok: bool
    message: str


# ---------------------------------------------------------------------------
# Knowledge Base DTOs
# ---------------------------------------------------------------------------

KnowledgeBaseStatus = Literal["active", "disabled", "deleting"]

# Search and per-base default share the same ceiling.
KNOWLEDGE_MAX_TOP_K = 20


@dataclass(frozen=True, slots=True)
class KnowledgeBaseCreate:
    name: str
    model_configuration_id: UUID
    description: str = ""


@dataclass(frozen=True, slots=True)
class KnowledgeBaseUpdate:
    """Partial update; ``None`` keeps the stored value."""

    name: str | None = None
    description: str | None = None
    status: Literal["active", "disabled"] | None = None
    default_top_k: int | None = None
    default_score_threshold: float | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeBaseView:
    id: UUID
    project_id: UUID
    name: str
    description: str
    model_configuration_id: UUID
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
    """One manually added segment; embedded with the base's model configuration."""

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
    """Search input; ``project_id`` comes from host context, never request bodies.

    ``top_k``/``score_threshold`` left as ``None`` resolve to the per-base
    defaults stored on the targeted knowledge bases. ``source`` labels the
    query-log row; it never changes ranking. ``metadata_filters`` restrict
    recall to documents matching every condition.
    """

    project_id: UUID
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
