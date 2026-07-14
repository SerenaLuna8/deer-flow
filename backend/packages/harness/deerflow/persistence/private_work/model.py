"""Final-state ORM models for project-private work and M4 migration control."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


def _scope_constraints(table: str) -> tuple[ForeignKeyConstraint, ...]:
    return (
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=f"fk_{table}_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=f"fk_{table}_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name=f"fk_{table}_project_membership",
            ondelete="RESTRICT",
        ),
    )


class RunAssetVersionRow(Base):
    __tablename__ = "run_asset_versions"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    dependency_order: Mapped[int] = mapped_column(primary_key=True)
    asset_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    catalog_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        PrimaryKeyConstraint(
            "project_id",
            "owner_user_id",
            "run_id",
            "asset_kind",
            "dependency_order",
            name="pk_run_asset_versions",
        ),
        *_scope_constraints("run_asset_versions"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_run_asset_versions_private_run",
            ondelete="CASCADE",
        ),
        CheckConstraint("asset_kind IN ('agent', 'skill', 'mcp')", name="ck_run_asset_versions_kind"),
        CheckConstraint("asset_scope IN ('system', 'project')", name="ck_run_asset_versions_scope"),
        CheckConstraint("dependency_order >= 0", name="ck_run_asset_versions_order"),
        CheckConstraint("catalog_generation >= 0", name="ck_run_asset_versions_generation"),
        CheckConstraint("payload_checksum ~ '^[0-9a-f]{64}$'", name="ck_run_asset_versions_checksum"),
    )


class RunMcpGrantSnapshotRow(Base):
    __tablename__ = "run_mcp_grant_snapshots"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mcp_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    credential_slot_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    credential_grant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    credential_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        PrimaryKeyConstraint(
            "project_id",
            "owner_user_id",
            "run_id",
            "mcp_version_id",
            "credential_slot_id",
            name="pk_run_mcp_grant_snapshots",
        ),
        *_scope_constraints("run_mcp_grant_snapshots"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_run_mcp_grant_snapshots_private_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["mcp_version_id"], ["mcp_server_versions.id"], name="fk_run_mcp_grant_snapshots_mcp_version", ondelete="RESTRICT"),
        ForeignKeyConstraint(["credential_slot_id"], ["mcp_version_credential_slots.id"], name="fk_run_mcp_grant_snapshots_slot", ondelete="RESTRICT"),
        ForeignKeyConstraint(["credential_grant_id"], ["credential_grants.id"], name="fk_run_mcp_grant_snapshots_grant", ondelete="RESTRICT"),
        ForeignKeyConstraint(["credential_version_id"], ["credential_versions.id"], name="fk_run_mcp_grant_snapshots_credential_version", ondelete="RESTRICT"),
    )


class PrivateFileRow(Base):
    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    logical_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False, default="application/octet-stream", server_default="application/octet-stream")
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="staging", server_default="staging")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    created_by_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        *_scope_constraints("files"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id"],
            ["threads_meta.project_id", "threads_meta.owner_user_id", "threads_meta.thread_id"],
            name="fk_files_private_thread",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "created_by_run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_files_created_by_private_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("project_id", "owner_user_id", "thread_id", "id", name="uq_files_private_scope"),
        CheckConstraint("kind IN ('upload', 'workspace', 'output')", name="ck_files_kind"),
        CheckConstraint("status IN ('staging', 'ready', 'deleted')", name="ck_files_status"),
        CheckConstraint("size >= 0", name="ck_files_size"),
        CheckConstraint("version >= 1", name="ck_files_version"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_files_sha256"),
        CheckConstraint(
            "logical_path <> '' AND left(logical_path, 1) <> '/' AND logical_path !~ '(^|/)\\.\\.(/|$)' AND logical_path !~ '^[A-Za-z]:'",
            name="ck_files_logical_path",
        ),
        Index(
            "uq_files_active_logical_path",
            "project_id",
            "owner_user_id",
            "thread_id",
            "logical_path",
            unique=True,
            postgresql_where=text("status != 'deleted'"),
        ),
    )


class PrivateFileChunkRow(Base):
    __tablename__ = "file_chunks"

    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), primary_key=True)
    chunk_index: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)

    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="ck_file_chunks_index"),
        CheckConstraint("size >= 0", name="ck_file_chunks_size"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_file_chunks_sha256"),
    )


class PrivateArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    artifact_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        *_scope_constraints("artifacts"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_artifacts_private_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "file_id"],
            ["files.project_id", "files.owner_user_id", "files.thread_id", "files.id"],
            name="fk_artifacts_private_file",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("project_id", "owner_user_id", "thread_id", "run_id", "id", name="uq_artifacts_private_scope"),
    )


class UserProjectMemoryRow(Base):
    __tablename__ = "user_project_memories"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False, default="default", server_default="default")
    context_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        *_scope_constraints("user_project_memories"),
        UniqueConstraint("project_id", "owner_user_id", "namespace", name="uq_user_project_memories_namespace"),
        UniqueConstraint("project_id", "owner_user_id", "id", name="uq_user_project_memories_private_scope"),
        CheckConstraint("namespace <> ''", name="ck_user_project_memories_namespace"),
        CheckConstraint("version >= 1", name="ck_user_project_memories_version"),
    )


class UserProjectMemoryFactRow(Base):
    __tablename__ = "user_project_memory_facts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    memory_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    source_thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        *_scope_constraints("user_project_memory_facts"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "memory_id"],
            ["user_project_memories.project_id", "user_project_memories.owner_user_id", "user_project_memories.id"],
            name="fk_user_project_memory_facts_memory",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "source_thread_id"],
            ["threads_meta.project_id", "threads_meta.owner_user_id", "threads_meta.thread_id"],
            name="fk_user_project_memory_facts_source_thread",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "source_thread_id", "source_run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_user_project_memory_facts_source_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint("content <> ''", name="ck_user_project_memory_facts_content"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_user_project_memory_facts_confidence"),
        CheckConstraint("source_run_id IS NULL OR source_thread_id IS NOT NULL", name="ck_user_project_memory_facts_source"),
    )


class PrivateWorkMigrationRunRow(Base):
    __tablename__ = "private_work_migration_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", server_default="running")
    source_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    owner_map_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    database_backup_proof_digest: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    legacy_source_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    checkpoint_marker_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    cross_scope_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("mode IN ('dry_run', 'execute')", name="ck_private_work_migration_runs_mode"),
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_private_work_migration_runs_status"),
        CheckConstraint("source_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_private_work_migration_runs_source"),
        CheckConstraint("owner_map_digest ~ '^[0-9a-f]{64}$'", name="ck_private_work_migration_runs_owner_map"),
        CheckConstraint(
            "database_backup_proof_digest IS NULL OR database_backup_proof_digest ~ '^[0-9a-f]{64}$'",
            name="ck_private_work_migration_runs_backup",
        ),
    )


class PrivateWorkMigrationLedgerRow(Base):
    __tablename__ = "private_work_migration_ledger"

    migration_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("private_work_migration_runs.id", ondelete="CASCADE"), primary_key=True)
    domain: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_key_hash: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    source_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    target_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="complete", server_default="complete")
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    byte_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("status = 'complete'", name="ck_private_work_migration_ledger_status"),
        CheckConstraint("source_key_hash ~ '^[0-9a-f]{64}$'", name="ck_private_work_migration_ledger_source_key"),
        CheckConstraint("source_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_private_work_migration_ledger_source"),
        CheckConstraint("target_digest ~ '^[0-9a-f]{64}$'", name="ck_private_work_migration_ledger_target"),
        CheckConstraint("row_count >= 0 AND byte_count >= 0", name="ck_private_work_migration_ledger_counts"),
    )


class PrivateWorkCutoverStateRow(Base):
    __tablename__ = "private_work_cutover_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1, server_default=text("1"))
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    migration_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("private_work_migration_runs.id", ondelete="RESTRICT"), nullable=True)
    empty_domain_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    checkpoint_marker_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    cutover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_private_work_cutover_state_singleton"),
        CheckConstraint(
            "stage IN ('empty_install', 'migration_ready', 'cutover_complete')",
            name="ck_private_work_cutover_state_stage",
        ),
        CheckConstraint(
            "stage != 'cutover_complete' OR cutover_at IS NOT NULL",
            name="ck_private_work_cutover_state_cutover_at",
        ),
    )
