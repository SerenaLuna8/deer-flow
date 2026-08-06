"""Final-schema ORM rows for the system model catalog."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class SystemModelCatalogStateRow(Base):
    """Singleton revision and deterministic default-model pointer."""

    __tablename__ = "system_model_catalog_state"

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
    default_model_config_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
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
        CheckConstraint("id = 1", name="ck_system_model_catalog_state_singleton"),
        CheckConstraint(
            "revision >= 1",
            name="ck_system_model_catalog_state_revision",
        ),
        ForeignKeyConstraint(
            ["default_model_config_id"],
            ["system_model_configs.id"],
            name="fk_system_model_catalog_state_default_model",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )


class SystemModelConfigRow(Base):
    """Mutable logical identity and current immutable-version pointer."""

    __tablename__ = "system_model_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    logical_name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        server_default=text("''"),
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="suspended",
        server_default=text("'suspended'"),
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
    )
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    sort_order: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    created_by_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
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
            "status IN ('active', 'suspended')",
            name="ck_system_model_configs_status",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_system_model_configs_revision",
        ),
        CheckConstraint(
            "sort_order >= 0",
            name="ck_system_model_configs_sort_order",
        ),
        UniqueConstraint(
            "id",
            "current_version_id",
            name="uq_system_model_configs_id_current_version",
        ),
        ForeignKeyConstraint(
            ["id", "current_version_id"],
            [
                "system_model_config_versions.model_config_id",
                "system_model_config_versions.id",
            ],
            name="fk_system_model_configs_current_version",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        Index(
            "uq_system_model_configs_logical_name",
            func.lower(logical_name),
            unique=True,
        ),
        Index(
            "ix_system_model_configs_status_order",
            status,
            sort_order,
            id,
        ),
    )


class SystemModelConfigVersionRow(Base):
    """Immutable, secret-free provider configuration."""

    __tablename__ = "system_model_config_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    model_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("system_model_configs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_model: Mapped[str] = mapped_column(String(255), nullable=False)
    settings: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    supports_thinking: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    supports_reasoning_effort: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    supports_vision: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    credential_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
    )
    credential_env_key: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
    )
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
            "version_number >= 1",
            name="ck_system_model_config_versions_number",
        ),
        CheckConstraint(
            "jsonb_typeof(settings) = 'object'",
            name="ck_system_model_config_versions_settings_object",
        ),
        CheckConstraint(
            "payload_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_system_model_config_versions_checksum",
        ),
        CheckConstraint(
            "(credential_id IS NULL AND credential_version_id IS NULL AND credential_env_key IS NULL) OR (credential_id IS NOT NULL AND credential_version_id IS NOT NULL AND credential_env_key IS NOT NULL)",
            name="ck_system_model_config_versions_credential_group",
        ),
        CheckConstraint(
            "credential_env_key IS NULL OR credential_env_key ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name="ck_system_model_config_versions_env_key",
        ),
        UniqueConstraint(
            "model_config_id",
            "version_number",
            name="uq_system_model_config_versions_number",
        ),
        UniqueConstraint(
            "model_config_id",
            "id",
            name="uq_system_model_config_versions_model_id",
        ),
        UniqueConstraint(
            "model_config_id",
            "id",
            "payload_checksum",
            name="uq_system_model_config_versions_exact",
        ),
        UniqueConstraint(
            "model_config_id",
            "id",
            "payload_checksum",
            "credential_id",
            "credential_version_id",
            "credential_env_key",
            name="uq_system_model_config_versions_snapshot_closure",
        ),
        ForeignKeyConstraint(
            ["credential_id", "credential_version_id"],
            ["credential_versions.credential_id", "credential_versions.id"],
            name="fk_system_model_config_versions_credential_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["model_config_id", "supersedes_version_id"],
            [
                "system_model_config_versions.model_config_id",
                "system_model_config_versions.id",
            ],
            name="fk_system_model_config_versions_supersedes",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_system_model_config_versions_credential",
            credential_id,
            credential_version_id,
        ),
    )


class RunModelConfigSnapshotRow(Base):
    """Exact, secret-free model version admitted for one Run purpose."""

    __tablename__ = "run_model_config_snapshots"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(64), primary_key=True)
    logical_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_config_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    model_config_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        nullable=False,
    )
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    credential_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
    )
    credential_env_key: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "purpose ~ '^[a-z][a-z0-9._-]{0,63}$'",
            name="ck_run_model_config_snapshots_purpose",
        ),
        CheckConstraint(
            "payload_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_run_model_config_snapshots_checksum",
        ),
        CheckConstraint(
            "(credential_id IS NULL AND credential_version_id IS NULL AND credential_env_key IS NULL) OR (credential_id IS NOT NULL AND credential_version_id IS NOT NULL AND credential_env_key IS NOT NULL)",
            name="ck_run_model_config_snapshots_credential_group",
        ),
        CheckConstraint(
            "credential_env_key IS NULL OR credential_env_key ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name="ck_run_model_config_snapshots_env_key",
        ),
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_run_model_config_snapshots_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_run_model_config_snapshots_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_run_model_config_snapshots_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_run_model_config_snapshots_private_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["model_config_id", "model_config_version_id", "payload_checksum"],
            [
                "system_model_config_versions.model_config_id",
                "system_model_config_versions.id",
                "system_model_config_versions.payload_checksum",
            ],
            name="fk_run_model_config_snapshots_exact_model",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "model_config_id",
                "model_config_version_id",
                "payload_checksum",
                "credential_id",
                "credential_version_id",
                "credential_env_key",
            ],
            [
                "system_model_config_versions.model_config_id",
                "system_model_config_versions.id",
                "system_model_config_versions.payload_checksum",
                "system_model_config_versions.credential_id",
                "system_model_config_versions.credential_version_id",
                "system_model_config_versions.credential_env_key",
            ],
            name="fk_run_model_config_snapshots_model_credential",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_run_model_config_snapshots_private_run",
            project_id,
            owner_user_id,
            thread_id,
            run_id,
        ),
        Index(
            "ix_run_model_config_snapshots_model_version",
            model_config_id,
            model_config_version_id,
        ),
        Index(
            "ix_run_model_config_snapshots_credential",
            credential_id,
            credential_version_id,
        ),
    )


__all__ = [
    "RunModelConfigSnapshotRow",
    "SystemModelCatalogStateRow",
    "SystemModelConfigRow",
    "SystemModelConfigVersionRow",
]
