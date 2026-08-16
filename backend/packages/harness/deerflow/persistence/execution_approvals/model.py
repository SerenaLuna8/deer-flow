"""Durable Local-host command approval requests and result receipts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base

EXECUTION_APPROVAL_STATUSES = frozenset(
    {
        "staged",
        "pending",
        "approved",
        "claimed",
        "finished",
        "launch_failed",
        "unknown",
        "denied",
        "expired",
        "cancelled",
    }
)
EXECUTION_APPROVAL_ACTIVE_STATUSES = frozenset({"staged", "pending", "approved", "claimed"})
EXECUTION_APPROVAL_KINDS = frozenset({"local_bash"})
EXECUTION_APPROVAL_OUTPUT_DELIVERY_MODES = frozenset({"any_one"})
EXECUTION_APPROVAL_OUTPUT_DELIVERY_STATUSES = frozenset(
    {
        "deferred",
        "assigned",
        "intent_recorded",
        "delivered",
        "cancelled",
        "blocked_unknown",
        "failed",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


class ExecutionApprovalRequestRow(Base):
    """Exact private command plan awaiting one-shot approval and execution."""

    __tablename__ = "execution_approval_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_job_attempt_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_agent_path: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    command_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    execution_domain_affinity: Mapped[str] = mapped_column(
        CHAR(64),
        nullable=False,
    )
    command_private_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="staged",
        server_default="staged",
    )
    version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    decision: Mapped[str | None] = mapped_column(String(16))
    decision_idempotency_key: Mapped[str | None] = mapped_column(CHAR(64))
    decision_request_digest: Mapped[str | None] = mapped_column(CHAR(64))
    decided_by_user_id: Mapped[str | None] = mapped_column(String(36))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    continuation_run_id: Mapped[str | None] = mapped_column(String(64))
    continuation_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    execution_job_attempt_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    # This was added by the first post-baseline migration. Keep it last so
    # ORM create_all/fresh SQL and upgraded PostgreSQL catalogs share ordinals.
    spawn_authorized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )

    __table_args__ = (
        UniqueConstraint(
            "id",
            "project_id",
            "owner_user_id",
            "thread_id",
            name="uq_execution_approval_requests_private_scope",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "source_run_id",
            "tool_call_id",
            name="uq_execution_approval_requests_source_tool",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            "owner_user_id",
            "thread_id",
            "continuation_job_id",
            "execution_job_attempt_id",
            name="uq_execution_approval_requests_receipt_scope",
        ),
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_execution_approval_requests_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_execution_approval_requests_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_execution_approval_requests_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id"],
            [
                "threads_meta.project_id",
                "threads_meta.owner_user_id",
                "threads_meta.thread_id",
            ],
            name="fk_execution_approval_requests_private_thread",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "source_run_id"],
            [
                "runs.project_id",
                "runs.owner_user_id",
                "runs.thread_id",
                "runs.run_id",
            ],
            name="fk_execution_approval_requests_source_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_job_id", "project_id", "owner_user_id", "source_run_id"],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id", "jobs.run_id"],
            name="fk_execution_approval_requests_source_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_job_attempt_id", "source_job_id"],
            ["job_attempts.id", "job_attempts.job_id"],
            name="fk_execution_approval_requests_source_attempt",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["users.id"],
            name="fk_execution_approval_requests_decider",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "continuation_run_id"],
            [
                "runs.project_id",
                "runs.owner_user_id",
                "runs.thread_id",
                "runs.run_id",
            ],
            name="fk_execution_approval_requests_continuation_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "continuation_job_id",
                "project_id",
                "owner_user_id",
                "continuation_run_id",
                "execution_domain_affinity",
            ],
            [
                "jobs.id",
                "jobs.project_id",
                "jobs.owner_user_id",
                "jobs.run_id",
                "jobs.execution_domain_affinity",
            ],
            name="fk_execution_approval_requests_continuation_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["execution_job_attempt_id", "continuation_job_id"],
            ["job_attempts.id", "job_attempts.job_id"],
            name="fk_execution_approval_requests_execution_attempt",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('staged', 'pending', 'approved', 'claimed', 'finished', 'launch_failed', 'unknown', 'denied', 'expired', 'cancelled')",
            name="ck_execution_approval_requests_status",
        ),
        CheckConstraint(
            "kind IN ('local_bash')",
            name="ck_execution_approval_requests_kind",
        ),
        CheckConstraint(
            "command_digest ~ '^[0-9a-f]{64}$'",
            name="ck_execution_approval_requests_digest",
        ),
        CheckConstraint(
            "execution_domain_affinity ~ '^[0-9a-f]{64}$'",
            name="ck_execution_approval_requests_execution_domain_affinity",
        ),
        CheckConstraint(
            "json_typeof(command_private_json) = 'object' AND octet_length(command_private_json::text) <= 1048576",
            name="ck_execution_approval_requests_command_json",
        ),
        CheckConstraint(
            "json_typeof(source_agent_path) = 'array' AND json_array_length(source_agent_path) BETWEEN 1 AND 16",
            name="ck_execution_approval_requests_agent_path_json",
        ),
        CheckConstraint(
            "tool_call_id <> '' AND tool_call_id = btrim(tool_call_id)",
            name="ck_execution_approval_requests_tool_call",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_execution_approval_requests_version",
        ),
        CheckConstraint(
            "(decision IS NULL AND decision_idempotency_key IS NULL "
            "AND decision_request_digest IS NULL "
            "AND decided_by_user_id IS NULL AND decided_at IS NULL) "
            "OR (decision IN ('allow_once', 'deny') "
            "AND decision_idempotency_key ~ '^[0-9a-f]{64}$' "
            "AND decision_request_digest ~ '^[0-9a-f]{64}$' "
            "AND decided_by_user_id IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_execution_approval_requests_decision_shape",
        ),
        CheckConstraint(
            "(status IN ('staged', 'pending') AND decision IS NULL) "
            "OR (status = 'denied' AND decision = 'deny') "
            "OR (status IN ('approved', 'claimed', 'finished', "
            "'launch_failed', 'unknown') AND decision = 'allow_once') "
            "OR (status = 'expired' AND "
            "(decision IS NULL OR decision = 'allow_once')) "
            "OR (status = 'cancelled' AND "
            "(decision IS NULL OR decision = 'allow_once'))",
            name="ck_execution_approval_requests_status_decision",
        ),
        CheckConstraint(
            "(continuation_run_id IS NULL) = (continuation_job_id IS NULL) "
            "AND (execution_job_attempt_id IS NULL) = (claimed_at IS NULL) "
            "AND (execution_job_attempt_id IS NULL "
            "OR continuation_job_id IS NOT NULL) "
            "AND (status IN ('staged', 'pending', 'denied') "
            "AND continuation_job_id IS NULL AND execution_job_attempt_id IS NULL "
            "OR status = 'approved' AND execution_job_attempt_id IS NULL "
            "OR status IN ('claimed', 'finished', 'launch_failed', 'unknown') "
            "AND continuation_job_id IS NOT NULL "
            "AND execution_job_attempt_id IS NOT NULL "
            "OR status = 'expired' AND execution_job_attempt_id IS NULL "
            "OR status = 'cancelled')",
            name="ck_execution_approval_requests_execution_shape",
        ),
        CheckConstraint(
            "(status IN ('staged', 'pending', 'approved', 'claimed') AND terminal_at IS NULL) OR (status IN ('finished', 'launch_failed', 'unknown', 'denied', 'expired', 'cancelled') AND terminal_at IS NOT NULL)",
            name="ck_execution_approval_requests_terminal_shape",
        ),
        CheckConstraint(
            "(status != 'finished' OR spawn_authorized_at IS NOT NULL) AND "
            "(spawn_authorized_at IS NULL OR "
            "(status IN ('claimed', 'finished', 'launch_failed', 'unknown', "
            "'cancelled') AND execution_job_attempt_id IS NOT NULL "
            "AND claimed_at IS NOT NULL AND spawn_authorized_at >= claimed_at "
            "AND (terminal_at IS NULL OR spawn_authorized_at <= terminal_at)))",
            name="ck_execution_approval_requests_spawn_authorization",
        ),
        CheckConstraint(
            "expires_at > created_at AND updated_at >= created_at AND (decided_at IS NULL OR decided_at >= created_at) AND (claimed_at IS NULL OR claimed_at >= created_at) AND (terminal_at IS NULL OR terminal_at >= created_at)",
            name="ck_execution_approval_requests_timestamps",
        ),
        Index(
            "uq_execution_approval_requests_active_thread",
            "project_id",
            "owner_user_id",
            "thread_id",
            unique=True,
            postgresql_where=text("status IN ('staged', 'pending', 'approved', 'claimed')"),
        ),
        Index(
            "uq_execution_approval_requests_decision_idempotency",
            "project_id",
            "owner_user_id",
            "decision_idempotency_key",
            unique=True,
            postgresql_where=text("decision_idempotency_key IS NOT NULL"),
        ),
        Index(
            "ix_execution_approval_requests_private_cursor",
            "project_id",
            "owner_user_id",
            "thread_id",
            created_at.desc(),
            id.desc(),
        ),
        Index(
            "ix_execution_approval_requests_status_expiry",
            "status",
            "expires_at",
            "id",
        ),
    )


class ExecutionApprovalOutputDeliveryObligationRow(Base):
    """One private output-delivery obligation bound to an approval."""

    __tablename__ = "execution_approval_output_delivery_obligations"

    approval_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="any_one",
        server_default="any_one",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="deferred",
        server_default="deferred",
    )
    continuation_run_id: Mapped[str | None] = mapped_column(String(64))
    continuation_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    intent_tool_call_id: Mapped[str | None] = mapped_column(String(128))
    intent_digest: Mapped[str | None] = mapped_column(CHAR(64))
    intent_private_json: Mapped[dict | None] = mapped_column(JSON)
    satisfied_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    intent_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint(
            "approval_id",
            "project_id",
            "owner_user_id",
            "thread_id",
            name="uq_ea_output_delivery_obligations_private_scope",
        ),
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_ea_output_delivery_obligations_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_ea_output_delivery_obligations_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_ea_output_delivery_obligations_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id"],
            [
                "threads_meta.project_id",
                "threads_meta.owner_user_id",
                "threads_meta.thread_id",
            ],
            name="fk_ea_output_delivery_obligations_private_thread",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["approval_id", "project_id", "owner_user_id", "thread_id"],
            [
                "execution_approval_requests.id",
                "execution_approval_requests.project_id",
                "execution_approval_requests.owner_user_id",
                "execution_approval_requests.thread_id",
            ],
            name="fk_ea_output_delivery_obligations_approval",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "continuation_run_id"],
            [
                "runs.project_id",
                "runs.owner_user_id",
                "runs.thread_id",
                "runs.run_id",
            ],
            name="fk_ea_output_delivery_obligations_continuation_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "continuation_job_id",
                "project_id",
                "owner_user_id",
                "continuation_run_id",
            ],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id", "jobs.run_id"],
            name="fk_ea_output_delivery_obligations_continuation_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "owner_user_id",
                "thread_id",
                "continuation_run_id",
                "satisfied_artifact_id",
            ],
            [
                "artifacts.project_id",
                "artifacts.owner_user_id",
                "artifacts.thread_id",
                "artifacts.run_id",
                "artifacts.id",
            ],
            name="fk_ea_output_delivery_obligations_satisfied_artifact",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "mode IN ('any_one')",
            name="ck_ea_output_delivery_obligations_mode",
        ),
        CheckConstraint(
            "status IN ('deferred', 'assigned', 'intent_recorded', 'delivered', 'cancelled', 'blocked_unknown', 'failed')",
            name="ck_ea_output_delivery_obligations_status",
        ),
        CheckConstraint(
            "(continuation_run_id IS NULL) = (continuation_job_id IS NULL) AND (continuation_run_id IS NULL) = (assigned_at IS NULL)",
            name="ck_ea_output_delivery_obligations_assignment_shape",
        ),
        CheckConstraint(
            "(intent_tool_call_id IS NULL AND intent_digest IS NULL "
            "AND intent_private_json IS NULL AND intent_recorded_at IS NULL) "
            "OR (intent_tool_call_id IS NOT NULL "
            "AND intent_tool_call_id <> '' "
            "AND intent_tool_call_id = btrim(intent_tool_call_id) "
            "AND intent_digest ~ '^[0-9a-f]{64}$' "
            "AND json_typeof(intent_private_json) = 'object' "
            "AND octet_length(intent_private_json::text) <= 1048576 "
            "AND intent_recorded_at IS NOT NULL)",
            name="ck_ea_output_delivery_obligations_intent_shape",
        ),
        CheckConstraint(
            "(status = 'deferred' AND continuation_run_id IS NULL "
            "AND intent_tool_call_id IS NULL AND satisfied_artifact_id IS NULL "
            "AND terminal_at IS NULL) "
            "OR (status = 'assigned' AND continuation_run_id IS NOT NULL "
            "AND intent_tool_call_id IS NULL AND satisfied_artifact_id IS NULL "
            "AND terminal_at IS NULL) "
            "OR (status = 'intent_recorded' AND continuation_run_id IS NOT NULL "
            "AND intent_tool_call_id IS NOT NULL AND satisfied_artifact_id IS NULL "
            "AND terminal_at IS NULL) "
            "OR (status = 'delivered' AND continuation_run_id IS NOT NULL "
            "AND intent_tool_call_id IS NOT NULL AND satisfied_artifact_id IS NOT NULL "
            "AND terminal_at IS NOT NULL) "
            "OR (status = 'cancelled' AND satisfied_artifact_id IS NULL "
            "AND terminal_at IS NOT NULL) "
            "OR (status IN ('blocked_unknown', 'failed') "
            "AND continuation_run_id IS NOT NULL "
            "AND satisfied_artifact_id IS NULL AND terminal_at IS NOT NULL)",
            name="ck_ea_output_delivery_obligations_lifecycle_shape",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_ea_output_delivery_obligations_version",
        ),
        CheckConstraint(
            "updated_at >= created_at "
            "AND (assigned_at IS NULL OR assigned_at >= created_at) "
            "AND (intent_recorded_at IS NULL OR "
            "intent_recorded_at >= assigned_at) "
            "AND (terminal_at IS NULL OR terminal_at >= "
            "COALESCE(intent_recorded_at, assigned_at, created_at))",
            name="ck_ea_output_delivery_obligations_timestamps",
        ),
        Index(
            "ix_ea_output_delivery_obligations_private_status",
            "project_id",
            "owner_user_id",
            "thread_id",
            "status",
            "updated_at",
        ),
    )


class ExecutionApprovalOutputDeliveryCandidateRow(Base):
    """One exact durable output eligible to satisfy an approval obligation."""

    __tablename__ = "execution_approval_output_delivery_candidates"

    approval_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint(
            "approval_id",
            "logical_path",
            name="uq_ea_output_delivery_candidates_path",
        ),
        ForeignKeyConstraint(
            ["approval_id", "project_id", "owner_user_id", "thread_id"],
            [
                "execution_approval_output_delivery_obligations.approval_id",
                "execution_approval_output_delivery_obligations.project_id",
                "execution_approval_output_delivery_obligations.owner_user_id",
                "execution_approval_output_delivery_obligations.thread_id",
            ],
            name="fk_ea_output_delivery_candidates_obligation",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "file_id"],
            ["files.project_id", "files.owner_user_id", "files.thread_id", "files.id"],
            name="fk_ea_output_delivery_candidates_private_file",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "logical_path LIKE 'outputs/%' AND logical_path <> 'outputs/' AND logical_path !~ '(^|/)\\.\\.(/|$)' AND logical_path !~ '^[A-Za-z]:'",
            name="ck_ea_output_delivery_candidates_path",
        ),
        CheckConstraint(
            "file_version >= 1",
            name="ck_ea_output_delivery_candidates_version",
        ),
        CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_ea_output_delivery_candidates_sha256",
        ),
        Index(
            "ix_ea_output_delivery_candidates_private",
            "project_id",
            "owner_user_id",
            "thread_id",
            "approval_id",
        ),
    )


class ExecutionApprovalResultReceiptRow(Base):
    """Bounded private result from one exact approved execution attempt."""

    __tablename__ = "execution_approval_result_receipts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    approval_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    execution_job_attempt_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        nullable=False,
    )
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    result_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    result_private_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    public_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint(
            "approval_id",
            name="uq_execution_approval_result_receipts_approval",
        ),
        UniqueConstraint(
            "execution_job_attempt_id",
            name="uq_execution_approval_result_receipts_attempt",
        ),
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_execution_approval_result_receipts_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_execution_approval_result_receipts_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_execution_approval_result_receipts_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id"],
            [
                "threads_meta.project_id",
                "threads_meta.owner_user_id",
                "threads_meta.thread_id",
            ],
            name="fk_execution_approval_result_receipts_private_thread",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "approval_id",
                "project_id",
                "owner_user_id",
                "thread_id",
                "execution_job_id",
                "execution_job_attempt_id",
            ],
            [
                "execution_approval_requests.id",
                "execution_approval_requests.project_id",
                "execution_approval_requests.owner_user_id",
                "execution_approval_requests.thread_id",
                "execution_approval_requests.continuation_job_id",
                "execution_approval_requests.execution_job_attempt_id",
            ],
            name="fk_execution_approval_result_receipts_approval_execution",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["execution_job_id", "project_id", "owner_user_id"],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id"],
            name="fk_execution_approval_result_receipts_execution_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["execution_job_attempt_id", "execution_job_id"],
            ["job_attempts.id", "job_attempts.job_id"],
            name="fk_execution_approval_result_receipts_execution_attempt",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "outcome IN ('finished', 'launch_failed')",
            name="ck_execution_approval_result_receipts_outcome",
        ),
        CheckConstraint(
            "result_digest ~ '^[0-9a-f]{64}$'",
            name="ck_execution_approval_result_receipts_digest",
        ),
        CheckConstraint(
            "json_typeof(result_private_json) = 'object' AND octet_length(result_private_json::text) <= 2097152",
            name="ck_execution_approval_result_receipts_result_json",
        ),
        CheckConstraint(
            "(outcome = 'finished' AND exit_code IS NOT NULL) OR (outcome = 'launch_failed' AND exit_code IS NULL AND public_error_code IS NOT NULL)",
            name="ck_execution_approval_result_receipts_result_shape",
        ),
        Index(
            "ix_execution_approval_result_receipts_private_created",
            "project_id",
            "owner_user_id",
            "thread_id",
            created_at.desc(),
            id.desc(),
        ),
    )
