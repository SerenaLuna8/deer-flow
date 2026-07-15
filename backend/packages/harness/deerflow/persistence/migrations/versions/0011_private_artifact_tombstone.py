"""Add an explicit tombstone to private artifacts.

Revision ID: 0011_private_artifact_tombstone
Revises: 0010_private_file_source
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_private_artifact_tombstone"
down_revision: str | Sequence[str] | None = "0010_private_file_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_artifacts_private_active",
        "artifacts",
        ["project_id", "owner_user_id", "thread_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_artifacts_private_active", table_name="artifacts")
    op.drop_column("artifacts", "deleted_at")
