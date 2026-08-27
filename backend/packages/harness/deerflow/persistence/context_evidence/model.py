"""Thread-owned Context Evidence and rebuildable Projection Head rows."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


def _thread_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["project_id", "owner_user_id", "thread_id"],
        [
            "threads_meta.project_id",
            "threads_meta.owner_user_id",
            "threads_meta.thread_id",
        ],
        name=f"fk_{table}_thread",
        ondelete="CASCADE",
    )


def _sequence_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["project_id", "owner_user_id", "thread_id"],
        [
            "context_evidence_sequences.project_id",
            "context_evidence_sequences.owner_user_id",
            "context_evidence_sequences.thread_id",
        ],
        name=f"fk_{table}_sequence",
        ondelete="CASCADE",
    )


_EVIDENCE_TYPES_SQL = (
    "'context.window.opened.v1',"
    " 'request.prepared.v1',"
    " 'request.dispatched.v1',"
    " 'provider.observed.v1',"
    " 'provider.usage_unreported.v1',"
    " 'provider.failed.v1',"
    " 'provider.ambiguous.v1',"
    " 'checkpoint.linked.v1',"
    " 'compaction.committed.v1',"
    " 'context.window.rebased.v1'"
)
_UUID_TEXT_SQL = "'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'"


class ContextEvidenceSequenceRow(Base):
    """Thread-wide high-watermarks for Evidence and Projection publications."""

    __tablename__ = "context_evidence_sequences"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    evidence_high_watermark: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    projection_high_watermark: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
        onupdate=_now,
    )

    __table_args__ = (
        _thread_fk("context_evidence_sequences"),
        CheckConstraint(
            "evidence_high_watermark >= 0 AND projection_high_watermark >= 0",
            name="ck_context_evidence_sequences_watermarks",
        ),
    )


class ContextEvidenceRow(Base):
    """One immutable, content-free fact in a private Thread Evidence log."""

    __tablename__ = "context_evidence"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    context_window_generation: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_schema_version: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    origin_run_id: Mapped[str | None] = mapped_column(String(64))
    provider_call_id: Mapped[str | None] = mapped_column(CHAR(64))
    checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    payload_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "project_id",
            "owner_user_id",
            "thread_id",
            "evidence_seq",
            name="pk_context_evidence",
        ),
        _sequence_fk("context_evidence"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "thread_id",
            "idempotency_key",
            name="uq_context_evidence_idempotency",
        ),
        CheckConstraint(
            f"event_type IN ({_EVIDENCE_TYPES_SQL})",
            name="ck_context_evidence_event_type",
        ),
        CheckConstraint(
            f"(subject_kind = 'lead_thread' AND subject_id = thread_id) OR (subject_kind = 'subagent_task' AND subject_id ~ {_UUID_TEXT_SQL})",
            name="ck_context_evidence_subject",
        ),
        CheckConstraint(
            "payload_schema_version = 1",
            name="ck_context_evidence_payload_schema",
        ),
        CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{64}$' AND payload_digest ~ '^[0-9a-f]{64}$' AND (provider_call_id IS NULL OR provider_call_id ~ '^[0-9a-f]{64}$')",
            name="ck_context_evidence_digests",
        ),
        CheckConstraint(
            "checkpoint_id IS NULL OR checkpoint_id <> ''",
            name="ck_context_evidence_checkpoint",
        ),
        CheckConstraint(
            "jsonb_typeof(payload_json) = 'object'",
            name="ck_context_evidence_payload_object",
        ),
        Index(
            "ix_context_evidence_subject_seq",
            "project_id",
            "owner_user_id",
            "thread_id",
            "subject_kind",
            "subject_id",
            "evidence_seq",
        ),
        Index(
            "ix_context_evidence_origin_run",
            "project_id",
            "owner_user_id",
            "thread_id",
            "origin_run_id",
            "evidence_seq",
            postgresql_where=text("origin_run_id IS NOT NULL"),
        ),
        Index(
            "ix_context_evidence_provider_call",
            "project_id",
            "owner_user_id",
            "thread_id",
            "provider_call_id",
            "evidence_seq",
            postgresql_where=text("provider_call_id IS NOT NULL"),
        ),
    )


class ContextProjectionHeadRow(Base):
    """Latest rebuildable Context Projection for one private Context Subject."""

    __tablename__ = "context_projection_heads"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evidence_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    projector_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    projection_schema_version: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=2,
        server_default=text("2"),
    )
    context_window_generation: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    active_run_id: Mapped[str | None] = mapped_column(String(64))
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    basis: Mapped[str] = mapped_column(String(24), nullable=False)
    coverage: Mapped[str] = mapped_column(String(16), nullable=False)
    freshness: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    projection_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
        onupdate=_now,
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "project_id",
            "owner_user_id",
            "thread_id",
            "subject_kind",
            "subject_id",
            name="pk_context_projection_heads",
        ),
        _sequence_fk("context_projection_heads"),
        CheckConstraint(
            f"(subject_kind = 'lead_thread' AND subject_id = thread_id) OR (subject_kind = 'subagent_task' AND subject_id ~ {_UUID_TEXT_SQL})",
            name="ck_context_projection_heads_subject",
        ),
        CheckConstraint(
            "projection_seq >= 1 AND evidence_seq >= 0 AND projection_schema_version = 2",
            name="ck_context_projection_heads_versions",
        ),
        CheckConstraint(
            "projector_revision ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*-v[1-9][0-9]*$'",
            name="ck_context_projection_heads_projector_revision",
        ),
        CheckConstraint(
            "checkpoint_id IS NULL OR checkpoint_id <> ''",
            name="ck_context_projection_heads_checkpoint",
        ),
        CheckConstraint(
            "phase IN ('idle', 'active', 'settled')",
            name="ck_context_projection_heads_phase",
        ),
        CheckConstraint(
            "basis IN ('provider_confirmed', 'hybrid', 'estimated', 'empty')",
            name="ck_context_projection_heads_basis",
        ),
        CheckConstraint(
            "coverage IN ('complete', 'partial')",
            name="ck_context_projection_heads_coverage",
        ),
        CheckConstraint(
            "freshness IN ('current', 'stale')",
            name="ck_context_projection_heads_freshness",
        ),
        CheckConstraint(
            "payload_digest ~ '^[0-9a-f]{64}$'",
            name="ck_context_projection_heads_digest",
        ),
        CheckConstraint(
            "jsonb_typeof(projection_json) = 'object'",
            name="ck_context_projection_heads_payload_object",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "thread_id",
            "projection_seq",
            name="uq_context_projection_heads_projection_seq",
        ),
        Index(
            "ix_context_projection_heads_replay",
            "project_id",
            "owner_user_id",
            "thread_id",
            "projection_seq",
        ),
    )


__all__ = [
    "ContextEvidenceRow",
    "ContextEvidenceSequenceRow",
    "ContextProjectionHeadRow",
]
