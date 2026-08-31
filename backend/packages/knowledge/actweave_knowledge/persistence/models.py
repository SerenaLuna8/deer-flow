"""Package-owned ORM rows for the seven ``knowledge_*`` tables.

The DDL authority is the host Schema V1 snapshot (``full_schema.sql``); these
mappings mirror it exactly. The runtime never emits DDL from this metadata.
Foreign keys to host tables (``projects``, ``model_provider_models``) exist
only in the SQL snapshot so this metadata stays self-contained.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Double,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class KnowledgeOrmBase(DeclarativeBase):
    """Isolated metadata; never merged into the host declarative Base."""


class KnowledgeBaseRow(KnowledgeOrmBase):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_knowledge_bases_project_id_id"),
        CheckConstraint("btrim(name) <> ''", name="ck_knowledge_bases_name"),
        CheckConstraint("status IN ('active', 'disabled', 'deleting')", name="ck_knowledge_bases_status"),
        CheckConstraint(
            "retrieval_mode IN ('semantic', 'hybrid')",
            name="ck_knowledge_bases_retrieval_mode",
        ),
        CheckConstraint("default_top_k BETWEEN 1 AND 20", name="ck_knowledge_bases_default_top_k"),
        CheckConstraint(
            "default_score_threshold >= 0 AND default_score_threshold <= 1",
            name="ck_knowledge_bases_default_score_threshold",
        ),
        Index("uq_knowledge_bases_project_name", "project_id", text("lower(name)"), unique=True),
        Index("ix_knowledge_bases_project_status", "project_id", "status", text("updated_at DESC"), "id"),
        Index("ix_knowledge_bases_embedding_model", "embedding_model_id"),
        Index("ix_knowledge_bases_reranker_model", "reranker_model_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, server_default=text("''"))
    # Bindings into the host-owned model registry (model_provider_models).
    # The two REFERENCES ... ON DELETE RESTRICT foreign keys live only in the
    # host SQL snapshot; the host KnowledgeModelPort validates type and status.
    embedding_model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reranker_model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    # semantic keeps recall vector-only; hybrid adds the lexical route. A
    # retrieval-test request may override for one call without persisting.
    retrieval_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'semantic'"))
    # Per-base retrieval defaults: used when a search request omits the value
    # (retrieval test prefill and the Agent tool without explicit arguments).
    default_top_k: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("4"))
    default_score_threshold: Mapped[float] = mapped_column(Double, nullable=False, server_default=text("0.2"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class KnowledgeDocumentRow(KnowledgeOrmBase):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("project_id", "knowledge_base_id", "id", name="uq_knowledge_documents_project_base_id"),
        CheckConstraint("btrim(name) <> '' AND btrim(original_name) <> ''", name="ck_knowledge_documents_name"),
        CheckConstraint("btrim(storage_key) <> ''", name="ck_knowledge_documents_storage_key"),
        CheckConstraint("size_bytes >= 0", name="ck_knowledge_documents_size"),
        CheckConstraint(
            "status IN ('uploading', 'queued', 'processing', 'ready', 'failed', 'deleting')",
            name="ck_knowledge_documents_status",
        ),
        CheckConstraint("version >= 1", name="ck_knowledge_documents_version"),
        CheckConstraint(
            "published_version IS NULL OR (published_version >= 1 AND published_version <= version)",
            name="ck_knowledge_documents_published_version",
        ),
        CheckConstraint("chunk_size BETWEEN 200 AND 4000", name="ck_knowledge_documents_chunk_size"),
        CheckConstraint(
            "chunk_overlap BETWEEN 0 AND 500 AND chunk_overlap < chunk_size",
            name="ck_knowledge_documents_chunk_overlap",
        ),
        CheckConstraint(
            "char_length(chunk_separator) BETWEEN 1 AND 64",
            name="ck_knowledge_documents_chunk_separator",
        ),
        CheckConstraint(
            "chunking_mode IN ('general', 'parent_child')",
            name="ck_knowledge_documents_chunking_mode",
        ),
        CheckConstraint(
            "child_chunk_size BETWEEN 100 AND 2000",
            name="ck_knowledge_documents_child_chunk_size",
        ),
        # The child parameters are meaningful only in parent_child mode;
        # general-mode rows keep the column defaults whatever chunk_size is.
        CheckConstraint(
            "chunking_mode = 'general' OR child_chunk_size < chunk_size",
            name="ck_knowledge_documents_child_chunk_ratio",
        ),
        CheckConstraint(
            "char_length(child_chunk_separator) BETWEEN 1 AND 64",
            name="ck_knowledge_documents_child_chunk_separator",
        ),
        CheckConstraint("segment_count >= 0", name="ck_knowledge_documents_segment_count"),
        CheckConstraint("word_count >= 0", name="ck_knowledge_documents_word_count"),
        CheckConstraint("hit_count >= 0", name="ck_knowledge_documents_hit_count"),
        CheckConstraint("jsonb_typeof(doc_metadata) = 'object'", name="ck_knowledge_documents_doc_metadata"),
        CheckConstraint(
            "(status = 'failed' AND error_message IS NOT NULL) OR (status <> 'failed' AND error_message IS NULL)",
            name="ck_knowledge_documents_error",
        ),
        ForeignKeyConstraint(
            ["project_id", "knowledge_base_id"],
            ["knowledge_bases.project_id", "knowledge_bases.id"],
            name="fk_knowledge_documents_base",
            ondelete="RESTRICT",
        ),
        Index("uq_knowledge_documents_storage_key", "storage_key", unique=True),
        Index(
            "ix_knowledge_documents_base_status",
            "project_id",
            "knowledge_base_id",
            "status",
            text("updated_at DESC"),
            "id",
        ),
        # Accelerates the search-path equality filter (doc_metadata @> {...}).
        Index("ix_knowledge_documents_doc_metadata", "doc_metadata", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'uploading'"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # Execution generation of the last successful ingest/reembed publish;
    # NULL only for documents that never published. Manual delete-all keeps
    # it, so "initialized but emptied" stays distinct from "never published".
    # ``content_initialized`` in DTOs derives from it — never a second flag.
    published_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1000"))
    chunk_overlap: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    # Escaped form as typed by the user (e.g. the four characters "\n\n");
    # the splitter decodes \n/\t/\r at use time.
    chunk_separator: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text(r"'\n\n'"))
    remove_extra_spaces: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    remove_urls_emails: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # parent_child mode: parents use the chunk_* parameters above, children
    # split within each parent by the child parameters (overlap always 0).
    chunking_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'general'"))
    child_chunk_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("500"))
    child_chunk_separator: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text(r"'\n'"))
    segment_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    word_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    # Historical sum of the per-query segment hit increments; deleting a
    # segment does not rewrite history.
    hit_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    # Values keyed by the base's metadata-field NAME ({name: string|number}).
    # Field rename/delete rewrites these keys in the same transaction.
    doc_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class KnowledgeMetadataFieldRow(KnowledgeOrmBase):
    """Base-scoped custom metadata field definition (string/number/time).

    Document values live in ``knowledge_documents.doc_metadata`` keyed by the
    field name; renaming or deleting a field rewrites those keys across the
    base's documents in the same transaction. ``time`` values are stored as
    epoch seconds (JSON numbers), so range filters compare numerically.
    """

    __tablename__ = "knowledge_metadata_fields"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_knowledge_metadata_fields_name"),
        CheckConstraint(
            "field_type IN ('string', 'number', 'time')",
            name="ck_knowledge_metadata_fields_type",
        ),
        ForeignKeyConstraint(
            ["project_id", "knowledge_base_id"],
            ["knowledge_bases.project_id", "knowledge_bases.id"],
            name="fk_knowledge_metadata_fields_base",
            ondelete="CASCADE",
        ),
        Index(
            "uq_knowledge_metadata_fields_base_name",
            "knowledge_base_id",
            text("lower(name)"),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    field_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class KnowledgeSegmentRow(KnowledgeOrmBase):
    __tablename__ = "knowledge_segments"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_document_id",
            "document_version",
            "position",
            name="uq_knowledge_segments_document_version_position",
        ),
        CheckConstraint("document_version >= 1", name="ck_knowledge_segments_version"),
        CheckConstraint("position >= 1", name="ck_knowledge_segments_position"),
        CheckConstraint("content <> ''", name="ck_knowledge_segments_content"),
        CheckConstraint("word_count >= 0", name="ck_knowledge_segments_word_count"),
        CheckConstraint("hit_count >= 0", name="ck_knowledge_segments_hit_count"),
        CheckConstraint("jsonb_typeof(source_position) = 'object'", name="ck_knowledge_segments_source_position"),
        CheckConstraint("lexical_version >= 0", name="ck_knowledge_segments_lexical_version"),
        # NULL for parent_child-mode parents: their vectors live on the child
        # rows and general-mode recall filters on ``embedding IS NOT NULL``.
        CheckConstraint(
            "embedding IS NULL OR public.vector_dims(embedding) BETWEEN 1 AND 16000",
            name="ck_knowledge_segments_embedding",
        ),
        ForeignKeyConstraint(
            ["project_id", "knowledge_base_id", "knowledge_document_id"],
            [
                "knowledge_documents.project_id",
                "knowledge_documents.knowledge_base_id",
                "knowledge_documents.id",
            ],
            name="fk_knowledge_segments_document",
            ondelete="CASCADE",
        ),
        Index(
            "ix_knowledge_segments_document",
            "project_id",
            "knowledge_base_id",
            "knowledge_document_id",
            "document_version",
            "position",
        ),
        Index("ix_knowledge_segments_lexical", "lexical_tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    knowledge_document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    source_position: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    embedding: Mapped[Any | None] = mapped_column(Vector(), nullable=True)
    # lexical_v1 derived tokens over normalized content (design §8.1). Both
    # chunking modes maintain the parent field so fusion can score every
    # shortlisted parent; content writes must refresh it in-transaction while
    # re-embeds never touch it. The defaults are the pre-tokenizer placeholder
    # (``lexical_version=0``); the lexical route requires the current version
    # and fails loudly on a mismatch instead of backfilling.
    lexical_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        nullable=False,
        server_default=text("to_tsvector('simple', '')"),
    )
    lexical_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class KnowledgeSegmentChildRow(KnowledgeOrmBase):
    """parent_child mode's vector carrier: one embedded child chunk per row.

    Children never surface in views or citations; retrieval joins them back to
    the parent segment, which owns content, position, and the enabled switch.
    The FK cascades so re-ingests and segment deletions drop children with
    their parents.
    """

    __tablename__ = "knowledge_segment_children"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_segment_id",
            "position",
            name="uq_knowledge_segment_children_segment_position",
        ),
        CheckConstraint("document_version >= 1", name="ck_knowledge_segment_children_version"),
        CheckConstraint("position >= 1", name="ck_knowledge_segment_children_position"),
        CheckConstraint("content <> ''", name="ck_knowledge_segment_children_content"),
        CheckConstraint("word_count >= 0", name="ck_knowledge_segment_children_word_count"),
        CheckConstraint(
            "lexical_version >= 0",
            name="ck_knowledge_segment_children_lexical_version",
        ),
        CheckConstraint(
            "public.vector_dims(embedding) BETWEEN 1 AND 16000",
            name="ck_knowledge_segment_children_embedding",
        ),
        ForeignKeyConstraint(
            ["knowledge_segment_id"],
            ["knowledge_segments.id"],
            name="fk_knowledge_segment_children_segment",
            ondelete="CASCADE",
        ),
        Index(
            "ix_knowledge_segment_children_document",
            "project_id",
            "knowledge_base_id",
            "knowledge_document_id",
            "document_version",
            "position",
        ),
        Index(
            "ix_knowledge_segment_children_lexical",
            "lexical_tsv",
            postgresql_using="gin",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    knowledge_document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    knowledge_segment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    embedding: Mapped[Any] = mapped_column(Vector(), nullable=False)
    # Same contract as the parent segment's lexical fields; the lexical
    # route recalls parent_child bases through these child tokens.
    lexical_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        nullable=False,
        server_default=text("to_tsvector('simple', '')"),
    )
    lexical_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class KnowledgeQueryRow(KnowledgeOrmBase):
    """Append-only retrieval log: one row per search on either path.

    ``knowledge_base_ids`` stores the resolved active bases the search really
    targeted (JSONB array of UUID strings). Rows are owner-private history:
    base deletion keeps them, former-owner/account retention removes the exact
    owner scope, and Project retention removes every owner scope.
    """

    __tablename__ = "knowledge_queries"
    __table_args__ = (
        CheckConstraint("btrim(query) <> ''", name="ck_knowledge_queries_query"),
        CheckConstraint("source IN ('agent', 'retrieval_test')", name="ck_knowledge_queries_source"),
        CheckConstraint(
            "jsonb_typeof(knowledge_base_ids) = 'array'",
            name="ck_knowledge_queries_base_ids",
        ),
        CheckConstraint("result_count >= 0", name="ck_knowledge_queries_result_count"),
        # Cosine similarity floors the score at -1 for rerank-free searches;
        # rerank and rank-fusion scores stay in [0, 1]. NULL still means
        # "no results".
        CheckConstraint(
            "top_score IS NULL OR (top_score >= -1 AND top_score <= 1)",
            name="ck_knowledge_queries_top_score",
        ),
        CheckConstraint(
            "top_score_kind IS NULL OR top_score_kind IN ('cosine', 'rerank', 'rank_fusion')",
            name="ck_knowledge_queries_top_score_kind",
        ),
        CheckConstraint(
            "strategy_version IS NULL OR btrim(strategy_version) <> ''",
            name="ck_knowledge_queries_strategy_version",
        ),
        Index(
            "ix_knowledge_queries_owner_created",
            "project_id",
            "owner_user_id",
            text("created_at DESC"),
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    knowledge_base_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    query: Mapped[str] = mapped_column(String(2000), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    top_score: Mapped[float | None] = mapped_column(Double, nullable=True)
    # Provenance of top_score (cosine/rerank/rank_fusion) and the strategy
    # label that produced this row; NULL on pre-M10 history means unknown.
    top_score_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class KnowledgeTaskRow(KnowledgeOrmBase):
    __tablename__ = "knowledge_tasks"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('ingest_document', 'reembed_document', 'delete_document', 'delete_document_object', 'delete_knowledge_base')",
            name="ck_knowledge_tasks_kind",
        ),
        # Both indexing kinds bind an execution generation; other kinds never.
        CheckConstraint(
            "(kind IN ('ingest_document', 'reembed_document') AND target_version IS NOT NULL AND target_version >= 1) OR (kind NOT IN ('ingest_document', 'reembed_document') AND target_version IS NULL)",
            name="ck_knowledge_tasks_target_version",
        ),
        CheckConstraint(
            "(kind = 'delete_document_object' AND storage_key IS NOT NULL AND btrim(storage_key) <> '') OR (kind <> 'delete_document_object' AND storage_key IS NULL)",
            name="ck_knowledge_tasks_storage_key",
        ),
        # Frozen reparse parameters ride only on an explicit-reparse ingest;
        # a plain upload ingest and every other kind keep NULL.
        CheckConstraint(
            "reparse_settings IS NULL OR (kind = 'ingest_document' AND jsonb_typeof(reparse_settings) = 'object')",
            name="ck_knowledge_tasks_reparse_settings",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')",
            name="ck_knowledge_tasks_status",
        ),
        CheckConstraint(
            "stage IN ('queued', 'reading_source', 'extracting_splitting', 'loading_segments', 'embedding', 'publishing', 'done')",
            name="ck_knowledge_tasks_stage",
        ),
        CheckConstraint(
            "completed_units >= 0 AND (total_units IS NULL OR (total_units >= 0 AND completed_units <= total_units))",
            name="ck_knowledge_tasks_progress_units",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND max_attempts AND max_attempts = 3",
            name="ck_knowledge_tasks_attempts",
        ),
        CheckConstraint(
            "(status = 'running' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR (status <> 'running' AND claim_token IS NULL AND lease_until IS NULL)",
            name="ck_knowledge_tasks_claim",
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'failed') AND finished_at IS NOT NULL) OR (status NOT IN ('succeeded', 'failed') AND finished_at IS NULL)",
            name="ck_knowledge_tasks_finished",
        ),
        Index(
            "ix_knowledge_tasks_claim",
            "available_at",
            "created_at",
            "id",
            postgresql_where=text("status IN ('queued', 'retry_wait')"),
        ),
        Index(
            "ix_knowledge_tasks_expired",
            "lease_until",
            "id",
            postgresql_where=text("status = 'running'"),
        ),
        # One open indexing operation per document/version regardless of
        # kind: a reembed cannot bypass the guard an ingest holds and vice
        # versa (M10 design §5).
        Index(
            "uq_knowledge_tasks_open_indexing",
            "resource_id",
            "target_version",
            unique=True,
            postgresql_where=text("kind IN ('ingest_document', 'reembed_document') AND status IN ('queued', 'running', 'retry_wait')"),
        ),
        Index(
            "uq_knowledge_tasks_open_document_delete",
            "resource_id",
            unique=True,
            postgresql_where=text("kind = 'delete_document' AND status IN ('queued', 'running', 'retry_wait')"),
        ),
        Index(
            "uq_knowledge_tasks_open_document_object_delete",
            "storage_key",
            unique=True,
            postgresql_where=text("kind = 'delete_document_object' AND status IN ('queued', 'running', 'retry_wait')"),
        ),
        Index(
            "uq_knowledge_tasks_open_base_delete",
            "resource_id",
            unique=True,
            postgresql_where=text("kind = 'delete_knowledge_base' AND status IN ('queued', 'running', 'retry_wait')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Complete, strictly validated chunk/clean parameters frozen at reparse
    # admission; retries inherit them. Never a general task payload.
    # ``none_as_null`` keeps an explicit Python None a SQL NULL: the CHECK
    # constraint forbids the JSON 'null' a plain JSONB bind would produce.
    reparse_settings: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'queued'"))
    # Real pipeline stage, orthogonal to status: a failed task keeps its
    # failing stage. ``done`` appears only with the final publish commit.
    stage: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'queued'"))
    # Verified provider-batch progress of the current attempt; a new attempt
    # resets to zero. ``total_units`` stays NULL while no verifiable total
    # exists — the UI never simulates a percentage from it.
    completed_units: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    total_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("3"))
    available_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
