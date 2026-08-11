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
from sqlalchemy.dialects.postgresql import JSONB
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
    namespace: Mapped[str | None] = mapped_column(String(255))
    run_id: Mapped[str | None] = mapped_column(String(64))
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    workflow_epoch: Mapped[int | None] = mapped_column(BigInteger)
    required_worker_profile_digest: Mapped[str | None] = mapped_column(CHAR(64))
    workflow_profile_key: Mapped[str | None] = mapped_column(CHAR(64))
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
            "namespace",
            name="uq_jobs_id_project_owner_namespace",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            "owner_user_id",
            "workflow_run_id",
            "workflow_epoch",
            name="uq_jobs_workflow_epoch_scope",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            "owner_user_id",
            "workflow_run_id",
            "workflow_epoch",
            "workflow_profile_key",
            name="uq_jobs_workflow_epoch_profile_scope",
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
            ["workflow_run_id", "project_id", "owner_user_id", "origin_trace_id"],
            ["workflow_runs.id", "workflow_runs.project_id", "workflow_runs.owner_user_id", "workflow_runs.origin_trace_id"],
            name="fk_jobs_workflow_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workflow_run_id", "workflow_epoch", "id"],
            [
                "workflow_run_jobs.workflow_run_id",
                "workflow_run_jobs.execution_epoch",
                "workflow_run_jobs.job_id",
            ],
            name="fk_jobs_workflow_run_mapping",
            ondelete="RESTRICT",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
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
            "job_type IN ('private_run', 'automation_run', 'workflow_run', 'retention_purge', 'mcp_discovery', 'memory_dream', 'memory_seal')",
            name="ck_jobs_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled', 'dead')",
            name="ck_jobs_status",
        ),
        CheckConstraint("retry_safety IN ('safe', 'unknown', 'unsafe')", name="ck_jobs_retry_safety"),
        CheckConstraint("attempt_count >= 0 AND max_attempts >= 1", name="ck_jobs_attempts"),
        CheckConstraint(
            "(job_type = 'private_run' AND run_id IS NOT NULL AND workflow_run_id IS NULL "
            "AND workflow_epoch IS NULL AND required_worker_profile_digest IS NULL "
            "AND workflow_profile_key IS NULL AND owner_user_id IS NOT NULL "
            "AND automation_occurrence_id IS NULL AND origin_trace_id IS NOT NULL) "
            "OR (job_type = 'automation_run' AND run_id IS NOT NULL AND workflow_run_id IS NULL "
            "AND workflow_epoch IS NULL AND required_worker_profile_digest IS NULL "
            "AND workflow_profile_key IS NULL AND owner_user_id IS NOT NULL "
            "AND automation_occurrence_id IS NOT NULL AND origin_trace_id IS NOT NULL) "
            "OR (job_type = 'workflow_run' AND run_id IS NULL AND workflow_run_id IS NOT NULL "
            "AND workflow_epoch IS NOT NULL AND workflow_profile_key IS NOT NULL "
            "AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NULL "
            "AND origin_trace_id IS NOT NULL) "
            "OR (job_type = 'retention_purge' AND run_id IS NULL AND workflow_run_id IS NULL "
            "AND workflow_epoch IS NULL AND required_worker_profile_digest IS NULL "
            "AND workflow_profile_key IS NULL AND automation_occurrence_id IS NULL "
            "AND origin_trace_id IS NULL) "
            "OR (job_type = 'mcp_discovery' AND owner_user_id IS NOT NULL AND run_id IS NULL "
            "AND workflow_run_id IS NULL AND workflow_epoch IS NULL "
            "AND required_worker_profile_digest IS NULL AND workflow_profile_key IS NULL "
            "AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) "
            "OR (job_type = 'memory_dream' AND owner_user_id IS NOT NULL "
            "AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL "
            "AND workflow_run_id IS NULL AND workflow_epoch IS NULL "
            "AND required_worker_profile_digest IS NULL AND workflow_profile_key IS NULL "
            "AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) "
            "OR (job_type = 'memory_seal' AND owner_user_id IS NOT NULL "
            "AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL "
            "AND workflow_run_id IS NULL AND workflow_epoch IS NULL "
            "AND required_worker_profile_digest IS NULL AND workflow_profile_key IS NULL "
            "AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL)",
            name="ck_jobs_authority_shape",
        ),
        CheckConstraint(
            "(workflow_run_id IS NULL AND workflow_epoch IS NULL AND required_worker_profile_digest IS NULL AND workflow_profile_key IS NULL) OR "
            "(workflow_run_id IS NOT NULL AND workflow_epoch >= 1 AND workflow_profile_key ~ '^[0-9a-f]{64}$' AND "
            "((required_worker_profile_digest IS NULL AND workflow_profile_key = '0000000000000000000000000000000000000000000000000000000000000000') OR "
            "(required_worker_profile_digest IS NOT NULL AND required_worker_profile_digest ~ '^[0-9a-f]{64}$' "
            "AND required_worker_profile_digest = workflow_profile_key)))",
            name="ck_jobs_workflow_profile",
        ),
        # ``namespace`` is the memory work coordinate: the Memory namespace for
        # memory_dream and the Thread id for memory_seal.
        CheckConstraint(
            "(job_type IN ('memory_dream', 'memory_seal')) = (namespace IS NOT NULL)",
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
            "ix_jobs_active_lease",
            "lease_expires_at",
            "id",
            postgresql_where=text("status IN ('leased', 'running')"),
        ),
        Index("ix_jobs_private_scope", "project_id", "owner_user_id", "created_at"),
        Index(
            "ix_jobs_workflow_claim",
            "status",
            "job_type",
            "required_worker_profile_digest",
            priority.desc(),
            "available_at",
            "created_at",
            "id",
            postgresql_where=text("job_type = 'workflow_run'"),
        ),
    )


class JobAttemptRow(Base):
    __tablename__ = "job_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("worker_nodes.id", ondelete="RESTRICT"), nullable=False)
    lease_token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(16))
    public_error_code: Mapped[str | None] = mapped_column(String(64))
    checkpoint_cursor: Mapped[str | None] = mapped_column(String(128))
    stream_cursor: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_number"),
        UniqueConstraint("id", "job_id", name="uq_job_attempts_id_job"),
        UniqueConstraint(
            "job_id",
            "attempt_number",
            "worker_id",
            name="uq_job_attempts_job_number_worker",
        ),
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
    runtime_profile_digests_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    workflow_runtime_policy_section: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workflow_runtime_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    workflow_runtime_policy_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    workflow_runtime_policy_schema_version: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    workflow_runtime_policy_checksum: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, nullable=False)
    draining: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("max_concurrent_jobs >= 1", name="ck_worker_nodes_capacity"),
        CheckConstraint("workflow_profile_digest_array_is_valid(runtime_profile_digests_json)", name="ck_worker_nodes_runtime_profiles_array"),
        CheckConstraint(
            "(workflow_runtime_policy_section IS NULL "
            "AND workflow_runtime_policy_version_id IS NULL "
            "AND workflow_runtime_policy_revision IS NULL "
            "AND workflow_runtime_policy_schema_version IS NULL "
            "AND workflow_runtime_policy_checksum IS NULL) OR "
            "(workflow_runtime_policy_section IS NOT NULL "
            "AND workflow_runtime_policy_section = 'workflow_runtime' "
            "AND workflow_runtime_policy_version_id IS NOT NULL "
            "AND workflow_runtime_policy_revision IS NOT NULL "
            "AND workflow_runtime_policy_revision >= 1 "
            "AND workflow_runtime_policy_schema_version IS NOT NULL "
            "AND workflow_runtime_policy_schema_version >= 1 "
            "AND workflow_runtime_policy_checksum IS NOT NULL "
            "AND workflow_runtime_policy_checksum ~ '^[0-9a-f]{64}$')",
            name="ck_worker_nodes_workflow_runtime_identity",
        ),
        ForeignKeyConstraint(
            [
                "workflow_runtime_policy_section",
                "workflow_runtime_policy_version_id",
                "workflow_runtime_policy_revision",
                "workflow_runtime_policy_schema_version",
                "workflow_runtime_policy_checksum",
            ],
            [
                "system_runtime_policy_versions.section",
                "system_runtime_policy_versions.id",
                "system_runtime_policy_versions.version_number",
                "system_runtime_policy_versions.schema_version",
                "system_runtime_policy_versions.payload_checksum",
            ],
            name="fk_worker_nodes_workflow_runtime_identity",
            match="FULL",
            ondelete="RESTRICT",
        ),
        Index("ix_worker_nodes_fresh", "draining", "heartbeat_at"),
        Index(
            "ix_worker_nodes_workflow_runtime_identity_fresh",
            "workflow_runtime_policy_section",
            "workflow_runtime_policy_version_id",
            "workflow_runtime_policy_revision",
            "workflow_runtime_policy_schema_version",
            "workflow_runtime_policy_checksum",
            "draining",
            "heartbeat_at",
        ),
    )
