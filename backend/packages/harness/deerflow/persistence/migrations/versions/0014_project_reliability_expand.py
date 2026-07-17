"""Expand project reliability schema without changing execution authority.

Revision ID: 0014_project_reliability_expand
Revises: 0013_project_automation_finalize
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_project_reliability_expand"
down_revision: str | Sequence[str] | None = "0013_project_automation_finalize"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_worker_and_job_tables() -> None:
    op.create_table(
        "worker_nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("capabilities_json", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("max_concurrent_jobs", sa.Integer(), nullable=False),
        sa.Column("draining", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("max_concurrent_jobs >= 1", name="ck_worker_nodes_capacity"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_worker_nodes_fresh", "worker_nodes", ["draining", "heartbeat_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("automation_occurrence_id", sa.String(64), nullable=True),
        sa.Column("predecessor_dead_job_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.CHAR(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="queued", nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner_id", sa.Uuid(), nullable=True),
        sa.Column("lease_token_hash", sa.CHAR(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_safety", sa.String(16), server_default="safe", nullable=False),
        sa.Column("public_error_code", sa.String(64), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("job_type IN ('private_run', 'automation_run', 'retention_purge')", name="ck_jobs_type"),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled', 'dead')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint("retry_safety IN ('safe', 'unknown', 'unsafe')", name="ck_jobs_retry_safety"),
        sa.CheckConstraint("attempt_count >= 0 AND max_attempts >= 1", name="ck_jobs_attempts"),
        sa.CheckConstraint(
            "(job_type = 'private_run' AND run_id IS NOT NULL AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NULL) "
            "OR (job_type = 'automation_run' AND run_id IS NOT NULL AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NOT NULL) "
            "OR (job_type = 'retention_purge' AND run_id IS NULL AND automation_occurrence_id IS NULL)",
            name="ck_jobs_authority_shape",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_jobs_project", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["lease_owner_id"], ["worker_nodes.id"], name="fk_jobs_lease_worker", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_type", "idempotency_key", name="uq_jobs_type_idempotency"),
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "available_at", sa.text("priority DESC"), "created_at"])
    op.create_index(
        "ix_jobs_active_lease",
        "jobs",
        ["lease_expires_at", "id"],
        postgresql_where=sa.text("status IN ('leased', 'running')"),
    )
    op.create_index("ix_jobs_private_scope", "jobs", ["project_id", "owner_user_id", "created_at"])

    op.create_table(
        "job_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Uuid(), nullable=False),
        sa.Column("lease_token_hash", sa.CHAR(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("public_error_code", sa.String(64), nullable=True),
        sa.Column("checkpoint_cursor", sa.String(128), nullable=True),
        sa.Column("stream_cursor", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("attempt_number >= 1", name="ck_job_attempts_number"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('succeeded', 'retry', 'cancelled', 'failed', 'lease_lost', 'dead')",
            name="ck_job_attempts_outcome",
        ),
        sa.CheckConstraint("stream_cursor IS NULL OR stream_cursor >= 0", name="ck_job_attempts_stream_cursor"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["worker_id"], ["worker_nodes.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_number"),
    )
    op.create_index("ix_job_attempts_job_started", "job_attempts", ["job_id", sa.text("started_at DESC")])

    op.create_table(
        "dead_jobs",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_ref_key_id", sa.String(64), nullable=True),
        sa.Column("owner_ref_hmac", sa.CHAR(64), nullable=True),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("retry_safety", sa.String(16), nullable=False),
        sa.Column("public_error_code", sa.String(64), nullable=False),
        sa.Column("dead_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("attempt_count >= 1", name="ck_dead_jobs_attempt_count"),
        sa.CheckConstraint("retry_safety IN ('safe', 'unknown', 'unsafe')", name="ck_dead_jobs_retry_safety"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index("ix_dead_jobs_project_dead", "dead_jobs", ["project_id", sa.text("dead_at DESC"), "job_id"])


def _create_quota_tables() -> None:
    op.create_table(
        "project_quotas",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("member_limit", sa.Integer(), nullable=True),
        sa.Column("storage_bytes_limit", sa.BigInteger(), nullable=True),
        sa.Column("concurrent_run_limit", sa.Integer(), nullable=True),
        sa.Column("mcp_calls_daily_limit", sa.Integer(), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_by_user_id", sa.String(36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(member_limit IS NULL OR member_limit >= 1) AND (storage_bytes_limit IS NULL OR storage_bytes_limit >= 0) "
            "AND (concurrent_run_limit IS NULL OR concurrent_run_limit >= 1) "
            "AND (mcp_calls_daily_limit IS NULL OR mcp_calls_daily_limit >= 0) AND version >= 1",
            name="ck_project_quotas_limits",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("project_id"),
    )
    op.create_table(
        "project_usage_counters",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("dimension", sa.String(32), nullable=False),
        sa.Column("bucket", sa.String(32), server_default="lifetime", nullable=False),
        sa.Column("used", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "dimension IN ('members', 'storage_bytes', 'concurrent_runs', 'mcp_calls_daily')",
            name="ck_project_usage_counters_dimension",
        ),
        sa.CheckConstraint("used >= 0 AND reserved >= 0 AND version >= 1", name="ck_project_usage_counters_values"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "dimension", "bucket"),
    )
    op.create_table(
        "project_usage_ledger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("dimension", sa.String(32), nullable=False),
        sa.Column("delta", sa.BigInteger(), nullable=False),
        sa.Column("bucket", sa.String(32), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_ref_key_id", sa.String(64), nullable=False),
        sa.Column("source_ref_hmac", sa.CHAR(64), nullable=False),
        sa.Column("idempotency_key", sa.CHAR(64), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "dimension IN ('members', 'storage_bytes', 'concurrent_runs', 'mcp_calls_daily')",
            name="ck_project_usage_ledger_dimension",
        ),
        sa.CheckConstraint("delta <> 0", name="ck_project_usage_ledger_delta"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "dimension", "idempotency_key", name="uq_project_usage_ledger_idempotency"),
    )
    op.create_index(
        "ix_project_usage_ledger_project_cursor",
        "project_usage_ledger",
        ["project_id", sa.text("occurred_at DESC"), sa.text("id DESC")],
    )


def _create_audit_and_recovery_tables() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_user_id", sa.String(36), nullable=True),
        sa.Column("actor_process", sa.String(32), nullable=True),
        sa.Column("actor_platform_role", sa.String(32), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_kind", sa.String(32), nullable=False),
        sa.Column("target_ref_key_id", sa.String(64), nullable=False),
        sa.Column("target_ref_hmac", sa.CHAR(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("public_error_code", sa.String(64), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.CheckConstraint(
            "(actor_user_id IS NOT NULL AND actor_process IS NULL) OR (actor_user_id IS NULL AND actor_process IS NOT NULL)",
            name="ck_audit_logs_actor",
        ),
        sa.CheckConstraint("outcome IN ('success', 'rejected', 'failed')", name="ck_audit_logs_outcome"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["attempt_id"], ["job_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_project_cursor", "audit_logs", ["project_id", sa.text("occurred_at DESC"), sa.text("id DESC")])
    op.create_index("ix_audit_logs_platform_cursor", "audit_logs", [sa.text("occurred_at DESC"), sa.text("id DESC")])

    op.create_table(
        "deletion_tombstones",
        sa.Column("journal_sequence", sa.BigInteger(), nullable=False),
        sa.Column("ciphertext_digest", sa.CHAR(64), nullable=False),
        sa.Column("record_digest", sa.CHAR(64), nullable=False),
        sa.Column("resource_kind", sa.String(32), nullable=False),
        sa.Column("resource_ref_key_id", sa.String(64), nullable=False),
        sa.Column("resource_ref_hmac", sa.CHAR(64), nullable=False),
        sa.Column("purge_status", sa.String(16), server_default="journaled", nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("journal_sequence >= 1", name="ck_deletion_tombstones_sequence"),
        sa.CheckConstraint("purge_status IN ('journaled', 'purged')", name="ck_deletion_tombstones_status"),
        sa.PrimaryKeyConstraint("journal_sequence"),
        sa.UniqueConstraint("ciphertext_digest"),
        sa.UniqueConstraint("record_digest"),
    )
    op.create_index("ix_deletion_tombstones_committed", "deletion_tombstones", ["committed_at", "journal_sequence"])

    op.create_table(
        "recovery_journal_state",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("source_installation_id", sa.CHAR(64), nullable=False),
        sa.Column("journal_id", sa.Uuid(), nullable=False),
        sa.Column("high_watermark", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("head_digest", sa.CHAR(64), server_default="0" * 64, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_recovery_journal_state_singleton"),
        sa.CheckConstraint("high_watermark >= 0", name="ck_recovery_journal_state_sequence"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("journal_id"),
    )

    op.create_table(
        "restore_proofs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("archive_id", sa.Uuid(), nullable=False),
        sa.Column("archive_digest", sa.CHAR(64), nullable=False),
        sa.Column("target_database_ref_key_id", sa.String(64), nullable=False),
        sa.Column("target_database_ref_hmac", sa.CHAR(64), nullable=False),
        sa.Column("schema_revision", sa.String(64), nullable=False),
        sa.Column("archive_tombstone_sequence", sa.BigInteger(), nullable=False),
        sa.Column("replayed_through_sequence", sa.BigInteger(), nullable=False),
        sa.Column("journal_id", sa.Uuid(), nullable=False),
        sa.Column("final_journal_head_digest", sa.CHAR(64), nullable=False),
        sa.Column("probes_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("restored_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "archive_tombstone_sequence >= 0 AND replayed_through_sequence >= archive_tombstone_sequence",
            name="ck_restore_proofs_sequences",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_restore_proofs_archive", "restore_proofs", ["archive_id", sa.text("restored_at DESC")])


def _create_migration_control_tables() -> None:
    op.create_table(
        "reliability_migration_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="running", nullable=False),
        sa.Column("source_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("backup_proof_digest", sa.CHAR(64), nullable=True),
        sa.Column("source_row_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("source_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("active_run_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("mode IN ('dry_run', 'execute')", name="ck_reliability_migration_runs_mode"),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_reliability_migration_runs_status"),
        sa.CheckConstraint("source_row_count >= 0", name="ck_reliability_migration_runs_count"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "reliability_migration_ledger",
        sa.Column("migration_run_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.String(32), nullable=False),
        sa.Column("source_digest", sa.CHAR(64), nullable=False),
        sa.Column("target_digest", sa.CHAR(64), nullable=False),
        sa.Column("source_row_count", sa.BigInteger(), nullable=False),
        sa.Column("target_row_count", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), server_default="complete", nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "domain IN ('jobs', 'quotas', 'audit', 'stream', 'recovery')",
            name="ck_reliability_migration_ledger_domain",
        ),
        sa.CheckConstraint("status = 'complete'", name="ck_reliability_migration_ledger_status"),
        sa.CheckConstraint("source_row_count >= 0 AND target_row_count >= 0", name="ck_reliability_migration_ledger_counts"),
        sa.ForeignKeyConstraint(["migration_run_id"], ["reliability_migration_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("migration_run_id", "domain"),
    )
    op.create_table(
        "reliability_cutover_state",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("migration_run_id", sa.Uuid(), nullable=True),
        sa.Column("empty_domain_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("source_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("active_run_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("quota_backfill_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("job_relation_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("audit_trigger_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("stream_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("recovery_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("final_schema_probe_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("schema_revision", sa.String(64), nullable=True),
        sa.Column("cutover_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_reliability_cutover_state_singleton"),
        sa.CheckConstraint(
            "stage IN ('expand_ready', 'empty_install', 'migration_ready', 'cutover_complete')",
            name="ck_reliability_cutover_state_stage",
        ),
        sa.CheckConstraint(
            "stage != 'cutover_complete' OR (((empty_domain_probe_complete AND migration_run_id IS NULL) OR "
            "(NOT empty_domain_probe_complete AND migration_run_id IS NOT NULL)) AND source_probe_complete AND "
            "active_run_probe_complete AND quota_backfill_probe_complete AND job_relation_probe_complete AND "
            "audit_trigger_probe_complete AND stream_probe_complete AND recovery_probe_complete AND "
            "final_schema_probe_complete AND schema_revision IS NOT NULL AND cutover_at IS NOT NULL)",
            name="ck_reliability_cutover_state_complete",
        ),
        sa.ForeignKeyConstraint(["migration_run_id"], ["reliability_migration_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )


def _add_nullable_execution_columns() -> None:
    for column in (
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("execution_lease_token_hash", sa.CHAR(64), nullable=True),
        sa.Column("execution_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(64), nullable=True),
    ):
        op.add_column("runs", column)
    op.add_column("scheduled_task_runs", sa.Column("job_id", sa.Uuid(), nullable=True))


def _add_durable_stream_terminal_invariant() -> None:
    op.create_table(
        "thread_event_sequences",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column(
            "high_watermark",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "high_watermark >= 0",
            name="ck_thread_event_sequences_high_watermark",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id"],
            [
                "threads_meta.project_id",
                "threads_meta.owner_user_id",
                "threads_meta.thread_id",
            ],
            name="fk_thread_event_sequences_thread",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "owner_user_id",
            "thread_id",
        ),
    )
    op.execute(
        sa.text(
            """INSERT INTO thread_event_sequences
               (project_id,owner_user_id,thread_id,high_watermark)
               SELECT t.project_id,t.owner_user_id,t.thread_id,
                      COALESCE(MAX(e.seq),0)
               FROM threads_meta t
               LEFT JOIN run_events e
                 ON e.project_id=t.project_id
                AND e.owner_user_id=t.owner_user_id
                AND e.thread_id=t.thread_id
               GROUP BY t.project_id,t.owner_user_id,t.thread_id"""
        )
    )
    op.create_index(
        "uq_run_events_stream_terminal",
        "run_events",
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        unique=True,
        postgresql_where=sa.text(
            "category = 'stream' AND event_type = 'stream.end'",
        ),
    )


def upgrade() -> None:
    _create_worker_and_job_tables()
    _create_quota_tables()
    _create_audit_and_recovery_tables()
    _create_migration_control_tables()
    _add_nullable_execution_columns()
    _add_durable_stream_terminal_invariant()


def downgrade() -> None:
    raise RuntimeError("M6 reliability migration is forward-only; restore a verified pre-M6 database")
