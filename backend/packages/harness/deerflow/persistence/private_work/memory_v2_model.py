"""ORM contract for the staged project-private Memory v2 pipeline."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


def _memory_scope_constraints(table: str) -> tuple:
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
        CheckConstraint("namespace <> ''", name=f"ck_{table}_namespace"),
    )


class MemorySourceBatchRow(Base):
    __tablename__ = "memory_source_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_attempt_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    pipeline_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_section: Mapped[str] = mapped_column(String(32), nullable=False, default="agent_runtime", server_default="agent_runtime")
    policy_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    policy_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_identity_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_hmac_key_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    suppressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppression_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        *_memory_scope_constraints("memory_source_batches"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "id", name="uq_memory_source_batches_scope"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "run_id",
            "source_attempt_id",
            "source_identity_digest",
            name="uq_memory_source_batches_identity",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_memory_source_batches_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_attempt_id", "source_job_id"],
            ["job_attempts.id", "job_attempts.job_id"],
            name="fk_memory_source_batches_attempt",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_job_id", "project_id", "owner_user_id", "run_id"],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id", "jobs.run_id"],
            name="fk_memory_source_batches_source_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "owner_user_id",
                "run_id",
                "policy_section",
                "policy_version_id",
                "policy_schema_version",
                "policy_checksum",
            ],
            [
                "run_runtime_policy_snapshots.project_id",
                "run_runtime_policy_snapshots.owner_user_id",
                "run_runtime_policy_snapshots.run_id",
                "run_runtime_policy_snapshots.section",
                "run_runtime_policy_snapshots.policy_version_id",
                "run_runtime_policy_snapshots.schema_version",
                "run_runtime_policy_snapshots.payload_checksum",
            ],
            name="fk_memory_source_batches_policy_snapshot",
            ondelete="RESTRICT",
        ),
        CheckConstraint("pipeline_mode IN ('shadow', 'consolidate', 'v2')", name="ck_memory_source_batches_mode"),
        CheckConstraint("policy_section = 'agent_runtime'", name="ck_memory_source_batches_policy_section"),
        CheckConstraint("policy_schema_version >= 1", name="ck_memory_source_batches_policy_schema"),
        CheckConstraint("policy_checksum ~ '^[0-9a-f]{64}$'", name="ck_memory_source_batches_policy_checksum"),
        CheckConstraint("source_identity_digest ~ '^[0-9a-f]{64}$'", name="ck_memory_source_batches_identity_digest"),
        CheckConstraint("source_item_count >= 0", name="ck_memory_source_batches_item_count"),
        CheckConstraint(
            "(suppressed_at IS NULL) = (suppression_reason IS NULL)",
            name="ck_memory_source_batches_suppression",
        ),
        Index("ix_memory_source_batches_run", "project_id", "owner_user_id", "namespace", "run_id"),
    )


class MemorySourceItemRow(Base):
    __tablename__ = "memory_source_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    source_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_event_sequence: Mapped[int | None] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    content_hmac: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        *_memory_scope_constraints("memory_source_items"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "id", name="uq_memory_source_items_scope"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "source_batch_id",
            "id",
            name="uq_memory_source_items_batch_scope",
        ),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "source_batch_id", "ordinal", name="uq_memory_source_items_order"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "source_batch_id",
            "source_message_id",
            name="uq_memory_source_items_message",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "source_batch_id"],
            [
                "memory_source_batches.project_id",
                "memory_source_batches.owner_user_id",
                "memory_source_batches.namespace",
                "memory_source_batches.id",
            ],
            name="fk_memory_source_items_batch",
            ondelete="CASCADE",
        ),
        CheckConstraint("ordinal >= 0", name="ck_memory_source_items_ordinal"),
        CheckConstraint("run_event_sequence IS NULL OR run_event_sequence >= 0", name="ck_memory_source_items_event_sequence"),
        CheckConstraint("role = 'user'", name="ck_memory_source_items_role"),
        CheckConstraint("content_hmac ~ '^[0-9a-f]{64}$'", name="ck_memory_source_items_hmac"),
        CheckConstraint(
            "(content IS NOT NULL AND content <> '' AND source_erased_at IS NULL) OR (content IS NULL AND source_erased_at IS NOT NULL)",
            name="ck_memory_source_items_content",
        ),
        CheckConstraint("content IS NULL OR char_length(content) <= 64000", name="ck_memory_source_items_content_size"),
    )


class MemoryExtractionGenerationRow(Base):
    __tablename__ = "memory_extraction_generations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    source_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    contract_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model_config_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    model_config_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    model_config_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    output_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        *_memory_scope_constraints("memory_extraction_generations"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "id", name="uq_memory_extraction_generations_scope"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "source_batch_id",
            "id",
            name="uq_memory_extraction_generations_batch_scope",
        ),
        UniqueConstraint("job_id", name="uq_memory_extraction_generations_job"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "source_batch_id",
            "contract_digest",
            name="uq_memory_extraction_generations_contract",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "source_batch_id"],
            [
                "memory_source_batches.project_id",
                "memory_source_batches.owner_user_id",
                "memory_source_batches.namespace",
                "memory_source_batches.id",
            ],
            name="fk_memory_extraction_generations_batch",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["job_id", "project_id", "owner_user_id", "namespace"],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id", "jobs.namespace"],
            name="fk_memory_extraction_generations_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["model_config_id", "model_config_version_id", "model_config_checksum"],
            [
                "system_model_config_versions.model_config_id",
                "system_model_config_versions.id",
                "system_model_config_versions.payload_checksum",
            ],
            name="fk_memory_extraction_generations_model",
            ondelete="RESTRICT",
        ),
        CheckConstraint("contract_digest ~ '^[0-9a-f]{64}$'", name="ck_memory_extraction_generations_contract"),
        CheckConstraint("model_config_checksum ~ '^[0-9a-f]{64}$'", name="ck_memory_extraction_generations_model_checksum"),
        CheckConstraint("policy_revision >= 1", name="ck_memory_extraction_generations_policy"),
        CheckConstraint(
            "prompt_version <> '' AND extractor_version <> '' AND output_schema_version <> ''",
            name="ck_memory_extraction_generations_versions",
        ),
        Index(
            "ix_memory_extraction_generations_uncommitted",
            "project_id",
            "owner_user_id",
            "namespace",
            postgresql_where=text("candidate_committed_at IS NULL"),
        ),
    )


class MemoryConsolidationGenerationRow(Base):
    __tablename__ = "memory_consolidation_generations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    candidate_input_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    model_config_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    model_config_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    model_config_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    consolidator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    output_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        *_memory_scope_constraints("memory_consolidation_generations"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "id", name="uq_memory_consolidation_generations_scope"),
        UniqueConstraint("job_id", name="uq_memory_consolidation_generations_job"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "candidate_input_digest",
            "contract_digest",
            name="uq_memory_consolidation_generations_contract",
        ),
        ForeignKeyConstraint(
            ["job_id", "project_id", "owner_user_id", "namespace"],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id", "jobs.namespace"],
            name="fk_memory_consolidation_generations_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["model_config_id", "model_config_version_id", "model_config_checksum"],
            [
                "system_model_config_versions.model_config_id",
                "system_model_config_versions.id",
                "system_model_config_versions.payload_checksum",
            ],
            name="fk_memory_consolidation_generations_model",
            ondelete="RESTRICT",
        ),
        CheckConstraint("candidate_input_digest ~ '^[0-9a-f]{64}$'", name="ck_memory_consolidation_generations_input"),
        CheckConstraint("contract_digest ~ '^[0-9a-f]{64}$'", name="ck_memory_consolidation_generations_contract"),
        CheckConstraint("model_config_checksum ~ '^[0-9a-f]{64}$'", name="ck_memory_consolidation_generations_model_checksum"),
        CheckConstraint("candidate_count BETWEEN 1 AND 20", name="ck_memory_consolidation_generations_count"),
        CheckConstraint("policy_revision >= 1", name="ck_memory_consolidation_generations_policy"),
        CheckConstraint(
            "prompt_version <> '' AND consolidator_version <> '' AND output_schema_version <> ''",
            name="ck_memory_consolidation_generations_versions",
        ),
        Index(
            "ix_memory_consolidation_generations_uncommitted",
            "project_id",
            "owner_user_id",
            "namespace",
            postgresql_where=text("fact_committed_at IS NULL"),
        ),
    )


class MemoryCandidateRow(Base):
    __tablename__ = "memory_candidates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    source_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    extraction_generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    consolidation_generation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    content_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    retention_class: Mapped[str] = mapped_column(String(16), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    decision_reason: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        *_memory_scope_constraints("memory_candidates"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "id", name="uq_memory_candidates_scope"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "extraction_generation_id",
            "ordinal",
            name="uq_memory_candidates_generation_order",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "extraction_generation_id",
            "content_digest",
            name="uq_memory_candidates_generation_digest",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "source_batch_id", "extraction_generation_id"],
            [
                "memory_extraction_generations.project_id",
                "memory_extraction_generations.owner_user_id",
                "memory_extraction_generations.namespace",
                "memory_extraction_generations.source_batch_id",
                "memory_extraction_generations.id",
            ],
            name="fk_memory_candidates_extraction",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "source_batch_id", "source_item_id"],
            [
                "memory_source_items.project_id",
                "memory_source_items.owner_user_id",
                "memory_source_items.namespace",
                "memory_source_items.source_batch_id",
                "memory_source_items.id",
            ],
            name="fk_memory_candidates_source_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "consolidation_generation_id"],
            [
                "memory_consolidation_generations.project_id",
                "memory_consolidation_generations.owner_user_id",
                "memory_consolidation_generations.namespace",
                "memory_consolidation_generations.id",
            ],
            name="fk_memory_candidates_consolidation",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="ck_memory_candidates_ordinal"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memory_candidates_confidence"),
        CheckConstraint("retention_class IN ('permanent', 'durable', 'ephemeral')", name="ck_memory_candidates_retention"),
        CheckConstraint("sensitivity IN ('normal', 'sensitive', 'restricted')", name="ck_memory_candidates_sensitivity"),
        CheckConstraint("status IN ('pending', 'accepted', 'rejected', 'superseded')", name="ck_memory_candidates_status"),
        CheckConstraint(
            "(status = 'pending' AND decision_reason IS NULL AND decided_at IS NULL) OR (status <> 'pending' AND decision_reason IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_memory_candidates_decision",
        ),
        CheckConstraint("content_digest ~ '^[0-9a-f]{64}$'", name="ck_memory_candidates_content_digest"),
        CheckConstraint(
            "(content IS NOT NULL AND content <> '' AND content_erased_at IS NULL) OR (content IS NULL AND content_erased_at IS NOT NULL)",
            name="ck_memory_candidates_content",
        ),
        CheckConstraint("content IS NULL OR char_length(content) <= 16000", name="ck_memory_candidates_content_size"),
        Index("ix_memory_candidates_pending", "project_id", "owner_user_id", "namespace", "status", "created_at", "id"),
    )


class MemoryFactRow(Base):
    __tablename__ = "memory_facts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    fact_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    current_revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        *_memory_scope_constraints("memory_facts"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "id", name="uq_memory_facts_scope"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "id", "current_revision_id"],
            [
                "memory_fact_revisions.project_id",
                "memory_fact_revisions.owner_user_id",
                "memory_fact_revisions.namespace",
                "memory_fact_revisions.fact_id",
                "memory_fact_revisions.id",
            ],
            name="fk_memory_facts_current_revision",
            ondelete="RESTRICT",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("fact_kind <> ''", name="ck_memory_facts_kind"),
        CheckConstraint("status IN ('active', 'disabled', 'superseded', 'deleted')", name="ck_memory_facts_status"),
        CheckConstraint("version >= 1", name="ck_memory_facts_version"),
        CheckConstraint(
            "(status = 'active' AND disabled_at IS NULL AND superseded_at IS NULL AND deleted_at IS NULL) OR "
            "(status = 'disabled' AND disabled_at IS NOT NULL AND superseded_at IS NULL AND deleted_at IS NULL) OR "
            "(status = 'superseded' AND superseded_at IS NOT NULL AND deleted_at IS NULL) OR "
            "(status = 'deleted' AND deleted_at IS NOT NULL)",
            name="ck_memory_facts_status_time",
        ),
        Index("ix_memory_facts_active", "project_id", "owner_user_id", "namespace", "status", "updated_at", "id"),
    )


class MemoryFactRevisionRow(Base):
    __tablename__ = "memory_fact_revisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    fact_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    content_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    changed_by: Mapped[str] = mapped_column(String(16), nullable=False)
    source_candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    supersedes_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    change_reason: Mapped[str | None] = mapped_column(String(64))
    content_erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        *_memory_scope_constraints("memory_fact_revisions"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "id", name="uq_memory_fact_revisions_scope"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "fact_id", "id", name="uq_memory_fact_revisions_fact_scope"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "fact_id",
            "id",
            "content_digest",
            name="uq_memory_fact_revisions_exact_content",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "fact_id",
            "revision_number",
            name="uq_memory_fact_revisions_number",
        ),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "revision_sequence", name="uq_memory_fact_revisions_sequence"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "fact_id"],
            ["memory_facts.project_id", "memory_facts.owner_user_id", "memory_facts.namespace", "memory_facts.id"],
            name="fk_memory_fact_revisions_fact",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "source_candidate_id"],
            [
                "memory_candidates.project_id",
                "memory_candidates.owner_user_id",
                "memory_candidates.namespace",
                "memory_candidates.id",
            ],
            name="fk_memory_fact_revisions_candidate",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "fact_id", "supersedes_revision_id"],
            [
                "memory_fact_revisions.project_id",
                "memory_fact_revisions.owner_user_id",
                "memory_fact_revisions.namespace",
                "memory_fact_revisions.fact_id",
                "memory_fact_revisions.id",
            ],
            name="fk_memory_fact_revisions_supersedes",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision_number >= 1 AND revision_sequence >= 1", name="ck_memory_fact_revisions_numbers"),
        CheckConstraint("content_digest ~ '^[0-9a-f]{64}$'", name="ck_memory_fact_revisions_content_digest"),
        CheckConstraint("category <> ''", name="ck_memory_fact_revisions_category"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memory_fact_revisions_confidence"),
        CheckConstraint("changed_by IN ('user', 'system', 'consolidator')", name="ck_memory_fact_revisions_changed_by"),
        CheckConstraint("valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from", name="ck_memory_fact_revisions_validity"),
        CheckConstraint(
            "(content IS NOT NULL AND content <> '' AND content_erased_at IS NULL) OR (content IS NULL AND content_erased_at IS NOT NULL)",
            name="ck_memory_fact_revisions_content",
        ),
        CheckConstraint("content IS NULL OR char_length(content) <= 16000", name="ck_memory_fact_revisions_content_size"),
        Index("ix_memory_fact_revisions_sequence", "project_id", "owner_user_id", "namespace", "revision_sequence"),
    )


class MemoryFactEvidenceRow(Base):
    __tablename__ = "memory_fact_evidence"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    fact_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    thread_id: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64))
    run_event_sequence: Mapped[int | None] = mapped_column(BigInteger)
    source_identity_hmac: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    evidence_excerpt: Mapped[str | None] = mapped_column(Text)
    trust_class: Mapped[str] = mapped_column(String(16), nullable=False)
    source_erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        *_memory_scope_constraints("memory_fact_evidence"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "id", name="uq_memory_fact_evidence_scope"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "revision_id",
            "source_identity_hmac",
            name="uq_memory_fact_evidence_identity",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "fact_id", "revision_id"],
            [
                "memory_fact_revisions.project_id",
                "memory_fact_revisions.owner_user_id",
                "memory_fact_revisions.namespace",
                "memory_fact_revisions.fact_id",
                "memory_fact_revisions.id",
            ],
            name="fk_memory_fact_evidence_revision",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "source_candidate_id"],
            [
                "memory_candidates.project_id",
                "memory_candidates.owner_user_id",
                "memory_candidates.namespace",
                "memory_candidates.id",
            ],
            name="fk_memory_fact_evidence_candidate",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "source_item_id"],
            [
                "memory_source_items.project_id",
                "memory_source_items.owner_user_id",
                "memory_source_items.namespace",
                "memory_source_items.id",
            ],
            name="fk_memory_fact_evidence_source_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_memory_fact_evidence_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint("run_event_sequence IS NULL OR run_event_sequence >= 0", name="ck_memory_fact_evidence_event_sequence"),
        CheckConstraint("source_identity_hmac ~ '^[0-9a-f]{64}$'", name="ck_memory_fact_evidence_hmac"),
        CheckConstraint("trust_class IN ('direct', 'derived', 'untrusted')", name="ck_memory_fact_evidence_trust"),
        CheckConstraint(
            "(source_erased_at IS NULL AND thread_id IS NOT NULL AND run_id IS NOT NULL "
            "AND run_event_sequence IS NOT NULL AND (source_candidate_id IS NOT NULL OR source_item_id IS NOT NULL)) OR "
            "(source_erased_at IS NOT NULL AND evidence_excerpt IS NULL AND source_candidate_id IS NULL "
            "AND source_item_id IS NULL AND thread_id IS NULL AND run_id IS NULL AND run_event_sequence IS NULL)",
            name="ck_memory_fact_evidence_source_state",
        ),
        CheckConstraint("evidence_excerpt IS NULL OR char_length(evidence_excerpt) <= 4000", name="ck_memory_fact_evidence_excerpt_size"),
    )


class MemoryContextSummaryRow(Base):
    __tablename__ = "memory_context_summaries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    summary_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fact_revision_ceiling: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_revision_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    summary_text: Mapped[str | None] = mapped_column(Text)
    content_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        *_memory_scope_constraints("memory_context_summaries"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "id", name="uq_memory_context_summaries_scope"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "id",
            "summary_revision",
            name="uq_memory_context_summaries_exact_revision",
        ),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "summary_revision", name="uq_memory_context_summaries_revision"),
        CheckConstraint("summary_revision >= 1 AND fact_revision_ceiling >= 0", name="ck_memory_context_summaries_revisions"),
        CheckConstraint("jsonb_typeof(source_revision_ids) = 'array'", name="ck_memory_context_summaries_sources"),
        CheckConstraint("content_digest ~ '^[0-9a-f]{64}$'", name="ck_memory_context_summaries_digest"),
        CheckConstraint("renderer_version <> '' AND prompt_version <> ''", name="ck_memory_context_summaries_versions"),
        CheckConstraint("policy_revision >= 1", name="ck_memory_context_summaries_policy"),
        CheckConstraint(
            "(summary_text IS NOT NULL AND content_erased_at IS NULL) OR (summary_text IS NULL AND content_erased_at IS NOT NULL)",
            name="ck_memory_context_summaries_content",
        ),
        CheckConstraint("summary_text IS NULL OR char_length(summary_text) <= 64000", name="ck_memory_context_summaries_content_size"),
    )


class MemorySuppressionRow(Base):
    __tablename__ = "memory_suppressions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    suppression_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    identity_hmac: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    hmac_key_version: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        *_memory_scope_constraints("memory_suppressions"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "id", name="uq_memory_suppressions_scope"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "namespace",
            "suppression_kind",
            "identity_hmac",
            "hmac_key_version",
            name="uq_memory_suppressions_identity",
        ),
        CheckConstraint("suppression_kind IN ('source', 'fact_lineage')", name="ck_memory_suppressions_kind"),
        CheckConstraint("identity_hmac ~ '^[0-9a-f]{64}$'", name="ck_memory_suppressions_hmac"),
        CheckConstraint("hmac_key_version <> '' AND reason <> ''", name="ck_memory_suppressions_values"),
    )


class RunMemoryContextSnapshotRow(Base):
    __tablename__ = "run_memory_context_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    fact_revision_ceiling: Mapped[int] = mapped_column(BigInteger, nullable=False)
    summary_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    summary_revision: Mapped[int | None] = mapped_column(BigInteger)
    selection_version: Mapped[str] = mapped_column(String(64), nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    rendered_content: Mapped[str | None] = mapped_column(Text)
    rendered_content_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    content_erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        *_memory_scope_constraints("run_memory_context_snapshots"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "id", name="uq_run_memory_context_snapshots_scope"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "run_id", name="uq_run_memory_context_snapshots_run"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_run_memory_context_snapshots_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "summary_id", "summary_revision"],
            [
                "memory_context_summaries.project_id",
                "memory_context_summaries.owner_user_id",
                "memory_context_summaries.namespace",
                "memory_context_summaries.id",
                "memory_context_summaries.summary_revision",
            ],
            name="fk_run_memory_context_snapshots_summary",
            ondelete="RESTRICT",
        ),
        CheckConstraint("pipeline_mode IN ('off', 'shadow', 'consolidate', 'v2')", name="ck_run_memory_context_snapshots_mode"),
        CheckConstraint("fact_revision_ceiling >= 0", name="ck_run_memory_context_snapshots_ceiling"),
        CheckConstraint("(summary_id IS NULL) = (summary_revision IS NULL)", name="ck_run_memory_context_snapshots_summary"),
        CheckConstraint("summary_revision IS NULL OR summary_revision >= 1", name="ck_run_memory_context_snapshots_summary_revision"),
        CheckConstraint("selection_version <> '' AND renderer_version <> '' AND prompt_version <> ''", name="ck_run_memory_context_snapshots_versions"),
        CheckConstraint("policy_revision >= 1 AND token_budget >= 0", name="ck_run_memory_context_snapshots_policy_budget"),
        CheckConstraint("rendered_content_digest ~ '^[0-9a-f]{64}$'", name="ck_run_memory_context_snapshots_digest"),
        CheckConstraint(
            "(rendered_content IS NOT NULL AND content_erased_at IS NULL) OR (rendered_content IS NULL AND content_erased_at IS NOT NULL)",
            name="ck_run_memory_context_snapshots_content",
        ),
        CheckConstraint("rendered_content IS NULL OR char_length(rendered_content) <= 128000", name="ck_run_memory_context_snapshots_content_size"),
    )


class RunMemoryContextItemRow(Base):
    __tablename__ = "run_memory_context_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    fact_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    rank_score: Mapped[float] = mapped_column(Float, nullable=False)
    selection_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    content_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        *_memory_scope_constraints("run_memory_context_items"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "id", name="uq_run_memory_context_items_scope"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "snapshot_id", "ordinal", name="uq_run_memory_context_items_order"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", "snapshot_id", "fact_id", name="uq_run_memory_context_items_fact"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "snapshot_id"],
            [
                "run_memory_context_snapshots.project_id",
                "run_memory_context_snapshots.owner_user_id",
                "run_memory_context_snapshots.namespace",
                "run_memory_context_snapshots.id",
            ],
            name="fk_run_memory_context_items_snapshot",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "namespace", "fact_id", "revision_id", "content_digest"],
            [
                "memory_fact_revisions.project_id",
                "memory_fact_revisions.owner_user_id",
                "memory_fact_revisions.namespace",
                "memory_fact_revisions.fact_id",
                "memory_fact_revisions.id",
                "memory_fact_revisions.content_digest",
            ],
            name="fk_run_memory_context_items_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="ck_run_memory_context_items_ordinal"),
        CheckConstraint("rank_score >= 0 AND rank_score <= 1", name="ck_run_memory_context_items_score"),
        CheckConstraint("selection_reason <> ''", name="ck_run_memory_context_items_reason"),
        CheckConstraint("content_digest ~ '^[0-9a-f]{64}$'", name="ck_run_memory_context_items_digest"),
    )


__all__ = [
    "MemoryCandidateRow",
    "MemoryConsolidationGenerationRow",
    "MemoryContextSummaryRow",
    "MemoryExtractionGenerationRow",
    "MemoryFactEvidenceRow",
    "MemoryFactRevisionRow",
    "MemoryFactRow",
    "MemorySourceBatchRow",
    "MemorySourceItemRow",
    "MemorySuppressionRow",
    "RunMemoryContextItemRow",
    "RunMemoryContextSnapshotRow",
]
