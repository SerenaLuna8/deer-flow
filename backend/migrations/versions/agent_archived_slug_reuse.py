"""Release archived project Agent slugs for reuse.

Revision ID: agent_archived_slug_reuse
Revises: agent_design_resume_index
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "agent_archived_slug_reuse"
down_revision = "agent_design_resume_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "uq_agents_project_slug",
        table_name="agents",
    )
    op.create_index(
        "uq_agents_project_slug",
        "agents",
        ["project_id", sa.text("lower(slug)")],
        unique=True,
        postgresql_where=sa.text(
            "scope = 'project' AND status != 'archived'",
        ),
    )


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead",
    )
