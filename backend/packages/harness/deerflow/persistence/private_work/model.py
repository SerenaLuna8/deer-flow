"""Final-state ORM models for project-private work."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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


class RunSkillCredentialSnapshotRow(Base):
    """Secret-free, immutable Skill credential references admitted for one Run."""

    __tablename__ = "run_skill_credential_snapshots"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    secret_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    skill_credential_binding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        nullable=False,
    )
    binding_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credential_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    credential_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    # New migration columns stay physically last so upgraded and fresh
    # PostgreSQL catalogs have identical attribute order.
    source_env_field_name: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "project_id",
            "owner_user_id",
            "run_id",
            "skill_version_id",
            "secret_name",
            name="pk_run_skill_credential_snapshots",
        ),
        *_scope_constraints("run_skill_credential_snapshots"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_run_skill_credential_snapshots_private_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name="ck_run_skill_credential_snapshots_secret_name",
        ),
        CheckConstraint(
            "length(source_env_field_name) BETWEEN 1 AND 255",
            name="ck_run_skill_credential_snapshots_source_env_field_name",
        ),
        CheckConstraint(
            "binding_revision >= 1",
            name="ck_run_skill_credential_snapshots_binding_revision",
        ),
        Index(
            "ix_run_skill_credential_snapshots_binding",
            skill_credential_binding_id,
        ),
        Index(
            "ix_run_skill_credential_snapshots_private_run",
            project_id,
            owner_user_id,
            thread_id,
            run_id,
        ),
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
    # Revision 0010 adds this column after the original 0008/0009 file catalog.
    # Keep ORM create_all column order identical to the full schema snapshot.
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

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
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "source_file_id"],
            ["files.project_id", "files.owner_user_id", "files.thread_id", "files.id"],
            name="fk_files_private_source",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("project_id", "owner_user_id", "thread_id", "id", name="uq_files_private_scope"),
        CheckConstraint("kind IN ('upload', 'workspace', 'output')", name="ck_files_kind"),
        CheckConstraint("status IN ('staging', 'ready', 'deleted')", name="ck_files_status"),
        CheckConstraint("size >= 0", name="ck_files_size"),
        CheckConstraint("version >= 1", name="ck_files_version"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_files_sha256"),
        CheckConstraint("source_file_id IS NULL OR source_file_id <> id", name="ck_files_source_not_self"),
        CheckConstraint("source_file_id IS NULL OR kind = 'workspace'", name="ck_files_source_kind"),
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

    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "files.id",
            name="fk_file_chunks_file_id_files",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    chunk_index: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)

    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="ck_file_chunks_index"),
        CheckConstraint("size >= 0", name="ck_file_chunks_size"),
        CheckConstraint("size = octet_length(content)", name="ck_file_chunks_content_size"),
        CheckConstraint("size > 0 AND size <= 1048576", name="ck_file_chunks_bounded_size"),
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
    artifact_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
        Index(
            "ix_artifacts_private_active",
            "project_id",
            "owner_user_id",
            "thread_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )
