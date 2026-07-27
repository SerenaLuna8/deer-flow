"""Add project-local Skill Credential bindings and Run snapshot references."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_skill_credentials"
down_revision = "0002_skill_design_builder"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_credentials_project_asset_id",
        "credentials",
        ["project_id", "id"],
    )

    op.create_table(
        "project_skill_credential_configs",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "revision",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=False),
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
            "revision >= 1",
            name="ck_project_skill_credential_configs_revision",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_skill_credential_configs_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
            name="fk_project_skill_credential_configs_skill",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id", "skill_version_id"],
            ["skill_versions.skill_id", "skill_versions.id"],
            name="fk_project_skill_credential_configs_skill_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_project_skill_credential_configs_creator",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_project_skill_credential_configs_updater",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "skill_id",
            "skill_version_id",
            name="pk_project_skill_credential_configs",
        ),
        sa.UniqueConstraint(
            "project_id",
            "skill_id",
            "skill_version_id",
            "revision",
            name="uq_project_skill_credential_configs_revision",
        ),
    )
    op.execute("CREATE TRIGGER trg_project_skill_credential_configs_updated_at BEFORE UPDATE ON project_skill_credential_configs FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()")
    op.create_index(
        "ix_project_skill_credential_configs_skill_version",
        "project_skill_credential_configs",
        ["skill_id", "skill_version_id"],
        unique=False,
    )

    op.create_table(
        "project_skill_credential_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("secret_name", sa.String(length=255), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("credential_version_id", sa.Uuid(), nullable=False),
        sa.Column("config_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="active",
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.String(length=36), nullable=True),
        sa.CheckConstraint(
            "secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name="ck_project_skill_credential_bindings_secret_name",
        ),
        sa.CheckConstraint(
            "config_revision >= 1",
            name="ck_project_skill_credential_bindings_revision",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_project_skill_credential_bindings_status",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL AND revoked_by_user_id IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)",
            name="ck_project_skill_credential_bindings_revocation",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "skill_id", "skill_version_id"],
            [
                "project_skill_credential_configs.project_id",
                "project_skill_credential_configs.skill_id",
                "project_skill_credential_configs.skill_version_id",
            ],
            name="fk_project_skill_credential_bindings_config",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id", "skill_version_id"],
            ["skill_versions.skill_id", "skill_versions.id"],
            name="fk_project_skill_credential_bindings_skill_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id", "credential_version_id"],
            ["credential_versions.credential_id", "credential_versions.id"],
            name="fk_project_skill_credential_bindings_credential_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "credential_id"],
            ["credentials.project_id", "credentials.id"],
            name="fk_project_skill_credential_bindings_project_credential",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_project_skill_credential_bindings_creator",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"],
            ["users.id"],
            name="fk_project_skill_credential_bindings_revoker",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_project_skill_credential_bindings",
        ),
        sa.UniqueConstraint(
            "project_id",
            "skill_id",
            "skill_version_id",
            "id",
            name="uq_project_skill_credential_bindings_scope_id",
        ),
    )
    op.create_index(
        "uq_project_skill_credential_bindings_active_name",
        "project_skill_credential_bindings",
        ["project_id", "skill_id", "skill_version_id", "secret_name"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_project_skill_credential_bindings_credential",
        "project_skill_credential_bindings",
        ["credential_id", "credential_version_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_project_skill_credential_bindings_config",
        "project_skill_credential_bindings",
        ["project_id", "skill_id", "skill_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_project_skill_credential_bindings_skill_version",
        "project_skill_credential_bindings",
        ["skill_id", "skill_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_project_skill_credential_bindings_project_credential",
        "project_skill_credential_bindings",
        ["project_id", "credential_id"],
        unique=False,
    )

    op.create_table(
        "run_skill_credential_snapshots",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("secret_name", sa.String(length=255), nullable=False),
        sa.Column("skill_credential_binding_id", sa.Uuid(), nullable=False),
        sa.Column("binding_revision", sa.BigInteger(), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("credential_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name="ck_run_skill_credential_snapshots_secret_name",
        ),
        sa.CheckConstraint(
            "binding_revision >= 1",
            name="ck_run_skill_credential_snapshots_binding_revision",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_run_skill_credential_snapshots_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_run_skill_credential_snapshots_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_run_skill_credential_snapshots_project_membership",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_run_skill_credential_snapshots_private_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "owner_user_id",
            "run_id",
            "skill_version_id",
            "secret_name",
            name="pk_run_skill_credential_snapshots",
        ),
    )
    op.create_index(
        "ix_run_skill_credential_snapshots_binding",
        "run_skill_credential_snapshots",
        ["skill_credential_binding_id"],
        unique=False,
    )
    op.create_index(
        "ix_run_skill_credential_snapshots_private_run",
        "run_skill_credential_snapshots",
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        unique=False,
    )


def downgrade() -> None:
    raise RuntimeError("Skill Credential downgrade is unsupported; restore from a verified backup")
