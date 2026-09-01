"""Public contracts of the ActWeave Knowledge Package.

Only symbols re-exported from :mod:`actweave_knowledge` are public API. ORM
models, repositories, object-store and provider clients stay internal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from .extraction.contracts import ParseWarning, ProcessingProfile, SourceSpan
    from .ingestion.profiles import ProcessingParameters

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


KNOWLEDGE_MAX_SEGMENTS_PER_DOCUMENT = 5000


class KnowledgeSettings(BaseModel):
    """Startup configuration for the Knowledge feature.

    The host projects its persisted system settings into this configuration.
    A missing settings row is equivalent to ``enabled=false``; the package
    neither reads host configuration files nor repairs host persistence.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    etl_type: Literal["builtin", "unstructured_local"] = "builtin"
    extraction_cache_enabled: bool = True
    worker_concurrency: int = Field(default=2, ge=1, le=16)
    task_timeout_seconds: int = Field(default=900, ge=30, le=7200)
    # MinIO's Python SDK buffers a single PUT part in process memory. Keep the
    # configurable ceiling at the 50 MiB product default so one accepted file
    # cannot turn the SDK's one-part path into a multi-GiB allocation.
    upload_max_bytes: int = Field(default=52428800, ge=1, le=50 * 1024**2)
    max_knowledge_bases_per_project: int = Field(default=20, ge=1)
    max_documents_per_knowledge_base: int = Field(default=500, ge=1)
    max_segments_per_document: int = Field(default=KNOWLEDGE_MAX_SEGMENTS_PER_DOCUMENT, ge=1, le=KNOWLEDGE_MAX_SEGMENTS_PER_DOCUMENT)
    # In-process query-vector cache (per Gateway/Worker process, LRU + TTL).
    # ``enabled=false`` keeps the cache object constructed but never hitting.
    query_cache_enabled: bool = True
    query_cache_max_entries: int = Field(default=512, ge=16, le=65536)
    query_cache_ttl_seconds: int = Field(default=300, ge=5, le=86400)
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

    Every session-taking method runs inside the caller's transaction
    (``session``): binding paths lock Provider then Model ``FOR SHARE`` so
    they serialize with the registry's ``FOR UPDATE`` write paths, and
    material resolution validates type and ``active`` status before
    decrypting. Any unresolvable model — missing, wrong type, disabled, or
    undecryptable material — raises
    ``KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE)``. The package never joins
    or imports host ORM tables.

    Summary generation is the exception to the session rule:
    ``resolve_summary_model`` reads the host's system settings inside the
    caller's transaction and returns ``None`` when no summary model is
    configured, while ``generate_summary`` performs the model call itself and
    must never hold a database session across it. A configured-but-invalid
    model raises ``KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE)``; a failed
    generation call raises ``KnowledgeError(KNOWLEDGE_TASK_FAILED)`` with a
    provider-safe message.
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

    async def resolve_summary_model(
        self,
        session: AsyncSession,
    ) -> str | None: ...

    async def generate_summary(
        self,
        *,
        model_ref: str,
        prompt: str,
    ) -> str: ...


# ---------------------------------------------------------------------------
# Knowledge Base DTOs
# ---------------------------------------------------------------------------

KnowledgeBaseStatus = Literal["active", "disabled", "deleting"]

# One retrieval strategy per base; ``hybrid`` adds the lexical route while
# ``semantic`` stays vector-only. A search request may override for one call.
KnowledgeRetrievalMode = Literal["semantic", "hybrid"]

# Search and per-base default share the same ceiling.
KNOWLEDGE_MAX_TOP_K = 20


@dataclass(frozen=True, slots=True)
class KnowledgeBaseCreate:
    name: str
    embedding_model_id: UUID | None = None
    description: str = ""
    reranker_model_id: UUID | None = None
    retrieval_mode: KnowledgeRetrievalMode = "semantic"


@dataclass(frozen=True, slots=True)
class KnowledgeBaseUpdate:
    """Partial update; ``None`` keeps the stored value.

    Embedding can only be set on an unconfigured base with no documents;
    changing an existing binding remains an explicit rebuild operation.
    Rerank rebinding is tri-state: ``reranker_model_id`` set rebinds without a
    rebuild, ``clear_reranker_model=True`` switches reranking off, and neither
    keeps the stored binding. Setting both is invalid.
    """

    name: str | None = None
    description: str | None = None
    status: Literal["active", "disabled"] | None = None
    default_top_k: int | None = None
    default_score_threshold: float | None = None
    embedding_model_id: UUID | None = None
    reranker_model_id: UUID | None = None
    clear_reranker_model: bool = False
    retrieval_mode: KnowledgeRetrievalMode | None = None
    summary_index_enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeBaseView:
    id: UUID
    project_id: UUID
    name: str
    description: str
    embedding_model_id: UUID | None
    reranker_model_id: UUID | None
    retrieval_mode: KnowledgeRetrievalMode
    summary_index_enabled: bool
    status: KnowledgeBaseStatus
    document_count: int
    default_top_k: int
    default_score_threshold: float
    delete_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class KnowledgeRebuildResult:
    """Outcome of a base-level re-embed: rebind plus per-document admission.

    ``skipped_document_ids`` lists never-published failed documents that stay
    failed: re-embedding has no published content to read, and reparsing the
    original file must remain an explicit, separate decision.
    """

    base: KnowledgeBaseView
    accepted_document_count: int
    skipped_document_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeSummaryBackfill:
    """Admission outcome of the summary backfill queued by turning the
    base's summary-index switch on.

    ``skipped_document_ids`` lists ready documents whose backfill task could
    not be queued right now (typically an already-open task on the same
    document); they are reported, never silently dropped.
    """

    accepted_document_count: int
    skipped_document_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeBaseUpdateResult:
    """Base update outcome; ``summary_backfill`` is populated only when this
    update turned the summary-index switch on."""

    base: KnowledgeBaseView
    summary_backfill: KnowledgeSummaryBackfill | None = None


# ---------------------------------------------------------------------------
# Knowledge Document DTOs
# ---------------------------------------------------------------------------

KnowledgeDocumentStatus = Literal["uploading", "queued", "processing", "ready", "failed", "deleting"]

# Real pipeline stages; orthogonal to task status. A failed task keeps its
# failing stage, and ``done`` appears only when the final publish commits.
KnowledgeTaskStage = Literal[
    "queued",
    "reading_source",
    "extracting_splitting",
    "loading_segments",
    "summarizing",
    "embedding",
    "publishing",
    "done",
]

KnowledgeTaskProgressStatus = Literal["queued", "running", "retry_wait", "failed"]

KnowledgeIndexingTaskKind = Literal["ingest_document", "reembed_document", "summarize_document"]


@dataclass(frozen=True, slots=True)
class KnowledgeTaskProgress:
    """Safe projection of the open indexing task bound to the current version.

    Deliberately excludes claim tokens, lease deadlines, storage keys and raw
    provider errors. ``total_units`` is ``None`` while no verifiable total
    exists (never a simulated percentage), ``completed_units`` counts only
    provider batches that validated successfully, and ``next_attempt_at`` is
    populated solely in ``retry_wait``.
    """

    kind: KnowledgeIndexingTaskKind
    status: KnowledgeTaskProgressStatus
    stage: KnowledgeTaskStage
    completed_units: int
    total_units: int | None
    attempt_count: int
    max_attempts: int
    target_version: int
    next_attempt_at: datetime | None = None


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
    processing_profile: ProcessingParameters | None = None
    expected_preview_fingerprint: str | None = None


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
    # Derived from ``published_version IS NOT NULL``: distinguishes a document
    # that has published at least once (even if later edited down to zero
    # segments) from one that never published successfully. Defaults to the
    # conservative reading so a projection that says nothing shows "not yet".
    content_initialized: bool = False
    # Open indexing task bound to the current target version, if any.
    task_progress: KnowledgeTaskProgress | None = None
    parsing_profile: ProcessingProfile | None = None
    parse_warnings: tuple[ParseWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentAttachmentView:
    """One selectable image from a Document's current publication."""

    attachment_id: UUID
    ref: str
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    width: int
    height: int


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
    token_count: int = 0
    source_spans: tuple[SourceSpan, ...] = ()


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


# ``current`` rows belong to the document's published content generation;
# ``stale`` rows were left behind by a failed reprocessing run and are
# read-only maintenance evidence, never retrievable content.
KnowledgeContentState = Literal["current", "stale"]

KNOWLEDGE_SEGMENT_DETAIL_CHILD_PAGE_SIZE = 50

# Segment-summary generation contract. The prompt is package-fixed; bumping
# ``KNOWLEDGE_SUMMARY_PROMPT_VERSION`` is an explicit regeneration decision,
# never a silent rewrite. Segments shorter than the minimum keep only their
# content vector; generated text is hard-truncated at the char ceiling and
# the model call itself is capped at the token ceiling.
KNOWLEDGE_SUMMARY_PROMPT_VERSION = 1
KNOWLEDGE_SUMMARY_MIN_SOURCE_CHARS = 200
KNOWLEDGE_SUMMARY_MAX_CHARS = 1000
KNOWLEDGE_SUMMARY_MAX_TOKENS = 1024


@dataclass(frozen=True, slots=True)
class KnowledgeSegmentChildView:
    """One child chunk shown in the segment detail; never cited directly."""

    id: UUID
    position: int
    content: str
    word_count: int


@dataclass(frozen=True, slots=True)
class KnowledgeSegmentSummaryView:
    """System-generated recall summary of one segment.

    A recall aid only: it may route a query to its segment but is never the
    citation content — hit passages always come from the segment itself.
    """

    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class KnowledgeSegmentAttachmentView:
    """One safe published image occurrence in Segment-detail order."""

    attachment_id: UUID
    ref: str
    alt_text: str
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class KnowledgeSegmentDetail:
    """Authoritative single-segment read with paged children.

    Opened from a search hit, callers supply the expected document version
    and content digest; a mismatch raises ``KNOWLEDGE_CONFLICT`` instead of
    silently explaining old scores with new text. Plain maintenance browsing
    omits the expectations and may read ``stale`` rows left by a failed
    reprocessing, clearly labeled and excluded from retrieval.
    """

    segment: KnowledgeSegmentView
    knowledge_base_id: UUID
    document_id: UUID
    document_name: str
    content_state: KnowledgeContentState
    stored_content_version: int
    current_document_version: int
    children_total: int
    child_page: int
    children: tuple[KnowledgeSegmentChildView, ...] = ()
    summary: KnowledgeSegmentSummaryView | None = None
    attachments: tuple[KnowledgeSegmentAttachmentView, ...] = ()


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
# Filter-field discovery and bounded batch assignment
# ---------------------------------------------------------------------------

KnowledgeFilterFieldKind = Literal["custom", "builtin"]

# Read-only builtin filter fields projected from document authority columns;
# they are never copied into ``doc_metadata`` and never writable.
KNOWLEDGE_BUILTIN_FILTER_FIELDS: tuple[str, ...] = (
    "document_name",
    "uploaded_at",
    "file_type",
    "source_type",
)

# Authority sources: name, created_at (epoch seconds), the original file's
# extension (lowercased), and the fixed ingestion channel "file_upload".
KNOWLEDGE_BUILTIN_FILTER_FIELD_TYPES: dict[str, KnowledgeMetadataFieldType] = {
    "document_name": "string",
    "uploaded_at": "time",
    "file_type": "string",
    "source_type": "string",
}

# Operators a field of each type supports. For custom fields this is
# advisory (a mismatched value is a non-match); for builtin fields, whose
# types are fixed here, an unsupported operator is an invalid request.
KNOWLEDGE_FILTER_OPERATORS_BY_TYPE: dict[KnowledgeMetadataFieldType, tuple[KnowledgeMetadataFilterOperator, ...]] = {
    "string": ("eq", "contains"),
    "number": ("eq", "gte", "lte"),
    "time": ("eq", "gte", "lte"),
}

# Discovery budget: at most this many bases per call (each base already caps
# custom fields at KNOWLEDGE_MAX_METADATA_FIELDS_PER_BASE plus 4 builtins).
# A wider scope must be narrowed explicitly — never silently truncated.
KNOWLEDGE_MAX_FILTER_DISCOVERY_BASES = 20

KNOWLEDGE_MAX_BATCH_METADATA_DOCUMENTS = 100
KNOWLEDGE_MAX_BATCH_METADATA_FIELDS = 20


@dataclass(frozen=True, slots=True)
class KnowledgeFilterFieldView:
    """One filterable field: stable identity, type, operators, writability."""

    kind: KnowledgeFilterFieldKind
    name: str
    field_type: KnowledgeMetadataFieldType
    operators: tuple[KnowledgeMetadataFilterOperator, ...]
    writable: bool


@dataclass(frozen=True, slots=True)
class KnowledgeBaseFilterFields:
    """Discovered filter fields of one base (builtin plus custom)."""

    knowledge_base_id: UUID
    fields: tuple[KnowledgeFilterFieldView, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeMetadataBatchPatch:
    """Bounded common patch over documents of one base, all-or-nothing.

    Untouched keys stay, ``None`` removes the key, builtin names are
    rejected. Applies to at most ``KNOWLEDGE_MAX_BATCH_METADATA_DOCUMENTS``
    documents and ``KNOWLEDGE_MAX_BATCH_METADATA_FIELDS`` fields in one
    transaction; any authority, existence or type conflict rolls back the
    whole batch. Metadata changes never trigger re-embedding.
    """

    document_ids: tuple[UUID, ...]
    values: dict[str, str | int | float | None]


# ---------------------------------------------------------------------------
# Chunk preview DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnowledgeChunkPreviewAttachment:
    """Logical image occurrence projected without storage or database identity."""

    ref: str
    alt_text: str


@dataclass(frozen=True, slots=True)
class KnowledgePreviewAttachment:
    """One bounded safe raster embedded in the preview response."""

    ref: str
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    data_base64: str


@dataclass(frozen=True, slots=True)
class KnowledgePreviewTableSource:
    """Server-derived table-header diagnostic for CSV and Excel previews."""

    sheet: str | None
    header_mode: Literal["auto", "none", "explicit"]
    header_row: int | None
    header_cells: tuple[str, ...] = ()


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
    processing_profile: ProcessingParameters | None = None
    expected_preview_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeChunkPreviewChunk:
    """One previewed segment; ``child_contents`` is empty in general mode."""

    position: int
    content: str
    word_count: int
    child_contents: tuple[str, ...] = ()
    token_count: int = 0
    source_spans: tuple[SourceSpan, ...] = ()
    attachments: tuple[KnowledgeChunkPreviewAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeChunkPreview:
    """First chunks plus the total count the same parameters would ingest."""

    total: int
    chunks: tuple[KnowledgeChunkPreviewChunk, ...]
    preview_fingerprint: str = ""
    source_sha256: str = ""
    effective_profile: ProcessingProfile | None = None
    warnings: tuple[ParseWarning, ...] = ()
    preview_attachments: tuple[KnowledgePreviewAttachment, ...] = ()
    omitted_preview_attachment_count: int = 0
    table_sources: tuple[KnowledgePreviewTableSource, ...] = ()


# ---------------------------------------------------------------------------
# Reprocessing DTOs (re-embed keeps content; reparse rebuilds from the file)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnowledgeReparseRequest:
    """Explicit re-parse of the original file with freshly confirmed settings.

    ``expected_version`` is a CAS guard against concurrent edits. The chunk
    settings are validated completely, frozen onto the task's dedicated
    ``reparse_settings``, and replace the document's stored parameters only
    when the new content publishes successfully. This is never a model change.
    """

    expected_version: int
    chunk_size: int = 1000
    chunk_overlap: int = 100
    chunk_separator: str = KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR
    remove_extra_spaces: bool = False
    remove_urls_emails: bool = False
    chunking_mode: KnowledgeChunkingMode = "general"
    child_chunk_size: int = 500
    child_chunk_separator: str = KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR
    processing_profile: ProcessingParameters | None = None
    expected_preview_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeReparsePreview:
    """Read-only re-parse preview computed from the stored original file.

    ``document_version`` is the version the preview was computed against;
    submitting the reparse still performs its own CAS check.
    """

    document_version: int
    preview: KnowledgeChunkPreview


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
    ``field_kind`` separates read-only builtin fields from custom ones so a
    custom field may reuse a builtin name without ambiguity.
    """

    name: str
    operator: KnowledgeMetadataFilterOperator
    value: str | int | float
    field_kind: KnowledgeFilterFieldKind = "custom"


@dataclass(frozen=True, slots=True)
class KnowledgeSearchRequest:
    """Search input; scope identities come from host context, never request bodies.

    ``top_k``/``score_threshold`` left as ``None`` resolve to the per-base
    defaults stored on the targeted knowledge bases. ``source`` labels the
    query-log row; it never changes ranking. ``metadata_filters`` restrict
    recall to documents matching every condition. ``retrieval_mode`` set
    overrides the per-base mode for this one call (retrieval test only) and
    is never persisted; ``debug`` adds the bounded safe diagnostics.
    """

    project_id: UUID
    owner_user_id: UUID
    query: str
    knowledge_base_ids: tuple[UUID, ...] | None = None
    top_k: int | None = None
    score_threshold: float | None = None
    source: KnowledgeQuerySource = "retrieval_test"
    metadata_filters: tuple[KnowledgeMetadataFilter, ...] | None = None
    retrieval_mode: KnowledgeRetrievalMode | None = None
    debug: bool = False


# ---------------------------------------------------------------------------
# Score provenance and hits
# ---------------------------------------------------------------------------

# ``citation.score``/``top_score`` provenance: a native cosine similarity in
# [-1, 1], a native reranker relevance in [0, 1], or the [0, 1] reciprocal
# rank fusion of the final ranking. Fusion scores are ordering evidence, not
# calibrated confidence, and thresholds never apply to them.
KnowledgeScoreKind = Literal["cosine", "rerank", "rank_fusion"]

# Native score kinds a threshold can act on (never ``rank_fusion``).
KnowledgeLocalScoreKind = Literal["cosine", "rerank"]

KnowledgeRecallRoute = Literal["semantic", "lexical"]

KnowledgeEmptyReason = Literal["not_ready", "no_candidates", "filtered_out", "stale_candidates"]

# Versioned retrieval strategy label written to query logs and diagnostics.
KNOWLEDGE_STRATEGY_VERSION = "m10.1"

# Fixed lexical derivation version (see retrieval/lexical); bumping it
# requires re-running the quality evaluation.
KNOWLEDGE_LEXICAL_VERSION = 1

# With a hybrid target, the query may carry at most this many deduplicated
# lexical tokens; longer queries must shorten or switch to semantic. A pure
# semantic search never builds a lexical query and never hits this cap.
KNOWLEDGE_MAX_LEXICAL_QUERY_TOKENS = 128

# Global parent-candidate budget shared by every search (design §8.2).
KNOWLEDGE_GLOBAL_PARENT_CANDIDATE_BUDGET = 400

# At most this many really-recalled children are projected per hit.
KNOWLEDGE_MAX_MATCHED_CHILDREN = 3

# What actually produced the hit's native semantic score: the segment's own
# vector, a child chunk's vector, or the segment-summary vector. Lexical-only
# hits keep the ``segment`` attribution.
KnowledgeMatchedVia = Literal["segment", "child", "summary"]


@dataclass(frozen=True, slots=True)
class KnowledgeMatchedChild:
    """One child chunk that really participated in recall for this hit.

    Carried by the recall transaction itself; never reconstructed by
    scanning children after the fact.
    """

    child_id: UUID
    position: int
    route: KnowledgeRecallRoute
    score: float


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
    # New writes always provide these; historical citations legally lack
    # them and render as short quotes with unknown provenance.
    document_version: int | None = None
    content_digest: str | None = None
    score_kind: KnowledgeScoreKind | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeSearchHit:
    """One search hit: the single source for citations and projections.

    ``passage`` is the complete parent segment text from the recall snapshot
    (never the whole original file). ``local_score`` is the native score the
    threshold acted on; ``ranking_method``/``ranking_score`` explain the
    final order. ``document_version`` and ``content_digest`` let a detail
    read detect that the same segment ID now carries different content.
    """

    citation: KnowledgeCitation
    passage: str
    document_version: int
    content_digest: str
    local_score: float
    local_score_kind: KnowledgeLocalScoreKind
    score_domain: str
    ranking_method: KnowledgeScoreKind
    ranking_score: float
    matched_children: tuple[KnowledgeMatchedChild, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeHitDiagnostics:
    """Per-hit safe diagnostics; no passage, child text or losing candidates."""

    segment_id: UUID
    local_score: float
    local_score_kind: KnowledgeLocalScoreKind
    score_domain: str
    ranking_method: KnowledgeScoreKind
    ranking_score: float
    # Defaults to ``segment`` until the recall transaction records real
    # attribution; only ever the actual score source, never a guess.
    matched_via: KnowledgeMatchedVia = "segment"
    matched_children: tuple[KnowledgeMatchedChild, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeRouteCounts:
    """Actual recall/filter counts of one search."""

    semantic_candidates: int = 0
    lexical_candidates: int = 0
    summary_candidates: int = 0
    parents_deduplicated: int = 0
    threshold_filtered: int = 0
    stale_filtered: int = 0
    returned: int = 0
    query_embedding_cache_hits: int = 0
    query_embedding_cache_misses: int = 0


@dataclass(frozen=True, slots=True)
class KnowledgeSearchTimings:
    """Server-side monotonic stage durations in milliseconds."""

    query_embedding_ms: float = 0.0
    recall_ms: float = 0.0
    rerank_ms: float = 0.0
    final_validation_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class KnowledgeSearchDiagnostics:
    """Bounded safe diagnostics returned only on ``debug=true`` responses.

    Exists solely in the response — never written to logs or a tracing
    table. Model identities are limited to registry ids the project's model
    options already expose; endpoints, keys and vectors stay out.
    """

    strategy_version: str
    lexical_version: int
    target_base_count: int
    effective_top_k: int
    per_base_route_budget: int
    retrieval_mode: KnowledgeRetrievalMode
    counts: KnowledgeRouteCounts
    timings: KnowledgeSearchTimings
    model_ids: tuple[UUID, ...] = ()
    ranking_method: KnowledgeScoreKind | None = None
    empty_reason: KnowledgeEmptyReason | None = None
    heterogeneous_without_lexical_evidence: bool = False
    hit_diagnostics: tuple[KnowledgeHitDiagnostics, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    """Search outcome; ``hits`` is the only ranked source of truth.

    ``citations`` is derived per access so no second ordered list can drift
    from the hits. ``diagnostics`` is present only for ``debug=true``.
    """

    hits: tuple[KnowledgeSearchHit, ...] = ()
    diagnostics: KnowledgeSearchDiagnostics | None = None

    @property
    def citations(self) -> tuple[KnowledgeCitation, ...]:
        return tuple(hit.citation for hit in self.hits)


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
    # Provenance of ``top_score``; historical rows without it show unknown.
    top_score_kind: KnowledgeScoreKind | None = None
    strategy_version: str | None = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnowledgeHealth:
    enabled: bool
    database_ok: bool
    storage_ok: bool
    message: str = ""
