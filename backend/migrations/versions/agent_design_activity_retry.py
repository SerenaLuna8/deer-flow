"""Add stoppable Builder turns and frozen generation profiles.

Revision ID: agent_design_activity_retry
Revises: agent_design_activity
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "agent_design_activity_retry"
down_revision = "agent_design_activity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_design_operations",
        sa.Column(
            "stop_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="智能体设计操作：停止请求时间。",
        ),
    )
    op.add_column(
        "agent_design_operations",
        sa.Column(
            "requested_generation_profile_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="智能体设计操作：请求生成配置 JSON 数据。",
        ),
    )
    op.add_column(
        "agent_design_operations",
        sa.Column(
            "effective_generation_profile_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="智能体设计操作：生效生成配置 JSON 数据。",
        ),
    )
    op.drop_constraint(
        "ck_agent_design_operations_status",
        "agent_design_operations",
        type_="check",
    )
    op.drop_constraint(
        "ck_agent_design_operations_completion",
        "agent_design_operations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_design_operations_status",
        "agent_design_operations",
        "status IN ('in_progress', 'completed', 'failed', 'stopped')",
    )
    op.create_check_constraint(
        "ck_agent_design_operations_completion",
        "agent_design_operations",
        "(status = 'in_progress' AND result_revision IS NULL AND public_error_code IS NULL) "
        "OR (status = 'completed' AND result_revision IS NOT NULL AND public_error_code IS NULL) "
        "OR (status = 'failed' AND result_revision IS NOT NULL AND public_error_code IS NOT NULL) "
        "OR (status = 'stopped' AND result_revision IS NOT NULL AND public_error_code IS NULL)",
    )
    op.create_check_constraint(
        "ck_agent_design_operations_generation_profile",
        "agent_design_operations",
        "(requested_generation_profile_json IS NULL AND effective_generation_profile_json IS NULL) OR (operation_kind = 'turn' AND requested_generation_profile_json IS NOT NULL AND effective_generation_profile_json IS NOT NULL)",
    )


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead",
    )
