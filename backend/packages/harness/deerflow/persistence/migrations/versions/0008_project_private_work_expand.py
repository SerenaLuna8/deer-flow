"""Expand project-private work schema without tightening legacy rows.

Revision ID: 0008_project_private_work_expand
Revises: 0007_project_shared_assets
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_project_private_work_expand"
down_revision: str | Sequence[str] | None = "0007_project_shared_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


M4_BUSINESS_TABLES: tuple[str, ...] = (
    "run_asset_versions",
    "run_mcp_grant_snapshots",
    "files",
    "file_chunks",
    "artifacts",
    "user_project_memories",
    "user_project_memory_facts",
)

M4_SCOPED_EXISTING_TABLES: tuple[str, ...] = (
    "threads_meta",
    "runs",
    "run_events",
    "feedback",
    "channel_connections",
    "channel_oauth_states",
    "channel_conversations",
)


def _scope_columns() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("owner_user_id", sa.String(36), nullable=True),
    )


def _create_private_work_tables() -> None:
    op.create_table(
        "run_asset_versions",
        *_scope_columns(),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("asset_kind", sa.String(16), nullable=False),
        sa.Column("dependency_order", sa.Integer(), nullable=False),
        sa.Column("asset_scope", sa.String(16), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("payload_checksum", sa.CHAR(64), nullable=False),
        sa.Column("catalog_generation", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "asset_kind", "dependency_order", name="pk_run_asset_versions"),
    )
    op.create_index("ix_run_asset_versions_staging_scope", "run_asset_versions", ["project_id", "owner_user_id", "run_id"])

    op.create_table(
        "run_mcp_grant_snapshots",
        *_scope_columns(),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("mcp_version_id", sa.Uuid(), nullable=False),
        sa.Column("credential_slot_id", sa.Uuid(), nullable=False),
        sa.Column("credential_grant_id", sa.Uuid(), nullable=False),
        sa.Column("credential_version_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["mcp_version_id"], ["mcp_server_versions.id"], name="fk_run_mcp_grant_snapshots_mcp_version", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["credential_slot_id"], ["mcp_version_credential_slots.id"], name="fk_run_mcp_grant_snapshots_slot", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["credential_grant_id"], ["credential_grants.id"], name="fk_run_mcp_grant_snapshots_grant", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["credential_version_id"], ["credential_versions.id"], name="fk_run_mcp_grant_snapshots_credential_version", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("run_id", "mcp_version_id", "credential_slot_id", name="pk_run_mcp_grant_snapshots"),
    )
    op.create_index("ix_run_mcp_grant_snapshots_staging_scope", "run_mcp_grant_snapshots", ["project_id", "owner_user_id", "run_id"])

    op.create_table(
        "files",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_scope_columns(),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("logical_path", sa.String(1024), nullable=False),
        sa.Column("media_type", sa.String(255), server_default="application/octet-stream", nullable=False),
        sa.Column("size", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="staging", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by_run_id", sa.String(64), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_files_staging_scope", "files", ["project_id", "owner_user_id", "thread_id"])

    op.create_table(
        "file_chunks",
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name="fk_file_chunks_file_id_files", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("file_id", "chunk_index"),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_scope_columns(),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("artifact_metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_staging_scope", "artifacts", ["project_id", "owner_user_id", "thread_id", "run_id"])

    op.create_table(
        "user_project_memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_scope_columns(),
        sa.Column("namespace", sa.String(255), server_default="default", nullable=False),
        sa.Column("context_summary", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_project_memories_staging_scope", "user_project_memories", ["project_id", "owner_user_id"])

    op.create_table(
        "user_project_memory_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_scope_columns(),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_thread_id", sa.String(64), nullable=True),
        sa.Column("source_run_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["memory_id"], ["user_project_memories.id"], name="fk_user_project_memory_facts_memory_id", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_project_memory_facts_staging_scope", "user_project_memory_facts", ["project_id", "owner_user_id", "memory_id"])


def _create_migration_control_tables() -> None:
    op.create_table(
        "private_work_migration_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="running", nullable=False),
        sa.Column("source_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("owner_map_digest", sa.CHAR(64), nullable=False),
        sa.Column("database_backup_proof_digest", sa.CHAR(64), nullable=True),
        sa.Column("legacy_source_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("checkpoint_marker_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("cross_scope_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("mode IN ('dry_run', 'execute')", name="ck_private_work_migration_runs_mode"),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_private_work_migration_runs_status"),
        sa.CheckConstraint("source_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_private_work_migration_runs_source"),
        sa.CheckConstraint("owner_map_digest ~ '^[0-9a-f]{64}$'", name="ck_private_work_migration_runs_owner_map"),
        sa.CheckConstraint(
            "database_backup_proof_digest IS NULL OR database_backup_proof_digest ~ '^[0-9a-f]{64}$'",
            name="ck_private_work_migration_runs_backup",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "private_work_migration_ledger",
        sa.Column("migration_run_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.String(32), nullable=False),
        sa.Column("source_key_hash", sa.CHAR(64), nullable=False),
        sa.Column("source_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("target_digest", sa.CHAR(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="complete", nullable=False),
        sa.Column("row_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("byte_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status = 'complete'", name="ck_private_work_migration_ledger_status"),
        sa.CheckConstraint("source_key_hash ~ '^[0-9a-f]{64}$'", name="ck_private_work_migration_ledger_source_key"),
        sa.CheckConstraint("source_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_private_work_migration_ledger_source"),
        sa.CheckConstraint("target_digest ~ '^[0-9a-f]{64}$'", name="ck_private_work_migration_ledger_target"),
        sa.CheckConstraint("row_count >= 0 AND byte_count >= 0", name="ck_private_work_migration_ledger_counts"),
        sa.ForeignKeyConstraint(["migration_run_id"], ["private_work_migration_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("migration_run_id", "domain", "source_key_hash"),
    )
    op.create_table(
        "private_work_cutover_state",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("migration_run_id", sa.Uuid(), nullable=True),
        sa.Column("empty_domain_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("checkpoint_marker_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("cutover_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_private_work_cutover_state_singleton"),
        sa.CheckConstraint("stage IN ('empty_install', 'migration_ready', 'cutover_complete')", name="ck_private_work_cutover_state_stage"),
        sa.CheckConstraint("stage != 'cutover_complete' OR cutover_at IS NOT NULL", name="ck_private_work_cutover_state_cutover_at"),
        sa.ForeignKeyConstraint(["migration_run_id"], ["private_work_migration_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )


def upgrade() -> None:
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(32),
        type_=sa.String(64),
        existing_nullable=False,
    )
    for table in ("threads_meta", "runs", "run_events", "feedback"):
        op.add_column(table, sa.Column("project_id", sa.Uuid(), nullable=True))
        op.create_index(f"ix_{table}_project_id", table, ["project_id"])

    op.add_column("threads_meta", sa.Column("agent_asset_id", sa.Uuid(), nullable=True))
    op.add_column("threads_meta", sa.Column("agent_scope", sa.String(16), nullable=True))
    op.add_column("threads_meta", sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("threads_meta", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("threads_meta", sa.Column("checkpoint_delete_status", sa.String(24), server_default="not_requested", nullable=True))
    op.add_column("threads_meta", sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=True))

    op.add_column("runs", sa.Column("authorization_cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("authorization_cancel_reason", sa.String(64), nullable=True))
    op.add_column("runs", sa.Column("finalization_status", sa.String(20), server_default="pending", nullable=True))

    for table in ("channel_connections", "channel_oauth_states", "channel_conversations"):
        op.add_column(table, sa.Column("project_id", sa.Uuid(), nullable=True))
        op.create_index(f"ix_{table}_project_id", table, ["project_id"])
    op.add_column("channel_connections", sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True))

    _create_private_work_tables()
    _create_migration_control_tables()


def _assert_expand_downgrade_safe() -> None:
    bind = op.get_bind()
    for table in M4_BUSINESS_TABLES:
        if bind.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}" LIMIT 1)')).scalar_one():  # noqa: S608 - fixed internal table tuple
            raise RuntimeError("cannot downgrade M4 expand while private-work data exists")
    for table in M4_SCOPED_EXISTING_TABLES:
        if bind.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}" WHERE project_id IS NOT NULL LIMIT 1)')).scalar_one():  # noqa: S608 - fixed internal table tuple
            raise RuntimeError("cannot downgrade M4 expand while backfilled private scope exists")


def downgrade() -> None:
    _assert_expand_downgrade_safe()

    for table in (
        "private_work_cutover_state",
        "private_work_migration_ledger",
        "private_work_migration_runs",
        "user_project_memory_facts",
        "user_project_memories",
        "artifacts",
        "file_chunks",
        "files",
        "run_mcp_grant_snapshots",
        "run_asset_versions",
    ):
        op.drop_table(table)

    op.drop_column("channel_connections", "frozen_at")
    for table in ("channel_conversations", "channel_oauth_states", "channel_connections"):
        op.drop_index(f"ix_{table}_project_id", table_name=table)
        op.drop_column(table, "project_id")

    for column in ("finalization_status", "authorization_cancel_reason", "authorization_cancel_requested_at"):
        op.drop_column("runs", column)
    for column in ("version", "checkpoint_delete_status", "deleted_at", "frozen_at", "agent_scope", "agent_asset_id"):
        op.drop_column("threads_meta", column)
    for table in ("feedback", "run_events", "runs", "threads_meta"):
        op.drop_index(f"ix_{table}_project_id", table_name=table)
        op.drop_column(table, "project_id")
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(64),
        type_=sa.String(32),
        existing_nullable=False,
    )
