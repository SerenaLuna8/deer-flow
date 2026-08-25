"""Final-state ORM models for project-private work."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    DDL,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    event,
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
    snapshot_schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    # Added by the Current Version migration; keep it physically last so
    # upgraded and freshly installed catalogs are identical.
    snapshot_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

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
        CheckConstraint(
            "snapshot_schema_version BETWEEN 2 AND 4",
            name="ck_run_asset_versions_snapshot_schema",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "run_id",
            "dependency_order",
            name="uq_run_asset_versions_dependency_order",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "thread_id",
            "run_id",
            "asset_kind",
            "dependency_order",
            "asset_scope",
            "asset_id",
            "version_id",
            "payload_checksum",
            "snapshot_schema_version",
            name="uq_run_asset_versions_runtime_exact",
        ),
        Index(
            "ix_run_asset_versions_legacy_project_skill",
            "project_id",
            "asset_id",
            "version_id",
            postgresql_where=text("asset_kind = 'skill' AND asset_scope = 'project' AND snapshot_schema_version IN (2, 3)"),
        ),
        Index(
            "ix_run_asset_versions_legacy_skill_version",
            "asset_id",
            "version_id",
            postgresql_where=text("asset_kind = 'skill' AND snapshot_schema_version IN (2, 3)"),
        ),
    )


class RunSkillVersionRefRow(Base):
    """Exact immutable Skill Version pin for one v4 Run asset parent."""

    __tablename__ = "run_skill_version_refs"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    dependency_order: Mapped[int] = mapped_column(primary_key=True)
    asset_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    skill_project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    file_count: Mapped[int] = mapped_column(nullable=False)
    content_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "project_id",
            "owner_user_id",
            "run_id",
            "asset_kind",
            "dependency_order",
            name="pk_run_skill_version_refs",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "run_id",
            "skill_id",
            "skill_version_id",
            name="uq_run_skill_version_refs_exact_version",
        ),
        CheckConstraint("asset_kind = 'skill'", name="ck_run_skill_version_refs_kind"),
        CheckConstraint(
            "snapshot_schema_version = 4",
            name="ck_run_skill_version_refs_schema",
        ),
        CheckConstraint(
            "asset_scope IN ('system', 'project')",
            name="ck_run_skill_version_refs_scope",
        ),
        CheckConstraint(
            "(asset_scope = 'system' AND skill_project_id IS NULL) OR (asset_scope = 'project' AND skill_project_id IS NOT NULL AND skill_project_id = project_id)",
            name="ck_run_skill_version_refs_scope_project",
        ),
        CheckConstraint(
            "dependency_order >= 0",
            name="ck_run_skill_version_refs_order",
        ),
        CheckConstraint(
            "payload_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_run_skill_version_refs_checksum",
        ),
        CheckConstraint(
            "file_count BETWEEN 1 AND 16384",
            name="ck_run_skill_version_refs_file_count",
        ),
        CheckConstraint(
            "content_size_bytes BETWEEN 0 AND 104857600",
            name="ck_run_skill_version_refs_content_size",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "owner_user_id",
                "thread_id",
                "run_id",
                "asset_kind",
                "dependency_order",
                "asset_scope",
                "skill_id",
                "skill_version_id",
                "payload_checksum",
                "snapshot_schema_version",
            ],
            [
                "run_asset_versions.project_id",
                "run_asset_versions.owner_user_id",
                "run_asset_versions.thread_id",
                "run_asset_versions.run_id",
                "run_asset_versions.asset_kind",
                "run_asset_versions.dependency_order",
                "run_asset_versions.asset_scope",
                "run_asset_versions.asset_id",
                "run_asset_versions.version_id",
                "run_asset_versions.payload_checksum",
                "run_asset_versions.snapshot_schema_version",
            ],
            name="fk_run_skill_version_refs_exact_run_asset",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["skill_id", "asset_scope"],
            ["skills.id", "skills.scope"],
            name="fk_run_skill_version_refs_skill_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["skill_project_id", "skill_id"],
            ["skills.project_id", "skills.id"],
            name="fk_run_skill_version_refs_project_skill",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "skill_id",
                "skill_version_id",
                "payload_checksum",
                "file_count",
                "content_size_bytes",
            ],
            [
                "skill_versions.skill_id",
                "skill_versions.id",
                "skill_versions.payload_checksum",
                "skill_versions.file_count",
                "skill_versions.content_size_bytes",
            ],
            name="fk_run_skill_version_refs_exact_version",
            ondelete="RESTRICT",
        ),
        Index("ix_run_skill_version_refs_version", "skill_version_id"),
        Index(
            "ix_run_skill_version_refs_skill_scope",
            "skill_id",
            "asset_scope",
        ),
        Index(
            "ix_run_skill_version_refs_project_skill",
            "skill_project_id",
            "skill_id",
        ),
    )


class RunMcpSecretSnapshotRow(Base):
    """Exact, secret-free MCP Generation references admitted for one Run."""

    __tablename__ = "run_mcp_secret_snapshots"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    mcp_server_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    slot_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    secret_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    secret_generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_generation_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        PrimaryKeyConstraint(
            "project_id",
            "owner_user_id",
            "run_id",
            "mcp_server_version_id",
            "slot_id",
            name="pk_run_mcp_secret_snapshots",
        ),
        *_scope_constraints("run_mcp_secret_snapshots"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_run_mcp_secret_snapshots_private_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["mcp_server_id", "mcp_server_version_id"],
            ["mcp_server_versions.mcp_server_id", "mcp_server_versions.id"],
            name="fk_run_mcp_secret_snapshots_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["mcp_server_version_id", "slot_id"],
            ["mcp_version_secret_slots.mcp_server_version_id", "mcp_version_secret_slots.id"],
            name="fk_run_mcp_secret_snapshots_slot",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "secret_revision >= 1",
            name="ck_run_mcp_secret_snapshots_revision",
        ),
        CheckConstraint(
            "secret_generation_digest ~ '^[0-9a-f]{64}$'",
            name="ck_run_mcp_secret_snapshots_generation_digest",
        ),
        Index(
            "ix_run_mcp_secret_snapshots_generation",
            secret_generation_id,
        ),
    )


class RunSkillSecretSnapshotRow(Base):
    """Exact, secret-free Skill Generation references admitted for one Run."""

    __tablename__ = "run_skill_secret_snapshots"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    secret_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    secret_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    secret_generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_generation_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    __table_args__ = (
        PrimaryKeyConstraint(
            "project_id",
            "owner_user_id",
            "run_id",
            "skill_version_id",
            "secret_name",
            name="pk_run_skill_secret_snapshots",
        ),
        *_scope_constraints("run_skill_secret_snapshots"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_run_skill_secret_snapshots_private_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name="ck_run_skill_secret_snapshots_secret_name",
        ),
        CheckConstraint(
            "secret_revision >= 1",
            name="ck_run_skill_secret_snapshots_revision",
        ),
        CheckConstraint(
            "secret_generation_digest ~ '^[0-9a-f]{64}$'",
            name="ck_run_skill_secret_snapshots_generation_digest",
        ),
        Index(
            "ix_run_skill_secret_snapshots_generation",
            secret_generation_id,
        ),
        Index(
            "ix_run_skill_secret_snapshots_private_run",
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


_CREATE_RUN_CLOSURE_SEAL_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_run_asset_closure_seal_transition()
RETURNS trigger AS $$
BEGIN
    IF NEW.asset_closure_sealed IS NOT DISTINCT FROM OLD.asset_closure_sealed
       OR (OLD.asset_closure_sealed IS FALSE
           AND NEW.asset_closure_sealed IS TRUE) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid Run asset closure seal transition'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql
"""

_CREATE_RUN_CLOSURE_CHILD_GATE_FUNCTION = """
CREATE OR REPLACE FUNCTION gate_run_closure_child_mutation()
RETURNS trigger AS $$
DECLARE
    exact_project_id uuid;
    exact_owner_user_id text;
    exact_thread_id text;
    exact_run_id text;
    closure_sealed boolean;
    run_found boolean := false;
    claimable_job_exists boolean := false;
    retention_authorized boolean := false;
    ref_parent_exists boolean := false;
BEGIN
    IF TG_OP = 'DELETE' THEN
        exact_project_id := OLD.project_id;
        exact_owner_user_id := OLD.owner_user_id;
        exact_thread_id := OLD.thread_id;
        exact_run_id := OLD.run_id;
    ELSE
        exact_project_id := NEW.project_id;
        exact_owner_user_id := NEW.owner_user_id;
        exact_thread_id := NEW.thread_id;
        exact_run_id := NEW.run_id;
    END IF;

    SELECT asset_closure_sealed
    INTO closure_sealed
    FROM runs
    WHERE project_id = exact_project_id
      AND owner_user_id = exact_owner_user_id
      AND thread_id = exact_thread_id
      AND run_id = exact_run_id
    FOR UPDATE;
    run_found := FOUND;

    IF TG_OP = 'DELETE' AND NOT run_found THEN
        RETURN OLD;
    END IF;
    IF NOT run_found THEN
        RAISE EXCEPTION 'Run closure child requires an exact Run'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'Run closure child rows are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        -- RetentionPurgeAuthority installs exact Run coordinates in a
        -- transaction-local temp table only after locked eligibility and
        -- quiescence verification.  This is deliberately not a blanket GUC.
        IF to_regclass('pg_temp.retention_purge_run_authority') IS NOT NULL THEN
            EXECUTE
                'SELECT EXISTS (
                     SELECT 1
                     FROM pg_temp.retention_purge_run_authority authority
                     WHERE authority.project_id = $1
                       AND authority.thread_id = $2
                       AND authority.run_id = $3
                       AND authority.purge_id IS NOT NULL
                       AND (
                           (authority.resource_kind = ''project''
                            AND authority.owner_user_id IS NULL)
                           OR
                           (authority.resource_kind IN
                                (''former_owner'', ''account'', ''run'')
                            AND authority.owner_user_id = $4)
                       )
                 )'
            INTO retention_authorized
            USING exact_project_id, exact_thread_id, exact_run_id,
                  exact_owner_user_id;
        END IF;
        IF retention_authorized AND TG_TABLE_NAME = 'run_skill_version_refs' THEN
            SELECT EXISTS (
                SELECT 1
                FROM run_asset_versions parent
                WHERE parent.project_id = OLD.project_id
                  AND parent.owner_user_id = OLD.owner_user_id
                  AND parent.thread_id = OLD.thread_id
                  AND parent.run_id = OLD.run_id
                  AND parent.asset_kind = OLD.asset_kind
                  AND parent.dependency_order = OLD.dependency_order
            ) INTO ref_parent_exists;
            IF ref_parent_exists THEN
                RAISE EXCEPTION 'Run Skill ref cannot be deleted independently'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END IF;
        IF retention_authorized THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'Run closure child deletion requires scoped retention authority'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF closure_sealed IS NOT FALSE
       OR current_setting('deerflow.run_asset_closure_assembly', true)
          IS DISTINCT FROM exact_run_id THEN
        RAISE EXCEPTION 'Run closure is not open for exact assembly'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM jobs
        WHERE project_id = exact_project_id
          AND owner_user_id = exact_owner_user_id
          AND run_id = exact_run_id
          AND (
              (status IN ('queued', 'retry_wait')
               AND available_at <= clock_timestamp())
              OR
              (status IN ('leased', 'running')
               AND lease_expires_at <= clock_timestamp())
          )
    ) INTO claimable_job_exists;
    IF claimable_job_exists THEN
        RAISE EXCEPTION 'claimable Job forbids Run closure assembly'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_CREATE_RUN_CLOSURE_VERIFY_FUNCTION = """
CREATE OR REPLACE FUNCTION verify_run_asset_closure()
RETURNS trigger AS $$
DECLARE
    current_run runs%ROWTYPE;
    asset run_asset_versions%ROWTYPE;
    asset_count bigint;
    minimum_dependency_order integer;
    max_dependency_order integer;
    ref_count bigint;
    ref_file_count integer;
    ref_content_size bigint;
    invalid_secret_identity boolean;
BEGIN
    SELECT * INTO current_run
    FROM runs
    WHERE run_id = NEW.run_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF current_run.asset_closure_sealed IS NOT TRUE THEN
        RAISE EXCEPTION 'Run asset closure must be sealed before commit'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT count(*), min(dependency_order), max(dependency_order)
    INTO asset_count, minimum_dependency_order, max_dependency_order
    FROM run_asset_versions
    WHERE project_id = current_run.project_id
      AND owner_user_id = current_run.owner_user_id
      AND thread_id = current_run.thread_id
      AND run_id = current_run.run_id;

    IF asset_count = 0 THEN
        SELECT EXISTS (
            SELECT 1 FROM run_skill_secret_snapshots secret
            WHERE secret.project_id = current_run.project_id
              AND secret.owner_user_id = current_run.owner_user_id
              AND secret.thread_id = current_run.thread_id
              AND secret.run_id = current_run.run_id
            UNION ALL
            SELECT 1 FROM run_mcp_secret_snapshots secret
            WHERE secret.project_id = current_run.project_id
              AND secret.owner_user_id = current_run.owner_user_id
              AND secret.thread_id = current_run.thread_id
              AND secret.run_id = current_run.run_id
        ) INTO invalid_secret_identity;
        IF invalid_secret_identity
           OR current_run.status NOT IN
                ('success', 'error', 'timeout', 'interrupted', 'deleted') THEN
            RAISE EXCEPTION 'only a terminal privacy-purged Run may have an empty closure'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NULL;
    END IF;

    IF minimum_dependency_order != 0
       OR max_dependency_order != asset_count - 1 THEN
        RAISE EXCEPTION 'Run asset dependency order must be globally continuous'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM run_asset_versions first_asset
        WHERE first_asset.project_id = current_run.project_id
          AND first_asset.owner_user_id = current_run.owner_user_id
          AND first_asset.thread_id = current_run.thread_id
          AND first_asset.run_id = current_run.run_id
          AND first_asset.dependency_order = 0
          AND first_asset.asset_kind = 'agent'
    ) THEN
        RAISE EXCEPTION 'Run asset closure must begin with an Agent'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM run_skill_secret_snapshots secret
        WHERE secret.project_id = current_run.project_id
          AND secret.owner_user_id = current_run.owner_user_id
          AND secret.thread_id = current_run.thread_id
          AND secret.run_id = current_run.run_id
          AND NOT EXISTS (
              SELECT 1
              FROM run_asset_versions parent
              WHERE parent.project_id = secret.project_id
                AND parent.owner_user_id = secret.owner_user_id
                AND parent.thread_id = secret.thread_id
                AND parent.run_id = secret.run_id
                AND parent.asset_kind = 'skill'
                AND parent.asset_id = secret.skill_id
                AND parent.version_id = secret.skill_version_id
          )
    ) INTO invalid_secret_identity;
    IF invalid_secret_identity THEN
        RAISE EXCEPTION 'Run Skill secret snapshot lacks its exact Skill parent'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM run_mcp_secret_snapshots secret
        WHERE secret.project_id = current_run.project_id
          AND secret.owner_user_id = current_run.owner_user_id
          AND secret.thread_id = current_run.thread_id
          AND secret.run_id = current_run.run_id
          AND NOT EXISTS (
              SELECT 1
              FROM run_asset_versions parent
              WHERE parent.project_id = secret.project_id
                AND parent.owner_user_id = secret.owner_user_id
                AND parent.thread_id = secret.thread_id
                AND parent.run_id = secret.run_id
                AND parent.asset_kind = 'mcp'
                AND parent.asset_id = secret.mcp_server_id
                AND parent.version_id = secret.mcp_server_version_id
          )
    ) INTO invalid_secret_identity;
    IF invalid_secret_identity THEN
        RAISE EXCEPTION 'Run MCP secret snapshot lacks its exact MCP parent'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    FOR asset IN
        SELECT *
        FROM run_asset_versions
        WHERE project_id = current_run.project_id
          AND owner_user_id = current_run.owner_user_id
          AND thread_id = current_run.thread_id
          AND run_id = current_run.run_id
        ORDER BY dependency_order
    LOOP
        IF jsonb_typeof(asset.snapshot_json) IS DISTINCT FROM 'object'
           OR jsonb_typeof(asset.snapshot_json->'schema_version') IS DISTINCT FROM 'number'
           OR jsonb_typeof(asset.snapshot_json->'kind') IS DISTINCT FROM 'string'
           OR jsonb_typeof(asset.snapshot_json->'scope') IS DISTINCT FROM 'string'
           OR jsonb_typeof(asset.snapshot_json->'asset_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(asset.snapshot_json->'version_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(asset.snapshot_json->'checksum') IS DISTINCT FROM 'string'
           OR jsonb_typeof(asset.snapshot_json->'catalog_generation') IS DISTINCT FROM 'number'
           OR jsonb_typeof(asset.snapshot_json->'dependency_version_ids') IS DISTINCT FROM 'array'
           OR asset.snapshot_json->>'schema_version' IS DISTINCT FROM asset.snapshot_schema_version::text
           OR asset.snapshot_json->>'kind' IS DISTINCT FROM asset.asset_kind
           OR asset.snapshot_json->>'scope' IS DISTINCT FROM asset.asset_scope
           OR asset.snapshot_json->>'asset_id' IS DISTINCT FROM asset.asset_id::text
           OR asset.snapshot_json->>'version_id' IS DISTINCT FROM asset.version_id::text
           OR asset.snapshot_json->>'checksum' IS DISTINCT FROM asset.payload_checksum
           OR asset.snapshot_json->>'catalog_generation' IS DISTINCT FROM asset.catalog_generation::text THEN
            RAISE EXCEPTION 'Run asset typed identity disagrees with snapshot JSON'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        IF asset.snapshot_schema_version = 4 AND asset.asset_kind != 'skill' THEN
            RAISE EXCEPTION 'Run asset schema v4 is reserved for Skill references'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        SELECT count(*), max(file_count), max(content_size_bytes)
        INTO ref_count, ref_file_count, ref_content_size
        FROM run_skill_version_refs ref
        WHERE ref.project_id = asset.project_id
          AND ref.owner_user_id = asset.owner_user_id
          AND ref.thread_id = asset.thread_id
          AND ref.run_id = asset.run_id
          AND ref.asset_kind = asset.asset_kind
          AND ref.dependency_order = asset.dependency_order;

        IF asset.asset_kind = 'skill' AND asset.snapshot_schema_version = 4 THEN
            IF ref_count != 1
               OR octet_length(asset.snapshot_json::text) > 262144
               OR asset.snapshot_json - 'schema_version' - 'kind' - 'scope'
                    - 'asset_id' - 'version_id' - 'checksum'
                    - 'catalog_generation' - 'dependency_version_ids'
                    - 'skill' != '{}'::jsonb
               OR jsonb_typeof(asset.snapshot_json->'skill') IS DISTINCT FROM 'object'
               OR (asset.snapshot_json->'skill') - 'source' - 'file_count'
                    - 'content_size_bytes' != '{}'::jsonb
               OR jsonb_typeof(asset.snapshot_json->'skill'->'source') IS DISTINCT FROM 'string'
               OR jsonb_typeof(asset.snapshot_json->'skill'->'file_count') IS DISTINCT FROM 'number'
               OR jsonb_typeof(asset.snapshot_json->'skill'->'content_size_bytes') IS DISTINCT FROM 'number'
               OR asset.snapshot_json->'skill'->>'source' IS DISTINCT FROM 'skill_version_ref'
               OR asset.snapshot_json->'skill'->>'file_count' IS DISTINCT FROM ref_file_count::text
               OR asset.snapshot_json->'skill'->>'content_size_bytes' IS DISTINCT FROM ref_content_size::text
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(asset.snapshot_json->'dependency_version_ids') value
                    WHERE jsonb_typeof(value) IS DISTINCT FROM 'string'
                       OR value #>> '{}' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               ) THEN
                RAISE EXCEPTION 'Run Skill v4 manifest and exact ref are incomplete'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        ELSIF ref_count != 0 THEN
            RAISE EXCEPTION 'only a Skill v4 parent may own an exact Skill ref'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

_RUN_CLOSURE_TRIGGER_DDL = (
    _CREATE_RUN_CLOSURE_SEAL_FUNCTION,
    _CREATE_RUN_CLOSURE_CHILD_GATE_FUNCTION,
    _CREATE_RUN_CLOSURE_VERIFY_FUNCTION,
    "CREATE TRIGGER trg_runs_asset_closure_seal_transition BEFORE UPDATE OF asset_closure_sealed ON runs FOR EACH ROW EXECUTE FUNCTION enforce_run_asset_closure_seal_transition()",
    "CREATE TRIGGER trg_run_asset_versions_closure_mutation BEFORE INSERT OR UPDATE OR DELETE ON run_asset_versions FOR EACH ROW EXECUTE FUNCTION gate_run_closure_child_mutation()",
    "CREATE TRIGGER trg_run_skill_version_refs_closure_mutation BEFORE INSERT OR UPDATE OR DELETE ON run_skill_version_refs FOR EACH ROW EXECUTE FUNCTION gate_run_closure_child_mutation()",
    "CREATE TRIGGER trg_run_skill_secret_snapshots_closure_mutation BEFORE INSERT OR UPDATE OR DELETE ON run_skill_secret_snapshots FOR EACH ROW EXECUTE FUNCTION gate_run_closure_child_mutation()",
    "CREATE TRIGGER trg_run_mcp_secret_snapshots_closure_mutation BEFORE INSERT OR UPDATE OR DELETE ON run_mcp_secret_snapshots FOR EACH ROW EXECUTE FUNCTION gate_run_closure_child_mutation()",
    "CREATE CONSTRAINT TRIGGER trg_runs_asset_closure_complete AFTER INSERT OR UPDATE ON runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION verify_run_asset_closure()",
)

_RUN_CLOSURE_TRIGGER_TABLES = frozenset(
    {
        "jobs",
        "runs",
        "run_asset_versions",
        "run_skill_version_refs",
        "run_skill_secret_snapshots",
        "run_mcp_secret_snapshots",
    }
)


def _install_run_closure_triggers(_target, connection, **kwargs) -> None:
    created_tables = {table.name for table in kwargs.get("tables", ())}
    if not _RUN_CLOSURE_TRIGGER_TABLES <= created_tables or connection.dialect.name != "postgresql":
        return
    for statement in _RUN_CLOSURE_TRIGGER_DDL:
        connection.execute(DDL(statement))


event.listen(Base.metadata, "after_create", _install_run_closure_triggers)
