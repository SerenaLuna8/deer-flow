"""Add owner-private conversational Skill Builder state.

The frozen 0001 baseline remains byte-for-byte unchanged. This forward-only
revision adds durable candidate packages, idempotent operations, and exact
system skill-creator version pins.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_skill_design_builder"
down_revision = "0001_project_saas_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_skills_project_id_id",
        "skills",
        ["project_id", "id"],
    )

    op.create_table(
        "skill_design_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="interviewing",
            nullable=False,
        ),
        sa.Column(
            "revision",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "messages_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "progress_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "active_clarification_json",
            postgresql.JSONB(astext_type=sa.Text(), none_as_null=True),
            nullable=True,
        ),
        sa.Column("draft_checksum", sa.CHAR(length=64), nullable=True),
        sa.Column(
            "validation_json",
            postgresql.JSONB(astext_type=sa.Text(), none_as_null=True),
            nullable=True,
        ),
        sa.Column(
            "validated_draft_checksum",
            sa.CHAR(length=64),
            nullable=True,
        ),
        sa.Column("skill_creator_skill_id", sa.Uuid(), nullable=False),
        sa.Column("skill_creator_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "skill_creator_payload_checksum",
            sa.CHAR(length=64),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=255), nullable=True),
        sa.Column("created_skill_id", sa.Uuid(), nullable=True),
        sa.Column("created_skill_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_skill_deleted",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "create_idempotency_key_hash",
            sa.CHAR(length=64),
            nullable=False,
        ),
        sa.Column("create_request_checksum", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('interviewing', 'generating', 'awaiting_clarification', 'draft_ready', 'validated', 'committing', 'completed', 'failed', 'cancelled')",
            name="ck_skill_design_sessions_status",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_skill_design_sessions_revision",
        ),
        sa.CheckConstraint(
            "skill_creator_payload_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_skill_design_sessions_creator_checksum",
        ),
        sa.CheckConstraint(
            "draft_checksum IS NULL OR draft_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_skill_design_sessions_draft_checksum",
        ),
        sa.CheckConstraint(
            "validated_draft_checksum IS NULL OR validated_draft_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_skill_design_sessions_validated_checksum",
        ),
        sa.CheckConstraint(
            "(validation_json IS NULL AND validated_draft_checksum IS NULL) OR (validation_json IS NOT NULL AND validated_draft_checksum IS NOT NULL)",
            name="ck_skill_design_sessions_validation_pair",
        ),
        sa.CheckConstraint(
            "(status IN ('validated', 'committing', 'completed') AND draft_checksum IS NOT NULL AND validation_json IS NOT NULL AND validated_draft_checksum = draft_checksum) OR status NOT IN ('validated', 'committing', 'completed')",
            name="ck_skill_design_sessions_validated_state",
        ),
        sa.CheckConstraint(
            "(status IN ('draft_ready', 'validated', 'committing', 'completed') AND draft_checksum IS NOT NULL) OR status NOT IN ('draft_ready', 'validated', 'committing', 'completed')",
            name="ck_skill_design_sessions_draft_state",
        ),
        sa.CheckConstraint(
            "(status = 'awaiting_clarification' AND active_clarification_json IS NOT NULL) OR (status <> 'awaiting_clarification' AND active_clarification_json IS NULL)",
            name="ck_skill_design_sessions_clarification",
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL) OR (status <> 'failed' AND error_code IS NULL AND error_message IS NULL)",
            name="ck_skill_design_sessions_error",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND ("
            "(created_skill_deleted IS FALSE "
            "AND created_skill_id IS NOT NULL "
            "AND created_skill_version_id IS NOT NULL) OR "
            "(created_skill_deleted IS TRUE "
            "AND created_skill_id IS NULL "
            "AND created_skill_version_id IS NULL))) OR "
            "(status <> 'completed' "
            "AND created_skill_deleted IS FALSE "
            "AND created_skill_id IS NULL "
            "AND created_skill_version_id IS NULL)",
            name="ck_skill_design_sessions_completion",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_skill_design_sessions_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_skill_design_sessions_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_skill_design_sessions_membership",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["skill_creator_skill_id", "skill_creator_version_id"],
            ["skill_versions.skill_id", "skill_versions.id"],
            name="fk_skill_design_sessions_skill_creator_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "created_skill_id"],
            ["skills.project_id", "skills.id"],
            name="fk_skill_design_sessions_created_skill_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_skill_id", "created_skill_version_id"],
            ["skill_versions.skill_id", "skill_versions.id"],
            name="fk_skill_design_sessions_created_skill_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_skill_design_sessions"),
        sa.UniqueConstraint(
            "project_id",
            "owner_user_id",
            "id",
            name="uq_skill_design_sessions_private_scope",
        ),
        sa.UniqueConstraint(
            "project_id",
            "owner_user_id",
            "thread_id",
            name="uq_skill_design_sessions_thread_scope",
        ),
        sa.UniqueConstraint(
            "project_id",
            "owner_user_id",
            "create_idempotency_key_hash",
            name="uq_skill_design_sessions_create_idempotency",
        ),
    )
    op.create_index(
        "ix_skill_design_sessions_resume",
        "skill_design_sessions",
        [
            "project_id",
            "owner_user_id",
            "status",
            sa.literal_column("updated_at DESC"),
            sa.literal_column("id DESC"),
        ],
        unique=False,
    )

    op.create_table(
        "skill_design_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("operation_kind", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("request_checksum", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="in_progress",
            nullable=False,
        ),
        sa.Column("result_revision", sa.BigInteger(), nullable=True),
        sa.Column("public_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "operation_kind IN ('turn', 'validate', 'commit', 'cancel')",
            name="ck_skill_design_operations_kind",
        ),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed', 'failed')",
            name="ck_skill_design_operations_status",
        ),
        sa.CheckConstraint(
            "result_revision IS NULL OR result_revision >= 1",
            name="ck_skill_design_operations_result_revision",
        ),
        sa.CheckConstraint(
            "(status = 'in_progress' AND result_revision IS NULL "
            "AND public_error_code IS NULL) OR "
            "(status = 'completed' AND result_revision IS NOT NULL "
            "AND public_error_code IS NULL) OR "
            "(status = 'failed' AND result_revision IS NOT NULL "
            "AND public_error_code IS NOT NULL)",
            name="ck_skill_design_operations_completion",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_skill_design_operations_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_skill_design_operations_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "session_id"],
            [
                "skill_design_sessions.project_id",
                "skill_design_sessions.owner_user_id",
                "skill_design_sessions.id",
            ],
            name="fk_skill_design_operations_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_skill_design_operations"),
        sa.UniqueConstraint(
            "project_id",
            "owner_user_id",
            "operation_kind",
            "idempotency_key_hash",
            name="uq_skill_design_operations_idempotency",
        ),
    )
    op.create_index(
        "ix_skill_design_operations_session",
        "skill_design_operations",
        [
            "project_id",
            "owner_user_id",
            "session_id",
            sa.literal_column("created_at DESC"),
        ],
        unique=False,
    )

    op.create_table(
        "skill_design_draft_files",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "path <> '' AND path !~ '(^/|(^|/)\\.\\.(/|$))'",
            name="ck_skill_design_draft_files_safe_path",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0 AND size_bytes <= 104857600",
            name="ck_skill_design_draft_files_size",
        ),
        sa.CheckConstraint(
            "size_bytes = octet_length(content)",
            name="ck_skill_design_draft_files_content_size",
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_skill_design_draft_files_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "session_id"],
            [
                "skill_design_sessions.project_id",
                "skill_design_sessions.owner_user_id",
                "skill_design_sessions.id",
            ],
            name="fk_skill_design_draft_files_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "owner_user_id",
            "session_id",
            "path",
            name="pk_skill_design_draft_files",
        ),
    )
    op.execute("CREATE TRIGGER trg_skill_design_sessions_updated_at BEFORE UPDATE ON skill_design_sessions FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()")
    op.execute("CREATE TRIGGER trg_skill_design_operations_updated_at BEFORE UPDATE ON skill_design_operations FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()")
    op.execute("CREATE TRIGGER trg_skill_design_draft_files_updated_at BEFORE UPDATE ON skill_design_draft_files FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()")


def downgrade() -> None:
    raise RuntimeError("Skill Builder downgrade is unsupported; restore from a verified backup")
