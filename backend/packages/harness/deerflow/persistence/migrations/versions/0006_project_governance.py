"""Add project governance lifecycle and invitation schema.

Revision ID: 0006_project_governance
Revises: 0005_project_foundation
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deerflow.persistence.migrations._helpers import safe_add_column, safe_drop_column

revision: str = "0006_project_governance"
down_revision: str | Sequence[str] | None = "0005_project_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    safe_add_column(
        "projects",
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    safe_add_column(
        "projects",
        sa.Column("deletion_effective_at", sa.DateTime(timezone=True), nullable=True),
    )
    safe_add_column(
        "projects",
        sa.Column("deletion_requested_by_user_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_deletion_requested_by_user_id_users",
        "projects",
        "users",
        ["deletion_requested_by_user_id"],
        ["id"],
    )
    op.drop_constraint("ck_projects_status", "projects", type_="check")
    op.create_check_constraint(
        "ck_projects_status",
        "projects",
        "status IN ('active', 'pending_deletion')",
    )

    safe_add_column(
        "project_memberships",
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    safe_add_column(
        "project_memberships",
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
    )
    safe_add_column(
        "project_memberships",
        sa.Column("ended_by_user_id", sa.String(36), nullable=True),
    )
    safe_add_column(
        "project_memberships",
        sa.Column("end_reason", sa.String(16), nullable=True),
    )
    op.create_foreign_key(
        "fk_project_memberships_ended_by_user_id_users",
        "project_memberships",
        "users",
        ["ended_by_user_id"],
        ["id"],
    )
    op.drop_constraint(
        "ck_project_memberships_status",
        "project_memberships",
        type_="check",
    )
    op.create_check_constraint(
        "ck_project_memberships_status",
        "project_memberships",
        "status IN ('active', 'left', 'removed')",
    )
    op.create_check_constraint(
        "ck_project_memberships_end_reason",
        "project_memberships",
        "end_reason IS NULL OR end_reason IN ('left', 'removed')",
    )

    op.create_table(
        "project_invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("invited_email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("redeemed_by_user_id", sa.String(36), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('editor', 'runner', 'viewer')", name="ck_project_invitations_role"),
        sa.CheckConstraint("status IN ('pending', 'redeemed', 'revoked', 'expired')", name="ck_project_invitations_status"),
        sa.CheckConstraint("version >= 1", name="ck_project_invitations_version"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["redeemed_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_project_invitations_token_hash"),
    )
    op.create_index(
        "uq_project_invitations_pending_email",
        "project_invitations",
        ["project_id", "invited_email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_project_invitations_pending_email",
        table_name="project_invitations",
    )
    op.drop_table("project_invitations")

    op.drop_constraint(
        "ck_project_memberships_end_reason",
        "project_memberships",
        type_="check",
    )
    op.drop_constraint(
        "ck_project_memberships_status",
        "project_memberships",
        type_="check",
    )
    op.create_check_constraint(
        "ck_project_memberships_status",
        "project_memberships",
        "status = 'active'",
    )
    op.drop_constraint(
        "fk_project_memberships_ended_by_user_id_users",
        "project_memberships",
        type_="foreignkey",
    )
    safe_drop_column("project_memberships", "end_reason")
    safe_drop_column("project_memberships", "ended_by_user_id")
    safe_drop_column("project_memberships", "retention_until")
    safe_drop_column("project_memberships", "ended_at")

    op.drop_constraint("ck_projects_status", "projects", type_="check")
    op.create_check_constraint(
        "ck_projects_status",
        "projects",
        "status = 'active'",
    )
    op.drop_constraint(
        "fk_projects_deletion_requested_by_user_id_users",
        "projects",
        type_="foreignkey",
    )
    safe_drop_column("projects", "deletion_requested_by_user_id")
    safe_drop_column("projects", "deletion_effective_at")
    safe_drop_column("projects", "deletion_requested_at")
