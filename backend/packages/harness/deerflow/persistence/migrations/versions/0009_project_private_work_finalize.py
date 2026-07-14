"""Finalize project-private work scope after explicit migration probes.

Revision ID: 0009_project_private_work_finalize
Revises: 0008_project_private_work_expand
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_project_private_work_finalize"
down_revision: str | Sequence[str] | None = "0008_project_private_work_expand"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


FINALIZE_LEDGER_DOMAINS: frozenset[str] = frozenset(
    {
        "threads",
        "runs",
        "run_events",
        "feedback",
        "checkpoints",
        "files",
        "memory",
        "channel_connections",
        "channel_oauth_states",
        "channel_conversations",
        "counts_probe",
        "scope_probe",
    }
)

_CORE_OWNER_TABLES = ("threads_meta", "runs", "run_events", "feedback")
_PRIVATE_SCOPE_TABLES = (
    "run_asset_versions",
    "run_mcp_grant_snapshots",
    "files",
    "artifacts",
    "user_project_memories",
    "user_project_memory_facts",
    "channel_connections",
    "channel_oauth_states",
    "channel_conversations",
)
_M4_BUSINESS_TABLES = (
    "run_asset_versions",
    "run_mcp_grant_snapshots",
    "files",
    "file_chunks",
    "artifacts",
    "user_project_memories",
    "user_project_memory_facts",
)


def _assert_finalize_prerequisites(bind) -> None:
    """Validate every staged prerequisite before the first schema mutation."""
    completed_run_id = bind.execute(
        sa.text(
            """SELECT id FROM private_work_migration_runs
            WHERE mode = 'execute'
              AND status = 'completed'
              AND completed_at IS NOT NULL
              AND legacy_source_probe_complete
              AND checkpoint_marker_probe_complete
              AND cross_scope_probe_complete
            ORDER BY completed_at DESC, id DESC
            LIMIT 1"""
        )
    ).scalar_one_or_none()
    if completed_run_id is None:
        raise RuntimeError("private-work finalize prerequisites are incomplete: completed migration run missing")

    completed_domains = set(
        bind.execute(
            sa.text(
                """SELECT DISTINCT domain FROM private_work_migration_ledger
                WHERE migration_run_id = :run_id AND status = 'complete'"""
            ),
            {"run_id": completed_run_id},
        ).scalars()
    )
    if not FINALIZE_LEDGER_DOMAINS <= completed_domains:
        raise RuntimeError("private-work finalize prerequisites are incomplete: migration ledger/probes missing")

    nullable_checks: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("threads_meta", ("project_id", "user_id", "agent_asset_id", "agent_scope", "checkpoint_delete_status", "version")),
        ("runs", ("project_id", "user_id", "finalization_status")),
        ("run_events", ("project_id", "user_id")),
        ("feedback", ("project_id", "user_id")),
        *((table, ("project_id", "owner_user_id")) for table in _PRIVATE_SCOPE_TABLES),
    )
    for table, columns in nullable_checks:
        predicate = " OR ".join(f'"{column}" IS NULL' for column in columns)
        has_null = bind.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}" WHERE {predicate} LIMIT 1)')).scalar_one()  # noqa: S608 - fixed internal identifiers
        if has_null:
            raise RuntimeError("private-work finalize prerequisites are incomplete: nullable private scope remains")


def _rename_owner_columns() -> None:
    op.drop_constraint("uq_feedback_thread_run_user", "feedback", type_="unique")
    for table in _CORE_OWNER_TABLES:
        op.drop_index(f"ix_{table}_user_id", table_name=table)
        op.alter_column(table, "user_id", new_column_name="owner_user_id", existing_type=sa.String(64))
        op.alter_column(
            table,
            "owner_user_id",
            existing_type=sa.String(64),
            type_=sa.String(36),
            existing_nullable=True,
        )
        op.create_index(f"ix_{table}_owner_user_id", table, ["owner_user_id"])

    for table in ("channel_connections", "channel_oauth_states", "channel_conversations"):
        op.alter_column(
            table,
            "owner_user_id",
            existing_type=sa.String(64),
            type_=sa.String(36),
            existing_nullable=False,
        )


def _make_private_scope_not_null() -> None:
    for table in (*_CORE_OWNER_TABLES, *_PRIVATE_SCOPE_TABLES):
        op.alter_column(table, "project_id", existing_type=sa.Uuid(), nullable=False)
        op.alter_column(table, "owner_user_id", existing_type=sa.String(36), nullable=False)

    for column, type_ in (
        ("agent_asset_id", sa.Uuid()),
        ("agent_scope", sa.String(16)),
        ("checkpoint_delete_status", sa.String(24)),
        ("version", sa.BigInteger()),
    ):
        op.alter_column("threads_meta", column, existing_type=type_, nullable=False)
    op.alter_column("runs", "finalization_status", existing_type=sa.String(20), nullable=False)


def _create_scope_constraints(table: str) -> None:
    op.create_foreign_key(f"fk_{table}_project", table, "projects", ["project_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(f"fk_{table}_owner", table, "users", ["owner_user_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(
        f"fk_{table}_project_membership",
        table,
        "project_memberships",
        ["project_id", "owner_user_id"],
        ["project_id", "user_id"],
        ondelete="RESTRICT",
    )


def _install_scope_checks_uniques_and_composite_foreign_keys() -> None:
    for table in (*_CORE_OWNER_TABLES, *_PRIVATE_SCOPE_TABLES):
        _create_scope_constraints(table)

    op.create_unique_constraint("uq_threads_meta_private_scope", "threads_meta", ["project_id", "owner_user_id", "thread_id"])
    op.create_foreign_key(
        "fk_threads_meta_agent_asset",
        "threads_meta",
        "agents",
        ["agent_asset_id", "agent_scope"],
        ["id", "scope"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint("ck_threads_meta_agent_scope", "threads_meta", "agent_scope IN ('system', 'project')")
    op.create_check_constraint(
        "ck_threads_meta_checkpoint_delete_status",
        "threads_meta",
        "checkpoint_delete_status IN ('not_requested', 'pending', 'complete', 'failed')",
    )
    op.create_check_constraint("ck_threads_meta_version", "threads_meta", "version >= 1")

    op.create_unique_constraint("uq_runs_private_scope", "runs", ["project_id", "owner_user_id", "thread_id", "run_id"])
    op.create_foreign_key(
        "fk_runs_private_thread",
        "runs",
        "threads_meta",
        ["project_id", "owner_user_id", "thread_id"],
        ["project_id", "owner_user_id", "thread_id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_runs_finalization_status",
        "runs",
        "finalization_status IN ('pending', 'finalizing', 'complete', 'failed')",
    )

    op.create_unique_constraint(
        "uq_run_events_private_seq",
        "run_events",
        ["project_id", "owner_user_id", "thread_id", "run_id", "seq"],
    )
    op.create_foreign_key(
        "fk_run_events_private_run",
        "run_events",
        "runs",
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ondelete="CASCADE",
    )

    op.create_unique_constraint(
        "uq_feedback_private_run_owner",
        "feedback",
        ["project_id", "owner_user_id", "thread_id", "run_id"],
    )
    op.create_foreign_key(
        "fk_feedback_private_run",
        "feedback",
        "runs",
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("pk_run_asset_versions", "run_asset_versions", type_="primary")
    op.create_primary_key(
        "pk_run_asset_versions",
        "run_asset_versions",
        ["project_id", "owner_user_id", "run_id", "asset_kind", "dependency_order"],
    )
    op.create_foreign_key(
        "fk_run_asset_versions_private_run",
        "run_asset_versions",
        "runs",
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint("ck_run_asset_versions_kind", "run_asset_versions", "asset_kind IN ('agent', 'skill', 'mcp')")
    op.create_check_constraint("ck_run_asset_versions_scope", "run_asset_versions", "asset_scope IN ('system', 'project')")
    op.create_check_constraint("ck_run_asset_versions_order", "run_asset_versions", "dependency_order >= 0")
    op.create_check_constraint("ck_run_asset_versions_generation", "run_asset_versions", "catalog_generation >= 0")
    op.create_check_constraint("ck_run_asset_versions_checksum", "run_asset_versions", "payload_checksum ~ '^[0-9a-f]{64}$'")

    op.drop_constraint("pk_run_mcp_grant_snapshots", "run_mcp_grant_snapshots", type_="primary")
    op.create_primary_key(
        "pk_run_mcp_grant_snapshots",
        "run_mcp_grant_snapshots",
        ["project_id", "owner_user_id", "run_id", "mcp_version_id", "credential_slot_id"],
    )
    op.create_foreign_key(
        "fk_run_mcp_grant_snapshots_private_run",
        "run_mcp_grant_snapshots",
        "runs",
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ondelete="CASCADE",
    )

    op.create_unique_constraint("uq_files_private_scope", "files", ["project_id", "owner_user_id", "thread_id", "id"])
    op.create_foreign_key(
        "fk_files_private_thread",
        "files",
        "threads_meta",
        ["project_id", "owner_user_id", "thread_id"],
        ["project_id", "owner_user_id", "thread_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_files_created_by_private_run",
        "files",
        "runs",
        ["project_id", "owner_user_id", "thread_id", "created_by_run_id"],
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint("ck_files_kind", "files", "kind IN ('upload', 'workspace', 'output')")
    op.create_check_constraint("ck_files_status", "files", "status IN ('staging', 'ready', 'deleted')")
    op.create_check_constraint("ck_files_size", "files", "size >= 0")
    op.create_check_constraint("ck_files_version", "files", "version >= 1")
    op.create_check_constraint("ck_files_sha256", "files", "sha256 ~ '^[0-9a-f]{64}$'")
    op.create_check_constraint(
        "ck_files_logical_path",
        "files",
        "logical_path <> '' AND left(logical_path, 1) <> '/' AND logical_path !~ '(^|/)\\.\\.(/|$)' AND logical_path !~ '^[A-Za-z]:'",
    )
    op.create_index(
        "uq_files_active_logical_path",
        "files",
        ["project_id", "owner_user_id", "thread_id", "logical_path"],
        unique=True,
        postgresql_where=sa.text("status != 'deleted'"),
    )
    op.create_check_constraint("ck_file_chunks_index", "file_chunks", "chunk_index >= 0")
    op.create_check_constraint("ck_file_chunks_size", "file_chunks", "size >= 0")
    op.create_check_constraint("ck_file_chunks_sha256", "file_chunks", "sha256 ~ '^[0-9a-f]{64}$'")

    op.create_unique_constraint(
        "uq_artifacts_private_scope",
        "artifacts",
        ["project_id", "owner_user_id", "thread_id", "run_id", "id"],
    )
    op.create_foreign_key(
        "fk_artifacts_private_run",
        "artifacts",
        "runs",
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_artifacts_private_file",
        "artifacts",
        "files",
        ["project_id", "owner_user_id", "thread_id", "file_id"],
        ["project_id", "owner_user_id", "thread_id", "id"],
        ondelete="RESTRICT",
    )

    op.create_unique_constraint(
        "uq_user_project_memories_namespace",
        "user_project_memories",
        ["project_id", "owner_user_id", "namespace"],
    )
    op.create_unique_constraint(
        "uq_user_project_memories_private_scope",
        "user_project_memories",
        ["project_id", "owner_user_id", "id"],
    )
    op.create_check_constraint("ck_user_project_memories_namespace", "user_project_memories", "namespace <> ''")
    op.create_check_constraint("ck_user_project_memories_version", "user_project_memories", "version >= 1")

    op.drop_constraint("fk_user_project_memory_facts_memory_id", "user_project_memory_facts", type_="foreignkey")
    op.create_foreign_key(
        "fk_user_project_memory_facts_memory",
        "user_project_memory_facts",
        "user_project_memories",
        ["project_id", "owner_user_id", "memory_id"],
        ["project_id", "owner_user_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_user_project_memory_facts_source_thread",
        "user_project_memory_facts",
        "threads_meta",
        ["project_id", "owner_user_id", "source_thread_id"],
        ["project_id", "owner_user_id", "thread_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_user_project_memory_facts_source_run",
        "user_project_memory_facts",
        "runs",
        ["project_id", "owner_user_id", "source_thread_id", "source_run_id"],
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint("ck_user_project_memory_facts_content", "user_project_memory_facts", "content <> ''")
    op.create_check_constraint(
        "ck_user_project_memory_facts_confidence",
        "user_project_memory_facts",
        "confidence >= 0 AND confidence <= 1",
    )
    op.create_check_constraint(
        "ck_user_project_memory_facts_source",
        "user_project_memory_facts",
        "source_run_id IS NULL OR source_thread_id IS NOT NULL",
    )

    op.drop_constraint("uq_channel_connection_owner_provider_identity", "channel_connections", type_="unique")
    op.create_unique_constraint(
        "uq_channel_connection_owner_provider_identity",
        "channel_connections",
        ["project_id", "owner_user_id", "provider", "external_account_id", "workspace_id"],
    )
    op.create_unique_constraint(
        "uq_channel_connections_private_scope",
        "channel_connections",
        ["project_id", "owner_user_id", "id"],
    )
    op.create_check_constraint(
        "ck_channel_connections_status",
        "channel_connections",
        "status IN ('connected', 'frozen', 'revoked')",
    )
    op.drop_index("uq_channel_connection_active_identity", table_name="channel_connections")
    op.create_index(
        "uq_channel_connection_active_identity",
        "channel_connections",
        ["provider", "external_account_id", "workspace_id"],
        unique=True,
        postgresql_where=sa.text("status = 'connected'"),
    )

    op.create_foreign_key(
        "fk_channel_conversations_private_connection",
        "channel_conversations",
        "channel_connections",
        ["project_id", "owner_user_id", "connection_id"],
        ["project_id", "owner_user_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_channel_conversations_private_thread",
        "channel_conversations",
        "threads_meta",
        ["project_id", "owner_user_id", "thread_id"],
        ["project_id", "owner_user_id", "thread_id"],
        ondelete="CASCADE",
    )

    for index, table in (
        ("ix_run_asset_versions_staging_scope", "run_asset_versions"),
        ("ix_run_mcp_grant_snapshots_staging_scope", "run_mcp_grant_snapshots"),
        ("ix_files_staging_scope", "files"),
        ("ix_artifacts_staging_scope", "artifacts"),
        ("ix_user_project_memories_staging_scope", "user_project_memories"),
        ("ix_user_project_memory_facts_staging_scope", "user_project_memory_facts"),
    ):
        op.drop_index(index, table_name=table)


def upgrade() -> None:
    bind = op.get_bind()
    _assert_finalize_prerequisites(bind)
    _rename_owner_columns()
    _make_private_scope_not_null()
    _install_scope_checks_uniques_and_composite_foreign_keys()


def _assert_finalize_downgrade_safe() -> None:
    bind = op.get_bind()
    for table in _M4_BUSINESS_TABLES:
        has_data = bind.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}" LIMIT 1)')).scalar_one()  # noqa: S608 - fixed internal identifiers
        if has_data:
            raise RuntimeError("cannot downgrade M4 finalize while private-work data exists")
    for table in ("channel_connections", "channel_oauth_states", "channel_conversations"):
        has_data = bind.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}" LIMIT 1)')).scalar_one()  # noqa: S608 - fixed internal identifiers
        if has_data:
            raise RuntimeError("cannot downgrade M4 finalize while scoped channel data exists")


def downgrade() -> None:
    _assert_finalize_downgrade_safe()

    for index, table in (
        ("uq_files_active_logical_path", "files"),
        ("uq_channel_connection_active_identity", "channel_connections"),
    ):
        op.drop_index(index, table_name=table)

    constraints: tuple[tuple[str, str, str], ...] = (
        ("fk_channel_conversations_private_thread", "channel_conversations", "foreignkey"),
        ("fk_channel_conversations_private_connection", "channel_conversations", "foreignkey"),
        ("ck_channel_connections_status", "channel_connections", "check"),
        ("uq_channel_connections_private_scope", "channel_connections", "unique"),
        ("uq_channel_connection_owner_provider_identity", "channel_connections", "unique"),
        ("ck_user_project_memory_facts_source", "user_project_memory_facts", "check"),
        ("ck_user_project_memory_facts_confidence", "user_project_memory_facts", "check"),
        ("ck_user_project_memory_facts_content", "user_project_memory_facts", "check"),
        ("fk_user_project_memory_facts_source_run", "user_project_memory_facts", "foreignkey"),
        ("fk_user_project_memory_facts_source_thread", "user_project_memory_facts", "foreignkey"),
        ("fk_user_project_memory_facts_memory", "user_project_memory_facts", "foreignkey"),
        ("ck_user_project_memories_version", "user_project_memories", "check"),
        ("ck_user_project_memories_namespace", "user_project_memories", "check"),
        ("uq_user_project_memories_private_scope", "user_project_memories", "unique"),
        ("uq_user_project_memories_namespace", "user_project_memories", "unique"),
        ("fk_artifacts_private_file", "artifacts", "foreignkey"),
        ("fk_artifacts_private_run", "artifacts", "foreignkey"),
        ("uq_artifacts_private_scope", "artifacts", "unique"),
        ("ck_file_chunks_sha256", "file_chunks", "check"),
        ("ck_file_chunks_size", "file_chunks", "check"),
        ("ck_file_chunks_index", "file_chunks", "check"),
        ("ck_files_logical_path", "files", "check"),
        ("ck_files_sha256", "files", "check"),
        ("ck_files_version", "files", "check"),
        ("ck_files_size", "files", "check"),
        ("ck_files_status", "files", "check"),
        ("ck_files_kind", "files", "check"),
        ("fk_files_created_by_private_run", "files", "foreignkey"),
        ("fk_files_private_thread", "files", "foreignkey"),
        ("uq_files_private_scope", "files", "unique"),
        ("fk_run_mcp_grant_snapshots_private_run", "run_mcp_grant_snapshots", "foreignkey"),
        ("ck_run_asset_versions_checksum", "run_asset_versions", "check"),
        ("ck_run_asset_versions_generation", "run_asset_versions", "check"),
        ("ck_run_asset_versions_order", "run_asset_versions", "check"),
        ("ck_run_asset_versions_scope", "run_asset_versions", "check"),
        ("ck_run_asset_versions_kind", "run_asset_versions", "check"),
        ("fk_run_asset_versions_private_run", "run_asset_versions", "foreignkey"),
        ("fk_feedback_private_run", "feedback", "foreignkey"),
        ("uq_feedback_private_run_owner", "feedback", "unique"),
        ("fk_run_events_private_run", "run_events", "foreignkey"),
        ("uq_run_events_private_seq", "run_events", "unique"),
        ("ck_runs_finalization_status", "runs", "check"),
        ("fk_runs_private_thread", "runs", "foreignkey"),
        ("uq_runs_private_scope", "runs", "unique"),
        ("ck_threads_meta_version", "threads_meta", "check"),
        ("ck_threads_meta_checkpoint_delete_status", "threads_meta", "check"),
        ("ck_threads_meta_agent_scope", "threads_meta", "check"),
        ("fk_threads_meta_agent_asset", "threads_meta", "foreignkey"),
        ("uq_threads_meta_private_scope", "threads_meta", "unique"),
    )
    for name, table, type_ in constraints:
        op.drop_constraint(name, table, type_=type_)

    for table in reversed((*_CORE_OWNER_TABLES, *_PRIVATE_SCOPE_TABLES)):
        for suffix in ("project_membership", "owner", "project"):
            op.drop_constraint(f"fk_{table}_{suffix}", table, type_="foreignkey")

    op.drop_constraint("pk_run_mcp_grant_snapshots", "run_mcp_grant_snapshots", type_="primary")
    op.create_primary_key(
        "pk_run_mcp_grant_snapshots",
        "run_mcp_grant_snapshots",
        ["run_id", "mcp_version_id", "credential_slot_id"],
    )
    op.drop_constraint("pk_run_asset_versions", "run_asset_versions", type_="primary")
    op.create_primary_key("pk_run_asset_versions", "run_asset_versions", ["run_id", "asset_kind", "dependency_order"])
    op.create_foreign_key(
        "fk_user_project_memory_facts_memory_id",
        "user_project_memory_facts",
        "user_project_memories",
        ["memory_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.create_unique_constraint(
        "uq_channel_connection_owner_provider_identity",
        "channel_connections",
        ["owner_user_id", "provider", "external_account_id", "workspace_id"],
    )
    op.create_index(
        "uq_channel_connection_active_identity",
        "channel_connections",
        ["provider", "external_account_id", "workspace_id"],
        unique=True,
        postgresql_where=sa.text("status != 'revoked'"),
    )

    for table in (*_CORE_OWNER_TABLES, *_PRIVATE_SCOPE_TABLES):
        op.alter_column(table, "project_id", existing_type=sa.Uuid(), nullable=True)
        owner_nullable = table not in {
            "channel_connections",
            "channel_oauth_states",
            "channel_conversations",
        }
        op.alter_column(table, "owner_user_id", existing_type=sa.String(36), nullable=owner_nullable)

    for column, type_ in (
        ("agent_asset_id", sa.Uuid()),
        ("agent_scope", sa.String(16)),
        ("checkpoint_delete_status", sa.String(24)),
        ("version", sa.BigInteger()),
    ):
        op.alter_column("threads_meta", column, existing_type=type_, nullable=True)
    op.alter_column("runs", "finalization_status", existing_type=sa.String(20), nullable=True)

    for table in _CORE_OWNER_TABLES:
        op.drop_index(f"ix_{table}_owner_user_id", table_name=table)
        op.alter_column(table, "owner_user_id", existing_type=sa.String(36), type_=sa.String(64), existing_nullable=True)
        op.alter_column(table, "owner_user_id", new_column_name="user_id", existing_type=sa.String(64))
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])
    op.create_unique_constraint("uq_feedback_thread_run_user", "feedback", ["thread_id", "run_id", "user_id"])

    for table in ("channel_connections", "channel_oauth_states", "channel_conversations"):
        op.alter_column(
            table,
            "owner_user_id",
            existing_type=sa.String(36),
            type_=sa.String(64),
            existing_nullable=False,
        )

    for index, table, columns in (
        ("ix_run_asset_versions_staging_scope", "run_asset_versions", ["project_id", "owner_user_id", "run_id"]),
        ("ix_run_mcp_grant_snapshots_staging_scope", "run_mcp_grant_snapshots", ["project_id", "owner_user_id", "run_id"]),
        ("ix_files_staging_scope", "files", ["project_id", "owner_user_id", "thread_id"]),
        ("ix_artifacts_staging_scope", "artifacts", ["project_id", "owner_user_id", "thread_id", "run_id"]),
        ("ix_user_project_memories_staging_scope", "user_project_memories", ["project_id", "owner_user_id"]),
        ("ix_user_project_memory_facts_staging_scope", "user_project_memory_facts", ["project_id", "owner_user_id", "memory_id"]),
    ):
        op.create_index(index, table, columns)
