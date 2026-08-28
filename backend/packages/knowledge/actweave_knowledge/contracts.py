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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Error codes and error type
# ---------------------------------------------------------------------------

KNOWLEDGE_DISABLED = "KNOWLEDGE_DISABLED"
KNOWLEDGE_NOT_FOUND = "KNOWLEDGE_NOT_FOUND"
KNOWLEDGE_NAME_CONFLICT = "KNOWLEDGE_NAME_CONFLICT"
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
    access_key: str = Field(min_length=1)
    secret_key: str = Field(min_length=1)
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
    api_key: str
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
    api_key: str | None = None


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


@dataclass(frozen=True, slots=True)
class KnowledgeBaseView:
    id: UUID
    project_id: UUID
    name: str
    description: str
    model_configuration_id: UUID
    status: KnowledgeBaseStatus
    document_count: int
    delete_error: str | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Knowledge Document DTOs
# ---------------------------------------------------------------------------

KnowledgeDocumentStatus = Literal["uploading", "queued", "processing", "ready", "failed", "deleting"]


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
    version: int
    chunk_size: int
    chunk_overlap: int
    segment_count: int
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
    source_position: dict[str, Any]
    created_at: datetime


# ---------------------------------------------------------------------------
# Search DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnowledgeSearchRequest:
    """Search input; ``project_id`` comes from host context, never request bodies."""

    project_id: UUID
    query: str
    knowledge_base_ids: tuple[UUID, ...] | None = None
    top_k: int | None = None
    score_threshold: float | None = None


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


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnowledgeHealth:
    enabled: bool
    database_ok: bool
    storage_ok: bool
    message: str = ""
