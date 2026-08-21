"""Enforce one terminal Activity per Builder operation.

Revision ID: agent_design_activity_terminal
Revises: agent_design_activity_retry
"""

from __future__ import annotations

from alembic import op

revision = "agent_design_activity_terminal"
down_revision = "agent_design_activity_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_design_activities_terminal ON agent_design_activities (operation_id) WHERE kind IN ('turn_terminal', 'commit_terminal')")


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead",
    )
