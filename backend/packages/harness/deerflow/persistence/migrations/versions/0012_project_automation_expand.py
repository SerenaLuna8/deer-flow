"""Expand project automation schema without tightening legacy rows.

Revision ID: 0012_project_automation_expand
Revises: 0011_private_artifact_tombstone
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_project_automation_expand"
down_revision: str | Sequence[str] | None = "0011_private_artifact_tombstone"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_occurrence_expand_columns() -> None:
    for column in (
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("owner_user_id", sa.String(36), nullable=True),
        sa.Column("task_version", sa.BigInteger(), nullable=True),
        sa.Column("occurrence_key", sa.CHAR(64), nullable=True),
        sa.Column("manual_idempotency_hash", sa.CHAR(64), nullable=True),
        sa.Column("resolved_membership_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_membership_version", sa.BigInteger(), nullable=True),
        sa.Column("launch_attempt_count", sa.Integer(), nullable=True),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("scheduled_task_runs", column)
    op.alter_column(
        "scheduled_task_runs",
        "status",
        existing_type=sa.String(16),
        type_=sa.String(20),
        existing_nullable=False,
    )
    # Staging must be able to clear legacy synthetic Thread pointers for
    # pre-admission skipped occurrences before the final relation probes run.
    op.alter_column(
        "scheduled_task_runs",
        "thread_id",
        existing_type=sa.String(64),
        nullable=True,
    )
    op.create_index(
        "ix_scheduled_task_runs_project_id",
        "scheduled_task_runs",
        ["project_id"],
    )
    op.create_index(
        "ix_scheduled_task_runs_owner_user_id",
        "scheduled_task_runs",
        ["owner_user_id"],
    )


def _create_automation_migration_control_tables() -> None:
    op.create_table(
        "automation_migration_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="running", nullable=False),
        sa.Column("source_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("owner_map_digest", sa.CHAR(64), nullable=False),
        sa.Column("source_task_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("source_run_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "source_probe_complete",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "scope_relation_probe_complete",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "mode IN ('dry_run', 'execute')",
            name="ck_automation_migration_runs_mode",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_automation_migration_runs_status",
        ),
        sa.CheckConstraint(
            "source_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_automation_migration_runs_source",
        ),
        sa.CheckConstraint(
            "owner_map_digest ~ '^[0-9a-f]{64}$'",
            name="ck_automation_migration_runs_owner_map",
        ),
        sa.CheckConstraint(
            "source_task_count >= 0 AND source_run_count >= 0",
            name="ck_automation_migration_runs_counts",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "automation_migration_ledger",
        sa.Column("migration_run_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.String(32), nullable=False),
        sa.Column("source_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("target_digest", sa.CHAR(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="complete", nullable=False),
        sa.Column("source_row_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("target_row_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "domain IN ('scheduled_tasks', 'scheduled_task_runs')",
            name="ck_automation_migration_ledger_domain",
        ),
        sa.CheckConstraint(
            "status = 'complete'",
            name="ck_automation_migration_ledger_status",
        ),
        sa.CheckConstraint(
            "source_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_automation_migration_ledger_source",
        ),
        sa.CheckConstraint(
            "target_digest ~ '^[0-9a-f]{64}$'",
            name="ck_automation_migration_ledger_target",
        ),
        sa.CheckConstraint(
            "source_row_count >= 0 AND target_row_count >= 0",
            name="ck_automation_migration_ledger_counts",
        ),
        sa.ForeignKeyConstraint(
            ["migration_run_id"],
            ["automation_migration_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("migration_run_id", "domain"),
    )
    op.create_table(
        "automation_cutover_state",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("migration_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "empty_domain_probe_complete",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "final_schema_probe_complete",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("cutover_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_automation_cutover_state_singleton"),
        sa.CheckConstraint(
            "stage IN ('empty_install', 'migration_ready', 'cutover_complete')",
            name="ck_automation_cutover_state_stage",
        ),
        sa.CheckConstraint(
            "stage != 'migration_ready' OR migration_run_id IS NOT NULL",
            name="ck_automation_cutover_state_migration_ready",
        ),
        sa.CheckConstraint(
            "stage != 'cutover_complete' OR ((empty_domain_probe_complete OR migration_run_id IS NOT NULL) AND final_schema_probe_complete AND cutover_at IS NOT NULL)",
            name="ck_automation_cutover_state_complete",
        ),
        sa.ForeignKeyConstraint(
            ["migration_run_id"],
            ["automation_migration_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def upgrade() -> None:
    for column in (
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("owner_user_id", sa.String(36), nullable=True),
        sa.Column("agent_asset_id", sa.Uuid(), nullable=True),
        sa.Column("agent_scope", sa.String(16), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=True),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outcome", sa.String(24), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
    ):
        op.add_column("scheduled_tasks", column)
    op.create_index("ix_scheduled_tasks_project_id", "scheduled_tasks", ["project_id"])
    op.create_index("ix_scheduled_tasks_owner_user_id", "scheduled_tasks", ["owner_user_id"])
    _add_occurrence_expand_columns()
    _create_automation_migration_control_tables()


def _assert_downgrade_safe() -> None:
    connection = op.get_bind()
    for table in (
        "scheduled_tasks",
        "scheduled_task_runs",
        "automation_migration_runs",
        "automation_migration_ledger",
    ):
        if connection.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}" LIMIT 1)')).scalar_one():  # noqa: S608 - fixed internal table allowlist
            raise RuntimeError("cannot downgrade M5 expand while automation migration data exists")


def downgrade() -> None:
    _assert_downgrade_safe()
    for table in (
        "automation_cutover_state",
        "automation_migration_ledger",
        "automation_migration_runs",
    ):
        op.drop_table(table)

    op.drop_index("ix_scheduled_task_runs_owner_user_id", table_name="scheduled_task_runs")
    op.drop_index("ix_scheduled_task_runs_project_id", table_name="scheduled_task_runs")
    op.alter_column(
        "scheduled_task_runs",
        "status",
        existing_type=sa.String(20),
        type_=sa.String(16),
        existing_nullable=False,
    )
    op.alter_column(
        "scheduled_task_runs",
        "thread_id",
        existing_type=sa.String(64),
        nullable=False,
    )
    for column in (
        "updated_at",
        "error_message",
        "error_code",
        "next_attempt_at",
        "lease_expires_at",
        "lease_owner",
        "launch_attempt_count",
        "resolved_membership_version",
        "resolved_membership_id",
        "manual_idempotency_hash",
        "occurrence_key",
        "task_version",
        "owner_user_id",
        "project_id",
    ):
        op.drop_column("scheduled_task_runs", column)

    op.drop_index("ix_scheduled_tasks_owner_user_id", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_project_id", table_name="scheduled_tasks")
    for column in (
        "last_error_code",
        "last_outcome",
        "deleted_at",
        "frozen_at",
        "version",
        "agent_scope",
        "agent_asset_id",
        "owner_user_id",
        "project_id",
    ):
        op.drop_column("scheduled_tasks", column)
