from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
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


class SkillDesignSessionRow(Base):
    """Private owner-scoped conversational Skill package design state."""

    __tablename__ = "skill_design_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "projects.id",
            name="fk_skill_design_sessions_project",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    owner_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "users.id",
            name="fk_skill_design_sessions_owner",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="interviewing",
        server_default="interviewing",
    )
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    messages_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    progress_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    active_clarification_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    draft_checksum: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    validation_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    validated_draft_checksum: Mapped[str | None] = mapped_column(
        CHAR(64),
        nullable=True,
    )
    skill_creator_skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    skill_creator_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    skill_creator_payload_checksum: Mapped[str] = mapped_column(
        CHAR(64),
        nullable=False,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_skill_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_skill_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
    )
    created_skill_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    create_idempotency_key_hash: Mapped[str] = mapped_column(
        CHAR(64),
        nullable=False,
    )
    create_request_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
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
        onupdate=_now,
        server_default=text("now()"),
    )
    authoring_dependencies_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    session_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="create",
        server_default="create",
    )
    target_skill_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    base_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    base_version_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    base_payload_checksum: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    target_skill_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    execution_model_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)
    execution_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    execution_thinking_enabled: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )
    execution_reasoning_effort: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_skill_design_sessions"),
        CheckConstraint(
            "status IN ('interviewing', 'generating', 'awaiting_clarification', 'draft_ready', 'validated', 'committing', 'completed', 'failed', 'cancelled')",
            name="ck_skill_design_sessions_status",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_skill_design_sessions_revision",
        ),
        CheckConstraint(
            "skill_creator_payload_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_skill_design_sessions_creator_checksum",
        ),
        CheckConstraint(
            "draft_checksum IS NULL OR draft_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_skill_design_sessions_draft_checksum",
        ),
        CheckConstraint(
            "validated_draft_checksum IS NULL OR validated_draft_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_skill_design_sessions_validated_checksum",
        ),
        CheckConstraint(
            "(validation_json IS NULL AND validated_draft_checksum IS NULL) OR (validation_json IS NOT NULL AND validated_draft_checksum IS NOT NULL)",
            name="ck_skill_design_sessions_validation_pair",
        ),
        CheckConstraint(
            "(status IN ('validated', 'committing', 'completed') AND draft_checksum IS NOT NULL AND validation_json IS NOT NULL AND validated_draft_checksum = draft_checksum) OR status NOT IN ('validated', 'committing', 'completed')",
            name="ck_skill_design_sessions_validated_state",
        ),
        CheckConstraint(
            "(status IN ('draft_ready', 'validated', 'committing', 'completed') AND draft_checksum IS NOT NULL) OR status NOT IN ('draft_ready', 'validated', 'committing', 'completed')",
            name="ck_skill_design_sessions_draft_state",
        ),
        CheckConstraint(
            "(status = 'awaiting_clarification' AND active_clarification_json IS NOT NULL) OR (status <> 'awaiting_clarification' AND active_clarification_json IS NULL)",
            name="ck_skill_design_sessions_clarification",
        ),
        CheckConstraint(
            "authoring_dependencies_json IS NULL OR ("
            "jsonb_typeof(authoring_dependencies_json) = 'object' AND "
            "authoring_dependencies_json ->> 'version' = '1' AND "
            "(authoring_dependencies_json ->> 'draft_checksum') "
            "~ '^[0-9a-f]{64}$' AND "
            "CASE WHEN jsonb_typeof("
            "authoring_dependencies_json -> 'requirements') = 'array' "
            "THEN jsonb_array_length("
            "authoring_dependencies_json -> 'requirements') <= 64 "
            "ELSE FALSE END)",
            name="ck_skill_design_sessions_authoring_dependencies",
        ),
        CheckConstraint(
            "(status = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL) OR (status <> 'failed' AND error_code IS NULL AND error_message IS NULL)",
            name="ck_skill_design_sessions_error",
        ),
        CheckConstraint(
            "(status = 'completed' AND ("
            "(created_skill_deleted IS FALSE AND created_skill_id IS NOT NULL "
            "AND created_skill_version_id IS NOT NULL) OR "
            "(created_skill_deleted IS TRUE AND created_skill_id IS NULL "
            "AND created_skill_version_id IS NULL))) OR "
            "(status <> 'completed' AND created_skill_deleted IS FALSE "
            "AND created_skill_id IS NULL AND created_skill_version_id IS NULL)",
            name="ck_skill_design_sessions_completion",
        ),
        CheckConstraint(
            "session_kind IN ('create', 'revise')",
            name="ck_skill_design_sessions_kind",
        ),
        CheckConstraint(
            "base_payload_checksum IS NULL OR base_payload_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_skill_design_sessions_base_checksum",
        ),
        CheckConstraint(
            "base_version_number IS NULL OR base_version_number >= 1",
            name="ck_skill_design_sessions_base_version_number",
        ),
        CheckConstraint(
            "(execution_model_ref IS NULL AND execution_mode IS NULL "
            "AND execution_thinking_enabled IS NULL "
            "AND execution_reasoning_effort IS NULL) OR "
            "(execution_model_ref IS NOT NULL "
            "AND execution_mode IN ('flash', 'thinking', 'pro', 'ultra') "
            "AND execution_thinking_enabled IS NOT NULL "
            "AND (execution_reasoning_effort IS NULL OR "
            "execution_reasoning_effort IN ('none', 'low', 'medium', 'high')))",
            name="ck_skill_design_sessions_execution_preference",
        ),
        CheckConstraint(
            "(session_kind = 'create' AND target_skill_id IS NULL "
            "AND base_version_id IS NULL AND base_version_number IS NULL "
            "AND base_payload_checksum IS NULL "
            "AND target_skill_deleted IS FALSE) OR "
            "(session_kind = 'revise' AND ("
            "(target_skill_deleted IS FALSE AND target_skill_id IS NOT NULL "
            "AND base_version_id IS NOT NULL AND base_version_number IS NOT NULL "
            "AND base_payload_checksum IS NOT NULL) OR "
            "(target_skill_deleted IS TRUE AND target_skill_id IS NULL "
            "AND base_version_id IS NULL AND base_version_number IS NULL "
            "AND base_payload_checksum IS NULL)))",
            name="ck_skill_design_sessions_revision_target",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "id",
            name="uq_skill_design_sessions_private_scope",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "thread_id",
            name="uq_skill_design_sessions_thread_scope",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "create_idempotency_key_hash",
            name="uq_skill_design_sessions_create_idempotency",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_skill_design_sessions_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["skill_creator_skill_id", "skill_creator_version_id"],
            ["skill_versions.skill_id", "skill_versions.id"],
            name="fk_skill_design_sessions_skill_creator_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "created_skill_id"],
            ["skills.project_id", "skills.id"],
            name="fk_skill_design_sessions_created_skill_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_skill_id", "created_skill_version_id"],
            ["skill_versions.skill_id", "skill_versions.id"],
            name="fk_skill_design_sessions_created_skill_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "target_skill_id"],
            ["skills.project_id", "skills.id"],
            name="fk_skill_design_sessions_target_skill_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_skill_id", "base_version_id"],
            ["skill_versions.skill_id", "skill_versions.id"],
            name="fk_skill_design_sessions_base_version",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_skill_design_sessions_resume",
            "project_id",
            "owner_user_id",
            "status",
            updated_at.desc(),
            id.desc(),
        ),
        Index(
            "uq_skill_design_sessions_live_revise_target",
            "project_id",
            "owner_user_id",
            "target_skill_id",
            unique=True,
            postgresql_where=text("session_kind = 'revise' AND target_skill_id IS NOT NULL AND status NOT IN ('completed', 'cancelled')"),
        ),
    )


class SkillDesignOperationRow(Base):
    """Durable idempotency record for Skill Builder mutations."""

    __tablename__ = "skill_design_operations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "projects.id",
            name="fk_skill_design_operations_project",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    owner_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "users.id",
            name="fk_skill_design_operations_owner",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    request_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="in_progress",
        server_default="in_progress",
    )
    result_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    public_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
        onupdate=_now,
        server_default=text("now()"),
    )
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    terminal_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    terminal_request_checksum: Mapped[str | None] = mapped_column(
        CHAR(64),
        nullable=True,
    )
    stop_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_skill_design_operations"),
        CheckConstraint(
            "operation_kind IN ('turn', 'validate', 'commit', 'cancel')",
            name="ck_skill_design_operations_kind",
        ),
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'failed', 'stopped')",
            name="ck_skill_design_operations_status",
        ),
        CheckConstraint(
            "result_revision IS NULL OR result_revision >= 1",
            name="ck_skill_design_operations_result_revision",
        ),
        CheckConstraint(
            "terminal_kind IS NULL OR terminal_kind IN ('clarification', 'candidate')",
            name="ck_skill_design_operations_terminal_kind",
        ),
        CheckConstraint(
            "terminal_request_checksum IS NULL OR terminal_request_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_skill_design_operations_terminal_checksum",
        ),
        CheckConstraint(
            "(terminal_kind IS NULL AND terminal_request_checksum IS NULL) OR (terminal_kind IS NOT NULL AND terminal_request_checksum IS NOT NULL)",
            name="ck_skill_design_operations_terminal_pair",
        ),
        CheckConstraint(
            "(status = 'in_progress' AND result_revision IS NULL "
            "AND public_error_code IS NULL) OR "
            "(status = 'completed' AND result_revision IS NOT NULL "
            "AND public_error_code IS NULL) OR "
            "(status = 'failed' AND result_revision IS NOT NULL "
            "AND public_error_code IS NOT NULL) OR "
            "(status = 'stopped' AND result_revision IS NOT NULL "
            "AND public_error_code IS NULL)",
            name="ck_skill_design_operations_completion",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "session_id"],
            [
                "skill_design_sessions.project_id",
                "skill_design_sessions.owner_user_id",
                "skill_design_sessions.id",
            ],
            name="fk_skill_design_operations_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.run_id"],
            name="fk_skill_design_operations_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "session_id",
            "id",
            name="uq_skill_design_operations_private_scope",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "operation_kind",
            "idempotency_key_hash",
            name="uq_skill_design_operations_idempotency",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "run_id",
            name="uq_skill_design_operations_run",
        ),
        Index(
            "ix_skill_design_operations_session",
            "project_id",
            "owner_user_id",
            "session_id",
            created_at.desc(),
        ),
    )


class SkillDesignDraftFileRow(Base):
    """Candidate file bytes scoped through their owning Builder session."""

    __tablename__ = "skill_design_draft_files"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        onupdate=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "project_id",
            "owner_user_id",
            "session_id",
            "path",
            name="pk_skill_design_draft_files",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "session_id"],
            [
                "skill_design_sessions.project_id",
                "skill_design_sessions.owner_user_id",
                "skill_design_sessions.id",
            ],
            name="fk_skill_design_draft_files_session",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "path <> '' AND path !~ '(^/|(^|/)\\.\\.(/|$))'",
            name="ck_skill_design_draft_files_safe_path",
        ),
        CheckConstraint(
            "size_bytes >= 0 AND size_bytes <= 104857600",
            name="ck_skill_design_draft_files_size",
        ),
        CheckConstraint(
            "size_bytes = octet_length(content)",
            name="ck_skill_design_draft_files_content_size",
        ),
        CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_skill_design_draft_files_sha256",
        ),
    )


class SkillDesignActivityRow(Base):
    """Append-only, public-safe Skill Builder activity event."""

    __tablename__ = "skill_design_activities"

    seq: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        PrimaryKeyConstraint("seq", name="pk_skill_design_activities"),
        CheckConstraint(
            "attempt IS NULL OR attempt >= 1",
            name="ck_skill_design_activities_attempt",
        ),
        CheckConstraint(
            "kind IN ('request_accepted', 'attempt_started', "
            "'reasoning', 'tool_started', 'tool_completed', 'tool_failed', "
            "'candidate_generated', 'validation_started', "
            "'validation_passed', 'validation_failed', 'repair_started', "
            "'run_terminal', 'commit_accepted', "
            "'commit_validation_started', 'commit_validation_passed', "
            "'commit_persistence_started', 'commit_persistence_completed', "
            "'commit_terminal')",
            name="ck_skill_design_activities_kind",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "session_id"],
            [
                "skill_design_sessions.project_id",
                "skill_design_sessions.owner_user_id",
                "skill_design_sessions.id",
            ],
            name="fk_skill_design_activities_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "session_id", "operation_id"],
            [
                "skill_design_operations.project_id",
                "skill_design_operations.owner_user_id",
                "skill_design_operations.session_id",
                "skill_design_operations.id",
            ],
            name="fk_skill_design_activities_operation",
            ondelete="CASCADE",
        ),
        Index(
            "ix_skill_design_activities_session_seq",
            "project_id",
            "owner_user_id",
            "session_id",
            "seq",
        ),
        Index(
            "uq_skill_design_activities_source_event",
            "operation_id",
            "source_event_id",
            unique=True,
            postgresql_where=text("source_event_id IS NOT NULL"),
        ),
        Index(
            "uq_skill_design_activities_terminal",
            "operation_id",
            unique=True,
            postgresql_where=text("kind IN ('run_terminal', 'commit_terminal')"),
        ),
    )


class SkillDesignOperationBaselineFileRow(Base):
    """Exact pre-Run candidate snapshot used for stop/failure rollback."""

    __tablename__ = "skill_design_operation_baseline_files"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
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
            "session_id",
            "operation_id",
            "path",
            name="pk_skill_design_operation_baseline_files",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "session_id", "operation_id"],
            [
                "skill_design_operations.project_id",
                "skill_design_operations.owner_user_id",
                "skill_design_operations.session_id",
                "skill_design_operations.id",
            ],
            name="fk_skill_design_operation_baseline_files_operation",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "path <> '' AND path !~ '(^/|(^|/)\\.\\.(/|$))'",
            name="ck_skill_design_operation_baseline_files_safe_path",
        ),
        CheckConstraint(
            "size_bytes >= 0 AND size_bytes <= 2097152",
            name="ck_skill_design_operation_baseline_files_size",
        ),
        CheckConstraint(
            "size_bytes = octet_length(content)",
            name="ck_skill_design_operation_baseline_files_content_size",
        ),
        CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_skill_design_operation_baseline_files_sha256",
        ),
    )


__all__ = [
    "SkillDesignActivityRow",
    "SkillDesignDraftFileRow",
    "SkillDesignOperationBaselineFileRow",
    "SkillDesignOperationRow",
    "SkillDesignSessionRow",
]
