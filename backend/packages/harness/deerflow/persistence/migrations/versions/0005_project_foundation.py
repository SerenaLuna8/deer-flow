"""Add project foundation and rename the platform admin role.

Revision ID: 0005_project_foundation
Revises: 0004_migration_ledger
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_project_foundation"
down_revision: str | Sequence[str] | None = "0004_migration_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE users SET system_role = 'system_admin' WHERE system_role = 'admin'")
    # Legacy bootstrap backfills baseline tables from current ORM metadata, so
    # the constraint can already exist even though Alembic is only at 0004.
    op.execute(
        """
        DO $$
        DECLARE
          users_oid oid := to_regclass(format('%I.users', current_schema()));
          existing_type "char";
          actual_expression text;
          expected_expression text;
        BEGIN
          SELECT contype INTO existing_type
          FROM pg_constraint
          WHERE conrelid = users_oid
            AND conname = 'ck_users_system_role';

          IF existing_type IS NULL THEN
            ALTER TABLE users ADD CONSTRAINT ck_users_system_role
              CHECK (system_role IN ('system_admin', 'user'));
          ELSIF existing_type <> 'c' THEN
            RAISE EXCEPTION
              'Cannot upgrade 0005_project_foundation: users role constraint type drift';
          ELSE
            ALTER TABLE users ADD CONSTRAINT ck_users_system_role_contract_probe
              CHECK (system_role IN ('system_admin', 'user')) NOT VALID;

            SELECT pg_get_expr(conbin, conrelid) INTO actual_expression
            FROM pg_constraint
            WHERE conrelid = users_oid
              AND conname = 'ck_users_system_role'
              AND contype = 'c';
            SELECT pg_get_expr(conbin, conrelid) INTO expected_expression
            FROM pg_constraint
            WHERE conrelid = users_oid
              AND conname = 'ck_users_system_role_contract_probe'
              AND contype = 'c';

            IF actual_expression IS DISTINCT FROM expected_expression THEN
              RAISE EXCEPTION
                'Cannot upgrade 0005_project_foundation: users role constraint definition drift';
            END IF;
            ALTER TABLE users DROP CONSTRAINT ck_users_system_role_contract_probe;
          END IF;
        END $$
        """
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500), server_default="", nullable=False),
        sa.Column("icon", sa.String(32), server_default="folder", nullable=False),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        sa.Column("is_suspended", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("membership_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        # Keep compatibility with the existing users VARCHAR(36) primary key.
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("char_length(slug) BETWEEN 3 AND 63", name="ck_projects_slug_length"),
        sa.CheckConstraint("slug = lower(slug)", name="ck_projects_slug_lowercase"),
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="ck_projects_slug_format"),
        sa.CheckConstraint("status = 'active'", name="ck_projects_status"),
        sa.CheckConstraint("membership_version >= 1", name="ck_projects_membership_version"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_projects_slug"),
    )
    op.create_table(
        "project_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("is_pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'editor', 'runner', 'viewer')", name="ck_project_memberships_role"),
        sa.CheckConstraint("status = 'active'", name="ck_project_memberships_status"),
        sa.CheckConstraint("version >= 1", name="ck_project_memberships_version"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_memberships_project_user"),
    )
    op.create_index("ix_project_memberships_user_id", "project_memberships", ["user_id"])


def downgrade() -> None:
    has_project_data = op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM projects LIMIT 1) OR EXISTS (SELECT 1 FROM project_memberships LIMIT 1)")).scalar_one()
    if has_project_data:
        raise RuntimeError("Refusing to downgrade 0005_project_foundation: project data exists")

    op.drop_index("ix_project_memberships_user_id", table_name="project_memberships")
    op.drop_table("project_memberships")
    op.drop_table("projects")
    op.drop_constraint("ck_users_system_role", "users", type_="check")
    op.execute("UPDATE users SET system_role = 'admin' WHERE system_role = 'system_admin'")
