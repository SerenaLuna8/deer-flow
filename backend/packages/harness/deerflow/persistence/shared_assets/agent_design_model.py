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


class AgentDesignSessionRow(Base):
    """Private, owner-scoped conversational Agent design state."""

    __tablename__ = "agent_design_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "projects.id",
            name="fk_agent_design_sessions_project",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    owner_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "users.id",
            name="fk_agent_design_sessions_owner",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="interviewing", server_default="interviewing")
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    messages_json: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    progress_json: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    # The database checks distinguish SQL NULL from a JSON `null` literal.
    # Persist Python None as SQL NULL for nullable builder state.
    active_clarification_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    blueprint_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    blueprint_checksum: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_agent_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    create_idempotency_key_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    create_request_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        onupdate=_now,
        server_default=text("now()"),
    )
    created_agent_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    generation_model_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)
    generation_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_agent_design_sessions"),
        CheckConstraint(
            "status IN ('interviewing', 'generating', 'awaiting_clarification', 'proposal_ready', 'committing', 'completed', 'failed', 'cancelled')",
            name="ck_agent_design_sessions_status",
        ),
        CheckConstraint("revision >= 1", name="ck_agent_design_sessions_revision"),
        CheckConstraint(
            "(blueprint_json IS NULL AND blueprint_checksum IS NULL) OR (blueprint_json IS NOT NULL AND blueprint_checksum IS NOT NULL)",
            name="ck_agent_design_sessions_blueprint",
        ),
        CheckConstraint(
            "(status = 'completed' AND ("
            "(created_agent_deleted IS FALSE AND created_agent_id IS NOT NULL "
            "AND created_agent_version_id IS NOT NULL) OR "
            "(created_agent_deleted IS TRUE AND created_agent_id IS NULL "
            "AND created_agent_version_id IS NULL))) OR "
            "(status <> 'completed' AND created_agent_deleted IS FALSE "
            "AND created_agent_id IS NULL AND created_agent_version_id IS NULL)",
            name="ck_agent_design_sessions_completion",
        ),
        CheckConstraint(
            "(status IN ('proposal_ready', 'committing', 'completed') AND blueprint_json IS NOT NULL AND blueprint_checksum IS NOT NULL) OR status NOT IN ('proposal_ready', 'committing', 'completed')",
            name="ck_agent_design_sessions_ready_blueprint",
        ),
        CheckConstraint(
            "(status = 'awaiting_clarification' AND active_clarification_json IS NOT NULL) OR (status <> 'awaiting_clarification' AND active_clarification_json IS NULL)",
            name="ck_agent_design_sessions_clarification",
        ),
        CheckConstraint(
            "(status = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL) OR (status <> 'failed' AND error_code IS NULL AND error_message IS NULL)",
            name="ck_agent_design_sessions_error",
        ),
        CheckConstraint(
            "(generation_model_ref IS NULL AND generation_mode IS NULL) OR (generation_model_ref IS NOT NULL AND generation_mode IN ('flash', 'thinking', 'pro', 'ultra'))",
            name="ck_agent_design_sessions_generation_preference",
        ),
        UniqueConstraint("project_id", "owner_user_id", "id", name="uq_agent_design_sessions_private_scope"),
        UniqueConstraint("project_id", "owner_user_id", "thread_id", name="uq_agent_design_sessions_thread_scope"),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "create_idempotency_key_hash",
            name="uq_agent_design_sessions_create_idempotency",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_agent_design_sessions_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "created_agent_id"],
            ["agents.project_id", "agents.id"],
            name="fk_agent_design_sessions_created_agent_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_agent_id", "created_agent_version_id"],
            ["agent_versions.agent_id", "agent_versions.id"],
            name="fk_agent_design_sessions_created_agent_version",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_agent_design_sessions_resume",
            "project_id",
            "owner_user_id",
            created_at.desc(),
            id.desc(),
            postgresql_where=text(
                "status NOT IN ('completed', 'cancelled')",
            ),
        ),
    )


class AgentDesignOperationRow(Base):
    """Durable idempotency and recovery record for builder mutations."""

    __tablename__ = "agent_design_operations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "projects.id",
            name="fk_agent_design_operations_project",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    owner_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "users.id",
            name="fk_agent_design_operations_owner",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    request_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="in_progress", server_default="in_progress")
    result_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    public_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        onupdate=_now,
        server_default=text("now()"),
    )
    stop_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    requested_generation_profile_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    effective_generation_profile_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_agent_design_operations"),
        CheckConstraint("operation_kind IN ('turn', 'commit', 'cancel')", name="ck_agent_design_operations_kind"),
        CheckConstraint("status IN ('in_progress', 'completed', 'failed', 'stopped')", name="ck_agent_design_operations_status"),
        CheckConstraint("result_revision IS NULL OR result_revision >= 1", name="ck_agent_design_operations_result_revision"),
        CheckConstraint(
            "(status = 'in_progress' AND result_revision IS NULL AND public_error_code IS NULL) "
            "OR (status = 'completed' AND result_revision IS NOT NULL AND public_error_code IS NULL) "
            "OR (status = 'failed' AND result_revision IS NOT NULL AND public_error_code IS NOT NULL) "
            "OR (status = 'stopped' AND result_revision IS NOT NULL AND public_error_code IS NULL)",
            name="ck_agent_design_operations_completion",
        ),
        CheckConstraint(
            "(requested_generation_profile_json IS NULL AND effective_generation_profile_json IS NULL) OR (operation_kind = 'turn' AND requested_generation_profile_json IS NOT NULL AND effective_generation_profile_json IS NOT NULL)",
            name="ck_agent_design_operations_generation_profile",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "session_id"],
            [
                "agent_design_sessions.project_id",
                "agent_design_sessions.owner_user_id",
                "agent_design_sessions.id",
            ],
            name="fk_agent_design_operations_session",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "session_id",
            "id",
            name="uq_agent_design_operations_private_scope",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "operation_kind",
            "idempotency_key_hash",
            name="uq_agent_design_operations_idempotency",
        ),
        Index(
            "ix_agent_design_operations_session",
            "project_id",
            "owner_user_id",
            "session_id",
            created_at.desc(),
        ),
    )


class AgentDesignActivityRow(Base):
    """Append-only, public-safe Builder activity event."""

    __tablename__ = "agent_design_activities"

    seq: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    attempt: Mapped[int | None] = mapped_column(nullable=True)
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
        PrimaryKeyConstraint("seq", name="pk_agent_design_activities"),
        CheckConstraint(
            "attempt IS NULL OR attempt IN (1, 2)",
            name="ck_agent_design_activities_attempt",
        ),
        CheckConstraint(
            "kind IN ('turn_accepted', 'attempt_started', 'reasoning', "
            "'candidate_generated', 'validation_started', 'validation_passed', "
            "'validation_failed', 'repair_started', 'turn_terminal', "
            "'commit_accepted', 'commit_validation_started', "
            "'commit_validation_passed', 'commit_persistence_started', "
            "'commit_persistence_completed', 'commit_terminal')",
            name="ck_agent_design_activities_kind",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "session_id"],
            [
                "agent_design_sessions.project_id",
                "agent_design_sessions.owner_user_id",
                "agent_design_sessions.id",
            ],
            name="fk_agent_design_activities_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "session_id", "operation_id"],
            [
                "agent_design_operations.project_id",
                "agent_design_operations.owner_user_id",
                "agent_design_operations.session_id",
                "agent_design_operations.id",
            ],
            name="fk_agent_design_activities_operation",
            ondelete="CASCADE",
        ),
        Index(
            "ix_agent_design_activities_session_seq",
            "project_id",
            "owner_user_id",
            "session_id",
            "seq",
        ),
        Index(
            "uq_agent_design_activities_terminal",
            "operation_id",
            unique=True,
            postgresql_where=text("kind IN ('turn_terminal', 'commit_terminal')"),
        ),
    )


__all__ = [
    "AgentDesignActivityRow",
    "AgentDesignOperationRow",
    "AgentDesignSessionRow",
]
