"""Add Builder generation profiles and durable activity.

Revision ID: agent_design_activity
Revises: current_asset_version_lifecycle
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "agent_design_activity"
down_revision = "current_asset_version_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_design_sessions",
        sa.Column(
            "generation_model_ref",
            sa.String(length=36),
            nullable=True,
            comment="智能体设计会话：生成模型引用。",
        ),
    )
    op.add_column(
        "agent_design_sessions",
        sa.Column(
            "generation_mode",
            sa.String(length=16),
            nullable=True,
            comment="智能体设计会话：生成模式。",
        ),
    )
    op.create_check_constraint(
        "ck_agent_design_sessions_generation_preference",
        "agent_design_sessions",
        "(generation_model_ref IS NULL AND generation_mode IS NULL) OR (generation_model_ref IS NOT NULL AND generation_mode IN ('flash', 'thinking', 'pro', 'ultra'))",
    )

    op.create_unique_constraint(
        "uq_agent_design_operations_private_scope",
        "agent_design_operations",
        ["project_id", "owner_user_id", "session_id", "id"],
    )

    op.create_table(
        "agent_design_activities",
        sa.Column(
            "seq",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
            comment="智能体设计活动：单调序号。",
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            nullable=False,
            comment="智能体设计活动：所属项目标识。",
        ),
        sa.Column(
            "owner_user_id",
            sa.String(length=36),
            nullable=False,
            comment="智能体设计活动：私有数据所有者的用户标识。",
        ),
        sa.Column(
            "session_id",
            sa.Uuid(),
            nullable=False,
            comment="智能体设计活动：会话标识。",
        ),
        sa.Column(
            "operation_id",
            sa.Uuid(),
            nullable=False,
            comment="智能体设计活动：操作标识。",
        ),
        sa.Column(
            "attempt",
            sa.Integer(),
            nullable=True,
            comment="智能体设计活动：尝试。",
        ),
        sa.Column(
            "kind",
            sa.String(length=40),
            nullable=False,
            comment="智能体设计活动：业务类型。",
        ),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="智能体设计活动：公开载荷 JSON 数据。",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="智能体设计活动：记录创建时间。",
        ),
        sa.CheckConstraint(
            "attempt IS NULL OR attempt IN (1, 2)",
            name="ck_agent_design_activities_attempt",
        ),
        sa.CheckConstraint(
            "kind IN ('turn_accepted', 'attempt_started', 'reasoning', "
            "'candidate_generated', 'validation_started', 'validation_passed', "
            "'validation_failed', 'repair_started', 'turn_terminal', "
            "'commit_accepted', 'commit_validation_started', "
            "'commit_validation_passed', 'commit_persistence_started', "
            "'commit_persistence_completed', 'commit_terminal')",
            name="ck_agent_design_activities_kind",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "session_id"],
            [
                "agent_design_sessions.project_id",
                "agent_design_sessions.owner_user_id",
                "agent_design_sessions.id",
            ],
            name="fk_agent_design_activities_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "session_id", "operation_id"],
            [
                "agent_design_operations.project_id",
                "agent_design_operations.owner_user_id",
                "agent_design_operations.session_id",
                "agent_design_operations.id",
            ],
            name="fk_agent_design_activities_operation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("seq", name="pk_agent_design_activities"),
        comment="保存智能体设计会话中可回放的公开过程事件。",
    )
    op.create_index(
        "ix_agent_design_activities_session_seq",
        "agent_design_activities",
        ["project_id", "owner_user_id", "session_id", "seq"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead",
    )
