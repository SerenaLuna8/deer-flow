"""Add logical deletion for Credentials."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_credential_soft_delete"
down_revision = "0003_skill_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "credentials",
        sa.Column(
            "is_delete",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )

    op.drop_index(
        "uq_credentials_project_name",
        table_name="credentials",
    )
    op.drop_index(
        "uq_credentials_system_name",
        table_name="credentials",
    )
    op.create_index(
        "uq_credentials_project_name",
        "credentials",
        ["project_id", sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("scope = 'project' AND is_delete = false"),
    )
    op.create_index(
        "uq_credentials_system_name",
        "credentials",
        [sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("scope = 'system' AND is_delete = false"),
    )
    op.create_index(
        "ix_credentials_scope_project_is_delete",
        "credentials",
        ["scope", "project_id", "is_delete"],
        unique=False,
    )
    op.execute("DROP TRIGGER trg_credentials_generation ON credentials")
    op.execute("CREATE TRIGGER trg_credentials_generation AFTER UPDATE OF status, current_version_id, is_delete ON credentials FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()")


def downgrade() -> None:
    raise RuntimeError("Credential soft-delete downgrade is unsupported; restore from a verified backup")
