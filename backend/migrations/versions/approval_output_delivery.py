"""Add durable output-delivery obligations for execution approvals.

Revision ID: approval_output_delivery
Revises: initial_schema
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "approval_output_delivery"
down_revision = "initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_approval_requests",
        sa.Column(
            "spawn_authorized_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="执行审批请求：一次性进程创建授权提交时间。",
        ),
    )
    # Legacy finished receipts were committed by the pre-marker runner only
    # after process creation. Preserve that historical proof at the earliest
    # timestamp available before enforcing the new durable prerequisite.
    op.execute(
        "UPDATE execution_approval_requests SET spawn_authorized_at = claimed_at WHERE status = 'finished' AND spawn_authorized_at IS NULL",
    )
    op.create_check_constraint(
        "ck_execution_approval_requests_spawn_authorization",
        "execution_approval_requests",
        "(status != 'finished' OR spawn_authorized_at IS NOT NULL) AND "
        "(spawn_authorized_at IS NULL OR "
        "(status IN ('claimed', 'finished', 'launch_failed', 'unknown', "
        "'cancelled') AND execution_job_attempt_id IS NOT NULL "
        "AND claimed_at IS NOT NULL AND spawn_authorized_at >= claimed_at "
        "AND (terminal_at IS NULL OR spawn_authorized_at <= terminal_at)))",
    )
    op.create_table(
        "execution_approval_output_delivery_obligations",
        sa.Column(
            "approval_id",
            sa.Uuid(),
            nullable=False,
            comment="审批输出交付义务：执行审批请求标识。",
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            nullable=False,
            comment="审批输出交付义务：所属项目标识。",
        ),
        sa.Column(
            "owner_user_id",
            sa.String(length=36),
            nullable=False,
            comment="审批输出交付义务：私有数据所有者的用户标识。",
        ),
        sa.Column(
            "thread_id",
            sa.String(length=64),
            nullable=False,
            comment="审批输出交付义务：线程标识。",
        ),
        sa.Column(
            "mode",
            sa.String(length=16),
            server_default="any_one",
            nullable=False,
            comment="审批输出交付义务：履约模式。",
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="deferred",
            nullable=False,
            comment="审批输出交付义务：生命周期状态。",
        ),
        sa.Column(
            "continuation_run_id",
            sa.String(length=64),
            nullable=True,
            comment="审批输出交付义务：审批通过后续接运行的标识。",
        ),
        sa.Column(
            "continuation_job_id",
            sa.Uuid(),
            nullable=True,
            comment="审批输出交付义务：审批通过后续接任务的标识。",
        ),
        sa.Column(
            "intent_tool_call_id",
            sa.String(length=128),
            nullable=True,
            comment="审批输出交付义务：记录输出交付意图的工具调用标识。",
        ),
        sa.Column(
            "intent_digest",
            sa.CHAR(length=64),
            nullable=True,
            comment="审批输出交付义务：规范化私有输出交付意图的内容摘要。",
        ),
        sa.Column(
            "intent_private_json",
            sa.JSON(),
            nullable=True,
            comment="审批输出交付义务：仅限授权边界读取的规范化输出交付意图 JSON（最多 1 MiB）。",
        ),
        sa.Column(
            "satisfied_artifact_id",
            sa.Uuid(),
            nullable=True,
            comment="审批输出交付义务：满足输出交付义务的运行制品标识。",
        ),
        sa.Column(
            "version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
            comment="审批输出交付义务：记录版本号。",
        ),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="审批输出交付义务：输出交付义务分配给续接运行的时间。",
        ),
        sa.Column(
            "intent_recorded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="审批输出交付义务：输出交付意图持久化的时间。",
        ),
        sa.Column(
            "terminal_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="审批输出交付义务：输出交付义务进入终态的时间。",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="审批输出交付义务：记录创建时间。",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="审批输出交付义务：记录最近更新时间。",
        ),
        sa.PrimaryKeyConstraint("approval_id"),
        sa.UniqueConstraint(
            "approval_id",
            "project_id",
            "owner_user_id",
            "thread_id",
            name="uq_ea_output_delivery_obligations_private_scope",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_ea_output_delivery_obligations_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_ea_output_delivery_obligations_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_ea_output_delivery_obligations_membership",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id"],
            [
                "threads_meta.project_id",
                "threads_meta.owner_user_id",
                "threads_meta.thread_id",
            ],
            name="fk_ea_output_delivery_obligations_private_thread",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id", "project_id", "owner_user_id", "thread_id"],
            [
                "execution_approval_requests.id",
                "execution_approval_requests.project_id",
                "execution_approval_requests.owner_user_id",
                "execution_approval_requests.thread_id",
            ],
            name="fk_ea_output_delivery_obligations_approval",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "continuation_run_id"],
            [
                "runs.project_id",
                "runs.owner_user_id",
                "runs.thread_id",
                "runs.run_id",
            ],
            name="fk_ea_output_delivery_obligations_continuation_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "continuation_job_id",
                "project_id",
                "owner_user_id",
                "continuation_run_id",
            ],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id", "jobs.run_id"],
            name="fk_ea_output_delivery_obligations_continuation_job",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "project_id",
                "owner_user_id",
                "thread_id",
                "continuation_run_id",
                "satisfied_artifact_id",
            ],
            [
                "artifacts.project_id",
                "artifacts.owner_user_id",
                "artifacts.thread_id",
                "artifacts.run_id",
                "artifacts.id",
            ],
            name="fk_ea_output_delivery_obligations_satisfied_artifact",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "mode IN ('any_one')",
            name="ck_ea_output_delivery_obligations_mode",
        ),
        sa.CheckConstraint(
            "status IN ('deferred', 'assigned', 'intent_recorded', 'delivered', 'cancelled', 'blocked_unknown', 'failed')",
            name="ck_ea_output_delivery_obligations_status",
        ),
        sa.CheckConstraint(
            "(continuation_run_id IS NULL) = (continuation_job_id IS NULL) AND (continuation_run_id IS NULL) = (assigned_at IS NULL)",
            name="ck_ea_output_delivery_obligations_assignment_shape",
        ),
        sa.CheckConstraint(
            "(intent_tool_call_id IS NULL AND intent_digest IS NULL "
            "AND intent_private_json IS NULL AND intent_recorded_at IS NULL) "
            "OR (intent_tool_call_id IS NOT NULL "
            "AND intent_tool_call_id <> '' "
            "AND intent_tool_call_id = btrim(intent_tool_call_id) "
            "AND intent_digest ~ '^[0-9a-f]{64}$' "
            "AND json_typeof(intent_private_json) = 'object' "
            "AND octet_length(intent_private_json::text) <= 1048576 "
            "AND intent_recorded_at IS NOT NULL)",
            name="ck_ea_output_delivery_obligations_intent_shape",
        ),
        sa.CheckConstraint(
            "(status = 'deferred' AND continuation_run_id IS NULL "
            "AND intent_tool_call_id IS NULL AND satisfied_artifact_id IS NULL "
            "AND terminal_at IS NULL) "
            "OR (status = 'assigned' AND continuation_run_id IS NOT NULL "
            "AND intent_tool_call_id IS NULL AND satisfied_artifact_id IS NULL "
            "AND terminal_at IS NULL) "
            "OR (status = 'intent_recorded' AND continuation_run_id IS NOT NULL "
            "AND intent_tool_call_id IS NOT NULL AND satisfied_artifact_id IS NULL "
            "AND terminal_at IS NULL) "
            "OR (status = 'delivered' AND continuation_run_id IS NOT NULL "
            "AND intent_tool_call_id IS NOT NULL AND satisfied_artifact_id IS NOT NULL "
            "AND terminal_at IS NOT NULL) "
            "OR (status = 'cancelled' AND satisfied_artifact_id IS NULL "
            "AND terminal_at IS NOT NULL) "
            "OR (status IN ('blocked_unknown', 'failed') "
            "AND continuation_run_id IS NOT NULL "
            "AND satisfied_artifact_id IS NULL AND terminal_at IS NOT NULL)",
            name="ck_ea_output_delivery_obligations_lifecycle_shape",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_ea_output_delivery_obligations_version",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at "
            "AND (assigned_at IS NULL OR assigned_at >= created_at) "
            "AND (intent_recorded_at IS NULL OR "
            "intent_recorded_at >= assigned_at) "
            "AND (terminal_at IS NULL OR terminal_at >= "
            "COALESCE(intent_recorded_at, assigned_at, created_at))",
            name="ck_ea_output_delivery_obligations_timestamps",
        ),
        comment="保存审批暂停后必须由续接运行完成的私有输出交付义务。",
    )
    op.create_index(
        "ix_ea_output_delivery_obligations_private_status",
        "execution_approval_output_delivery_obligations",
        ["project_id", "owner_user_id", "thread_id", "status", "updated_at"],
    )

    op.create_table(
        "execution_approval_output_delivery_candidates",
        sa.Column(
            "approval_id",
            sa.Uuid(),
            nullable=False,
            comment="审批输出交付候选：执行审批请求标识。",
        ),
        sa.Column(
            "file_id",
            sa.Uuid(),
            nullable=False,
            comment="审批输出交付候选：文件标识。",
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            nullable=False,
            comment="审批输出交付候选：所属项目标识。",
        ),
        sa.Column(
            "owner_user_id",
            sa.String(length=36),
            nullable=False,
            comment="审批输出交付候选：私有数据所有者的用户标识。",
        ),
        sa.Column(
            "thread_id",
            sa.String(length=64),
            nullable=False,
            comment="审批输出交付候选：线程标识。",
        ),
        sa.Column(
            "logical_path",
            sa.String(length=1024),
            nullable=False,
            comment="审批输出交付候选：文件在项目中的逻辑路径。",
        ),
        sa.Column(
            "file_version",
            sa.BigInteger(),
            nullable=False,
            comment="审批输出交付候选：候选文件的冻结版本号。",
        ),
        sa.Column(
            "sha256",
            sa.CHAR(length=64),
            nullable=False,
            comment="审批输出交付候选：内容的 SHA-256 摘要。",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="审批输出交付候选：记录创建时间。",
        ),
        sa.PrimaryKeyConstraint("approval_id", "file_id"),
        sa.UniqueConstraint(
            "approval_id",
            "logical_path",
            name="uq_ea_output_delivery_candidates_path",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id", "project_id", "owner_user_id", "thread_id"],
            [
                "execution_approval_output_delivery_obligations.approval_id",
                "execution_approval_output_delivery_obligations.project_id",
                "execution_approval_output_delivery_obligations.owner_user_id",
                "execution_approval_output_delivery_obligations.thread_id",
            ],
            name="fk_ea_output_delivery_candidates_obligation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "file_id"],
            ["files.project_id", "files.owner_user_id", "files.thread_id", "files.id"],
            name="fk_ea_output_delivery_candidates_private_file",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "logical_path LIKE 'outputs/%' AND logical_path <> 'outputs/' AND logical_path !~ '(^|/)\\.\\.(/|$)' AND logical_path !~ '^[A-Za-z]:'",
            name="ck_ea_output_delivery_candidates_path",
        ),
        sa.CheckConstraint(
            "file_version >= 1",
            name="ck_ea_output_delivery_candidates_version",
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_ea_output_delivery_candidates_sha256",
        ),
        comment="冻结可满足审批输出交付义务的私有文件身份与版本。",
    )
    op.create_index(
        "ix_ea_output_delivery_candidates_private",
        "execution_approval_output_delivery_candidates",
        ["project_id", "owner_user_id", "thread_id", "approval_id"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead",
    )
