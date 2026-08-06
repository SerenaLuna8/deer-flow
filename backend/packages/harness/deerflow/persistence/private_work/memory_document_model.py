"""Final owner-private Memory document persistence model."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


def _scope_constraints(table: str) -> tuple[ForeignKeyConstraint, ...]:
    return (
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=f"fk_{table}_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=f"fk_{table}_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name=f"fk_{table}_membership",
            ondelete="RESTRICT",
        ),
    )


class MemoryHistoryEntryRow(Base):
    __tablename__ = "memory_history_entries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sequence: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=False),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False)
    committed_checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    tagged_text: Mapped[str | None] = mapped_column(Text)
    content_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    preference_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    snip_prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_model_ref: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    dream_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        *_scope_constraints("memory_history_entries"),
        UniqueConstraint("sequence", name="uq_memory_history_entries_sequence"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "id",
            name="uq_memory_history_entries_scope",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "thread_id",
            "source_digest",
            name="uq_memory_history_entries_source",
        ),
        ForeignKeyConstraint(
            ["dream_job_id", "project_id", "owner_user_id", "namespace"],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id", "jobs.namespace"],
            name="fk_memory_history_entries_dream_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["summary_model_ref"],
            ["system_model_config_versions.id"],
            name="fk_memory_history_entries_summary_model",
            ondelete="RESTRICT",
        ),
        CheckConstraint("namespace <> ''", name="ck_memory_history_entries_namespace"),
        CheckConstraint(
            "source_checkpoint_id <> '' AND committed_checkpoint_id <> ''",
            name="ck_memory_history_entries_checkpoints",
        ),
        CheckConstraint(
            "source_digest ~ '^[0-9a-f]{64}$' AND content_digest ~ '^[0-9a-f]{64}$'",
            name="ck_memory_history_entries_digests",
        ),
        CheckConstraint(
            "preference_version >= 1",
            name="ck_memory_history_entries_preference_version",
        ),
        CheckConstraint(
            "snip_prompt_version <> ''",
            name="ck_memory_history_entries_contract",
        ),
        CheckConstraint(
            "tagged_text IS NULL OR char_length(tagged_text) <= 1000",
            name="ck_memory_history_entries_text_size",
        ),
        CheckConstraint(
            "(status = 'pending' AND tagged_text IS NOT NULL AND dream_job_id IS NULL AND consumed_at IS NULL) OR "
            "(status = 'processing' AND tagged_text IS NOT NULL AND dream_job_id IS NOT NULL AND consumed_at IS NULL) OR "
            "(status = 'consumed' AND tagged_text IS NULL AND dream_job_id IS NOT NULL AND consumed_at IS NOT NULL)",
            name="ck_memory_history_entries_lifecycle",
        ),
        Index(
            "ix_memory_history_entries_pending",
            "project_id",
            "owner_user_id",
            "namespace",
            "sequence",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_memory_history_entries_dream_job",
            "dream_job_id",
            "sequence",
            postgresql_where=text("dream_job_id IS NOT NULL"),
        ),
    )


class MemoryDocumentRow(Base):
    __tablename__ = "memory_documents"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    dream_cursor: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    active_dream_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            name="pk_memory_documents",
        ),
        *_scope_constraints("memory_documents"),
        ForeignKeyConstraint(
            ["active_dream_job_id", "project_id", "owner_user_id", "namespace"],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id", "jobs.namespace"],
            name="fk_memory_documents_active_dream_job",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "active_dream_job_id",
            name="uq_memory_documents_active_dream_job",
        ),
        CheckConstraint("namespace <> ''", name="ck_memory_documents_namespace"),
        CheckConstraint(
            "content_digest ~ '^[0-9a-f]{64}$'",
            name="ck_memory_documents_digest",
        ),
        CheckConstraint(
            "char_length(content) <= 16000",
            name="ck_memory_documents_content_size",
        ),
        CheckConstraint(
            "version >= 0 AND dream_cursor >= 0",
            name="ck_memory_documents_versions",
        ),
    )


class MemoryDreamRunRow(Base):
    __tablename__ = "memory_dream_runs"

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    history_from: Mapped[int] = mapped_column(BigInteger, nullable=False)
    history_to: Mapped[int] = mapped_column(BigInteger, nullable=False)
    history_count: Mapped[int] = mapped_column(Integer, nullable=False)
    history_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    base_document_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_content_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    preference_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model_ref: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    result_version: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        *_scope_constraints("memory_dream_runs"),
        UniqueConstraint(
            "job_id",
            "project_id",
            "owner_user_id",
            "namespace",
            name="uq_memory_dream_runs_job_scope",
        ),
        ForeignKeyConstraint(
            ["job_id", "project_id", "owner_user_id", "namespace"],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id", "jobs.namespace"],
            name="fk_memory_dream_runs_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace"],
            [
                "memory_documents.project_id",
                "memory_documents.owner_user_id",
                "memory_documents.namespace",
            ],
            name="fk_memory_dream_runs_document",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["model_ref"],
            ["system_model_config_versions.id"],
            name="fk_memory_dream_runs_model",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "result_version"],
            [
                "memory_document_versions.project_id",
                "memory_document_versions.owner_user_id",
                "memory_document_versions.namespace",
                "memory_document_versions.version",
            ],
            name="fk_memory_dream_runs_result_version",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint("namespace <> ''", name="ck_memory_dream_runs_namespace"),
        CheckConstraint(
            "trigger IN ('auto_dream', 'manual_dream')",
            name="ck_memory_dream_runs_trigger",
        ),
        CheckConstraint(
            "history_count BETWEEN 1 AND 20 AND history_from >= 1 AND history_to >= history_from",
            name="ck_memory_dream_runs_history",
        ),
        CheckConstraint(
            "history_digest ~ '^[0-9a-f]{64}$' AND base_content_digest ~ '^[0-9a-f]{64}$'",
            name="ck_memory_dream_runs_digests",
        ),
        CheckConstraint(
            "base_document_version >= 0 AND preference_version >= 1 AND policy_revision >= 1",
            name="ck_memory_dream_runs_versions",
        ),
        CheckConstraint(
            "prompt_version <> ''",
            name="ck_memory_dream_runs_contract",
        ),
        CheckConstraint(
            "(result_version IS NULL AND completed_at IS NULL) OR (result_version >= 1 AND completed_at IS NOT NULL)",
            name="ck_memory_dream_runs_result",
        ),
    )


class MemoryDocumentVersionRow(Base):
    __tablename__ = "memory_document_versions"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    unified_diff: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    dream_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    history_from: Mapped[int | None] = mapped_column(BigInteger)
    history_to: Mapped[int | None] = mapped_column(BigInteger)
    history_count: Mapped[int | None] = mapped_column(Integer)
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    model_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)
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
            "namespace",
            "version",
            name="pk_memory_document_versions",
        ),
        *_scope_constraints("memory_document_versions"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace"],
            [
                "memory_documents.project_id",
                "memory_documents.owner_user_id",
                "memory_documents.namespace",
            ],
            name="fk_memory_document_versions_document",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["dream_job_id", "project_id", "owner_user_id", "namespace"],
            [
                "memory_dream_runs.job_id",
                "memory_dream_runs.project_id",
                "memory_dream_runs.owner_user_id",
                "memory_dream_runs.namespace",
            ],
            name="fk_memory_document_versions_dream_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["model_ref"],
            ["system_model_config_versions.id"],
            name="fk_memory_document_versions_model",
            ondelete="RESTRICT",
        ),
        CheckConstraint("namespace <> ''", name="ck_memory_document_versions_namespace"),
        CheckConstraint("version >= 1", name="ck_memory_document_versions_version"),
        CheckConstraint(
            "content_digest ~ '^[0-9a-f]{64}$'",
            name="ck_memory_document_versions_digest",
        ),
        CheckConstraint(
            "char_length(content) <= 16000",
            name="ck_memory_document_versions_content_size",
        ),
        CheckConstraint(
            "trigger IN ('auto_dream', 'manual_dream', 'restore')",
            name="ck_memory_document_versions_trigger",
        ),
        CheckConstraint(
            "(trigger = 'restore' AND dream_job_id IS NULL AND history_from IS NULL AND history_to IS NULL "
            "AND history_count IS NULL AND prompt_version IS NULL AND model_ref IS NULL) OR "
            "(trigger IN ('auto_dream', 'manual_dream') AND dream_job_id IS NOT NULL "
            "AND history_from >= 1 AND history_to >= history_from AND history_count BETWEEN 1 AND 20 "
            "AND prompt_version IS NOT NULL AND prompt_version <> '' AND model_ref IS NOT NULL)",
            name="ck_memory_document_versions_source",
        ),
        Index(
            "uq_memory_document_versions_dream_job",
            "dream_job_id",
            unique=True,
            postgresql_where=text("dream_job_id IS NOT NULL"),
        ),
    )


class RunMemoryContextSnapshotRow(Base):
    __tablename__ = "run_memory_context_snapshots"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    document_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
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
            "run_id",
            "namespace",
            name="pk_run_memory_context_snapshots",
        ),
        *_scope_constraints("run_memory_context_snapshots"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "run_id",
            name="uq_run_memory_context_snapshots_run",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.run_id"],
            name="fk_run_memory_context_snapshots_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "namespace <> ''",
            name="ck_run_memory_context_snapshots_namespace",
        ),
        CheckConstraint(
            "document_version >= 1",
            name="ck_run_memory_context_snapshots_version",
        ),
        CheckConstraint(
            "content_digest ~ '^[0-9a-f]{64}$'",
            name="ck_run_memory_context_snapshots_digest",
        ),
        CheckConstraint(
            "content <> '' AND char_length(content) <= 16000",
            name="ck_run_memory_context_snapshots_content",
        ),
    )


__all__ = [
    "MemoryDocumentRow",
    "MemoryDocumentVersionRow",
    "MemoryDreamRunRow",
    "MemoryHistoryEntryRow",
    "RunMemoryContextSnapshotRow",
]
