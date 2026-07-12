"""migration ledger.

Revision ID: 0004_migration_ledger
Revises: 0003_scheduled_tasks
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_migration_ledger"
down_revision: str | Sequence[str] | None = "0003_scheduled_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "migration_ledger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_table", sa.String(length=128), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("target_table", sa.String(length=128), nullable=False),
        sa.Column("target_key", sa.Text(), nullable=False),
        sa.Column("row_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("migrated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_sha256",
            "source_table",
            "source_key",
            name="uq_migration_source_row",
        ),
    )


def downgrade() -> None:
    op.drop_table("migration_ledger")
