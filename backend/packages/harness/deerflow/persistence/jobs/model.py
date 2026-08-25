from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(36))
    owner_private_generation: Mapped[int | None] = mapped_column(BigInteger)
    retention_resource_kind: Mapped[str | None] = mapped_column(String(16))
    retention_effective_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    retention_membership_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    namespace: Mapped[str | None] = mapped_column(String(255))
    run_id: Mapped[str | None] = mapped_column(String(64))
    automation_occurrence_id: Mapped[str | None] = mapped_column(String(64))
    predecessor_dead_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    origin_trace_id: Mapped[str | None] = mapped_column(String(512))
    idempotency_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", server_default="queued")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_token_hash: Mapped[str | None] = mapped_column(CHAR(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_safety: Mapped[str] = mapped_column(String(16), nullable=False, default="safe", server_default="safe")
    public_error_code: Mapped[str | None] = mapped_column(String(64))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    execution_domain_affinity: Mapped[str | None] = mapped_column(CHAR(64))

    __table_args__ = (
        UniqueConstraint("job_type", "idempotency_key", name="uq_jobs_type_idempotency"),
        UniqueConstraint(
            "id",
            "project_id",
            "owner_user_id",
            name="uq_jobs_id_project_owner",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            "owner_user_id",
            "run_id",
            name="uq_jobs_id_project_owner_run",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            "owner_user_id",
            "run_id",
            "execution_domain_affinity",
            name="uq_jobs_id_project_owner_run_execution_domain",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            "owner_user_id",
            "namespace",
            name="uq_jobs_id_project_owner_namespace",
        ),
        UniqueConstraint(
            "predecessor_dead_job_id",
            name="uq_jobs_predecessor_dead_job",
        ),
        ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_jobs_project", ondelete="RESTRICT"),
        ForeignKeyConstraint(["lease_owner_id"], ["worker_nodes.id"], name="fk_jobs_lease_worker", ondelete="SET NULL"),
        ForeignKeyConstraint(
            [
                "project_id",
                "owner_user_id",
                "run_id",
                "origin_trace_id",
            ],
            [
                "runs.project_id",
                "runs.owner_user_id",
                "runs.run_id",
                "runs.origin_trace_id",
            ],
            name="fk_jobs_private_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "automation_occurrence_id"],
            ["scheduled_task_runs.project_id", "scheduled_task_runs.owner_user_id", "scheduled_task_runs.id"],
            name="fk_jobs_automation_occurrence",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["predecessor_dead_job_id"],
            ["dead_jobs.job_id"],
            name="fk_jobs_predecessor_dead_job",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "job_type IN ('private_run', 'automation_run', 'retention_purge', 'mcp_discovery', 'memory_dream', 'memory_dream_prepare', 'memory_seal')",
            name="ck_jobs_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled', 'dead')",
            name="ck_jobs_status",
        ),
        CheckConstraint("retry_safety IN ('safe', 'unknown', 'unsafe')", name="ck_jobs_retry_safety"),
        CheckConstraint(
            "execution_domain_affinity IS NULL OR (job_type = 'private_run' AND execution_domain_affinity ~ '^[0-9a-f]{64}$')",
            name="ck_jobs_execution_domain_affinity",
        ),
        CheckConstraint("attempt_count >= 0 AND max_attempts >= 1", name="ck_jobs_attempts"),
        CheckConstraint(
            "owner_private_generation IS NOT NULL AND owner_private_generation >= 1 AND (job_type = 'retention_purge' OR owner_user_id IS NOT NULL)",
            name="ck_jobs_owner_private_generation",
        ),
        CheckConstraint(
            "(job_type = 'retention_purge' AND retention_resource_kind IS NOT NULL AND "
            "retention_resource_kind IN ('project', 'former_owner', 'account') AND retention_effective_at IS NOT NULL AND "
            "((retention_resource_kind = 'project' AND owner_user_id IS NULL AND retention_membership_id IS NULL) OR "
            "(retention_resource_kind = 'former_owner' AND owner_user_id IS NOT NULL AND retention_membership_id IS NOT NULL) OR "
            "(retention_resource_kind = 'account' AND owner_user_id IS NOT NULL AND retention_membership_id IS NULL))) OR "
            "(job_type <> 'retention_purge' AND retention_resource_kind IS NULL AND retention_effective_at IS NULL AND retention_membership_id IS NULL)",
            name="ck_jobs_retention_authority",
        ),
        CheckConstraint(
            "(job_type = 'private_run' AND run_id IS NOT NULL AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NOT NULL) "
            "OR (job_type = 'automation_run' AND run_id IS NOT NULL AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NOT NULL AND origin_trace_id IS NOT NULL) "
            "OR (job_type = 'retention_purge' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) "
            "OR (job_type = 'mcp_discovery' AND owner_user_id IS NOT NULL AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) "
            "OR (job_type = 'memory_dream' AND owner_user_id IS NOT NULL "
            "AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) "
            "OR (job_type = 'memory_dream_prepare' AND owner_user_id IS NOT NULL "
            "AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) "
            "OR (job_type = 'memory_seal' AND owner_user_id IS NOT NULL "
            "AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL)",
            name="ck_jobs_authority_shape",
        ),
        # ``namespace`` is the memory work coordinate: the Memory namespace for
        # memory_dream and the Thread id for memory_seal.
        CheckConstraint(
            "(job_type IN ('memory_dream', 'memory_dream_prepare', 'memory_seal')) = (namespace IS NOT NULL)",
            name="ck_jobs_memory_namespace",
        ),
        Index(
            "uq_jobs_active_memory_seal",
            "project_id",
            "owner_user_id",
            "namespace",
            unique=True,
            postgresql_where=text("job_type = 'memory_seal' AND status IN ('queued', 'leased', 'running', 'retry_wait')"),
        ),
        Index("ix_jobs_claim", "status", "available_at", priority.desc(), "created_at"),
        Index(
            "ix_jobs_execution_domain_claim",
            "execution_domain_affinity",
            "status",
            "available_at",
            priority.desc(),
            "created_at",
            postgresql_where=text("execution_domain_affinity IS NOT NULL"),
        ),
        Index(
            "ix_jobs_active_lease",
            "lease_expires_at",
            "id",
            postgresql_where=text("status IN ('leased', 'running')"),
        ),
        Index("ix_jobs_private_scope", "project_id", "owner_user_id", "created_at"),
    )


class JobAttemptRow(Base):
    __tablename__ = "job_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("worker_nodes.id", ondelete="RESTRICT"), nullable=False)
    lease_token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    execution_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(16))
    public_error_code: Mapped[str | None] = mapped_column(String(64))
    checkpoint_cursor: Mapped[str | None] = mapped_column(String(128))
    stream_cursor: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_number"),
        UniqueConstraint("id", "job_id", name="uq_job_attempts_id_job"),
        CheckConstraint("attempt_number >= 1", name="ck_job_attempts_number"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('succeeded', 'retry', 'cancelled', 'failed', 'lease_lost', 'dead')",
            name="ck_job_attempts_outcome",
        ),
        CheckConstraint("stream_cursor IS NULL OR stream_cursor >= 0", name="ck_job_attempts_stream_cursor"),
        Index("ix_job_attempts_job_started", "job_id", started_at.desc()),
    )


class DeadJobRow(Base):
    __tablename__ = "dead_jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", name="fk_dead_jobs_job", ondelete="RESTRICT"),
        primary_key=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", name="fk_dead_jobs_project", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_ref_key_id: Mapped[str | None] = mapped_column(String(64))
    owner_ref_hmac: Mapped[str | None] = mapped_column(CHAR(64))
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_safety: Mapped[str] = mapped_column(String(16), nullable=False)
    public_error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    dead_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("attempt_count >= 1", name="ck_dead_jobs_attempt_count"),
        CheckConstraint("retry_safety IN ('safe', 'unknown', 'unsafe')", name="ck_dead_jobs_retry_safety"),
        Index("ix_dead_jobs_project_dead", "project_id", dead_at.desc(), "job_id"),
    )


class WorkerNodeRow(Base):
    __tablename__ = "worker_nodes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list, server_default=text("'[]'"))
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_domain_affinity: Mapped[str | None] = mapped_column(CHAR(64))
    draining: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("max_concurrent_jobs >= 1", name="ck_worker_nodes_capacity"),
        CheckConstraint(
            "execution_domain_affinity IS NULL OR execution_domain_affinity ~ '^[0-9a-f]{64}$'",
            name="ck_worker_nodes_execution_domain_affinity",
        ),
        Index("ix_worker_nodes_fresh", "draining", "heartbeat_at"),
        Index(
            "ix_worker_nodes_fresh_affinity",
            "execution_domain_affinity",
            "heartbeat_at",
            postgresql_where=text("draining = false"),
        ),
    )
