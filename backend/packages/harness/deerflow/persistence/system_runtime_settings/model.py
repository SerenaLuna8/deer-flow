"""Final-schema rows for immutable, database-backed runtime policy."""

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
    SmallInteger,
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


class SystemRuntimePolicyCatalogStateRow(Base):
    """Singleton revision for all runtime-policy sections."""

    __tablename__ = "system_runtime_policy_catalog_state"

    id: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        default=1,
        server_default=text("1"),
    )
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    updated_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        onupdate=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_system_runtime_policy_catalog_state_singleton"),
        CheckConstraint("revision >= 1", name="ck_system_runtime_policy_catalog_state_revision"),
    )


class SystemRuntimePolicyRow(Base):
    """Current immutable version pointer for one policy section."""

    __tablename__ = "system_runtime_policies"

    section: Mapped[str] = mapped_column(String(32), primary_key=True)
    current_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    updated_by_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        onupdate=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "section IN ('agent_runtime', 'auth', 'memory_document', 'quotas', 'workflow_runtime')",
            name="ck_system_runtime_policies_section",
        ),
        CheckConstraint("revision >= 1", name="ck_system_runtime_policies_revision"),
        UniqueConstraint(
            "section",
            "current_version_id",
            name="uq_system_runtime_policies_current_version",
        ),
        ForeignKeyConstraint(
            ["section", "current_version_id"],
            [
                "system_runtime_policy_versions.section",
                "system_runtime_policy_versions.id",
            ],
            name="fk_system_runtime_policies_current_version",
            ondelete="RESTRICT",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    )


class SystemRuntimePolicyVersionRow(Base):
    """Append-only, canonical and secret-free policy payload."""

    __tablename__ = "system_runtime_policy_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    section: Mapped[str] = mapped_column(String(32), nullable=False)
    version_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    value: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "section IN ('agent_runtime', 'auth', 'memory_document', 'quotas', 'workflow_runtime')",
            name="ck_system_runtime_policy_versions_section",
        ),
        CheckConstraint("version_number >= 1", name="ck_system_runtime_policy_versions_number"),
        CheckConstraint("schema_version >= 1", name="ck_system_runtime_policy_versions_schema"),
        CheckConstraint("jsonb_typeof(value) = 'object'", name="ck_system_runtime_policy_versions_value_object"),
        CheckConstraint(
            "payload_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_system_runtime_policy_versions_checksum",
        ),
        UniqueConstraint(
            "section",
            "version_number",
            name="uq_system_runtime_policy_versions_number",
        ),
        UniqueConstraint(
            "section",
            "id",
            name="uq_system_runtime_policy_versions_section_id",
        ),
        UniqueConstraint(
            "section",
            "id",
            "schema_version",
            "payload_checksum",
            name="uq_system_runtime_policy_versions_exact",
        ),
        UniqueConstraint(
            "section",
            "id",
            "version_number",
            "schema_version",
            "payload_checksum",
            name="uq_system_runtime_policy_versions_revision_exact",
        ),
        ForeignKeyConstraint(
            ["section"],
            ["system_runtime_policies.section"],
            name="fk_system_runtime_policy_versions_policy",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["section", "supersedes_version_id"],
            [
                "system_runtime_policy_versions.section",
                "system_runtime_policy_versions.id",
            ],
            name="fk_system_runtime_policy_versions_supersedes",
            ondelete="RESTRICT",
        ),
        Index("ix_system_runtime_policy_versions_created_at", section, created_at),
    )


class RunRuntimePolicySnapshotRow(Base):
    """Exact immutable runtime policy admitted with a private Run."""

    __tablename__ = "run_runtime_policy_snapshots"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    section: Mapped[str] = mapped_column(String(32), primary_key=True)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "section = 'agent_runtime'",
            name="ck_run_runtime_policy_snapshots_section",
        ),
        CheckConstraint("schema_version >= 1", name="ck_run_runtime_policy_snapshots_schema"),
        CheckConstraint(
            "payload_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_run_runtime_policy_snapshots_checksum",
        ),
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_run_runtime_policy_snapshots_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_run_runtime_policy_snapshots_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_run_runtime_policy_snapshots_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_run_runtime_policy_snapshots_private_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "section",
                "policy_version_id",
                "schema_version",
                "payload_checksum",
            ],
            [
                "system_runtime_policy_versions.section",
                "system_runtime_policy_versions.id",
                "system_runtime_policy_versions.schema_version",
                "system_runtime_policy_versions.payload_checksum",
            ],
            name="fk_run_runtime_policy_snapshots_exact_policy",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "run_id",
            "section",
            "policy_version_id",
            "schema_version",
            "payload_checksum",
            name="uq_run_runtime_policy_snapshots_exact",
        ),
        Index(
            "ix_run_runtime_policy_snapshots_private_run",
            project_id,
            owner_user_id,
            thread_id,
            run_id,
        ),
    )


__all__ = [
    "RunRuntimePolicySnapshotRow",
    "SystemRuntimePolicyCatalogStateRow",
    "SystemRuntimePolicyRow",
    "SystemRuntimePolicyVersionRow",
]
