"""Align Agent Builder resume lookup with its immutable keyset.

Revision ID: agent_design_resume_index
Revises: model_catalog_simplify
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "agent_design_resume_index"
down_revision = "model_catalog_simplify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_agent_design_sessions_resume",
        table_name="agent_design_sessions",
    )
    op.create_index(
        "ix_agent_design_sessions_resume",
        "agent_design_sessions",
        [
            "project_id",
            "owner_user_id",
            sa.text("created_at DESC"),
            sa.text("id DESC"),
        ],
        postgresql_where=sa.text(
            "status NOT IN ('completed', 'cancelled')",
        ),
    )


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead",
    )
