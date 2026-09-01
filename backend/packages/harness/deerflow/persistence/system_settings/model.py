"""Schema V1 ORM rows for stable System Model configurations."""

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
    LargeBinary,
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
    """Stable mutable model identity and its current domain-owned API Key."""

    __tablename__ = "system_model_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="suspended",
        server_default=text("'suspended'"),
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider_adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_model: Mapped[str] = mapped_column(String(255), nullable=False)
    max_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
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
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    current_secret_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
    )
    secret_revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
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
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
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
        CheckConstraint(
            "status IN ('active', 'suspended')",
            name="ck_system_model_configs_status",
        ),
        CheckConstraint(
            "deleted_at IS NULL OR status = 'suspended'",
            name="ck_system_model_configs_deleted_state",
        ),
        CheckConstraint(
            "jsonb_typeof(settings) = 'object'",
            name="ck_system_model_configs_settings_object",
        ),
        CheckConstraint(
            "max_input_tokens BETWEEN 1 AND 2000000",
            name="ck_system_model_configs_max_input_tokens",
        ),
        CheckConstraint(
            "payload_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_system_model_configs_checksum",
        ),
        CheckConstraint(
            "secret_revision >= 0",
            name="ck_system_model_configs_secret_revision",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_system_model_configs_revision",
        ),
        ForeignKeyConstraint(
            ["id", "current_secret_generation_id"],
            [
                "system_model_secret_generations.model_config_id",
                "system_model_secret_generations.id",
            ],
            name="fk_system_model_configs_current_secret_generation",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        # ``model_providers`` is registered by the model-registry module and
        # created later in the Schema V1 snapshot, so the binding is a named
        # ALTER TABLE constraint rather than an inline column reference.
        ForeignKeyConstraint(
            ["provider_id"],
            ["model_providers.id"],
            name="fk_system_model_configs_provider",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        Index(
            "ix_system_model_configs_status_created",
            status,
            created_at.desc(),
            id.desc(),
            postgresql_where=deleted_at.is_(None),
        ),
        Index(
            "ix_system_model_configs_provider",
            provider_id,
        ),
    )


class SystemModelSecretGenerationRow(Base):
    """One materializable API Key generation owned by one System Model."""

    __tablename__ = "system_model_secret_generations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("system_model_configs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    envelope_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
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
            "revision >= 1",
            name="ck_system_model_secret_generations_revision",
        ),
        CheckConstraint(
            "octet_length(nonce) = 12",
            name="ck_system_model_secret_generations_nonce_size",
        ),
        CheckConstraint(
            "octet_length(ciphertext) >= 16",
            name="ck_system_model_secret_generations_ciphertext_size",
        ),
        CheckConstraint(
            "envelope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_system_model_secret_generations_digest",
        ),
        UniqueConstraint(
            "model_config_id",
            "revision",
            name="uq_system_model_secret_generations_revision",
        ),
        UniqueConstraint(
            "model_config_id",
            "id",
            name="uq_system_model_secret_generations_model_id",
        ),
    )


class SystemModelSecretTombstoneRow(Base):
    """Secret-free history for a destroyed System Model generation."""

    __tablename__ = "system_model_secret_tombstones"

    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    model_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("system_model_configs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    envelope_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    destroyed_by_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    destroyed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "revision >= 1",
            name="ck_system_model_secret_tombstones_revision",
        ),
        CheckConstraint(
            "envelope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_system_model_secret_tombstones_digest",
        ),
        CheckConstraint(
            "reason IN ('replaced', 'cleared', 'recipient_changed')",
            name="ck_system_model_secret_tombstones_reason",
        ),
        UniqueConstraint(
            "model_config_id",
            "revision",
            name="uq_system_model_secret_tombstones_revision",
        ),
    )


class RunModelConfigSnapshotRow(Base):
    """Exact secret-free model payload admitted for one Run purpose."""

    __tablename__ = "run_model_config_snapshots"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_config_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider_payload: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
    )
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    secret_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
    )
    secret_envelope_digest: Mapped[str | None] = mapped_column(
        CHAR(64),
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
            "jsonb_typeof(provider_payload) = 'object'",
            name="ck_run_model_config_snapshots_provider_payload",
        ),
        CheckConstraint(
            "payload_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_run_model_config_snapshots_checksum",
        ),
        CheckConstraint(
            "(secret_generation_id IS NULL AND secret_envelope_digest IS NULL) OR (secret_generation_id IS NOT NULL AND secret_envelope_digest IS NOT NULL)",
            name="ck_run_model_config_snapshots_secret_group",
        ),
        CheckConstraint(
            "secret_envelope_digest IS NULL OR secret_envelope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_run_model_config_snapshots_secret_digest",
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
            ["model_config_id"],
            ["system_model_configs.id"],
            name="fk_run_model_config_snapshots_model",
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
            "ix_run_model_config_snapshots_model",
            model_config_id,
        ),
        Index(
            "ix_run_model_config_snapshots_secret_generation",
            secret_generation_id,
        ),
    )


__all__ = [
    "RunModelConfigSnapshotRow",
    "SystemModelCatalogStateRow",
    "SystemModelConfigRow",
    "SystemModelSecretGenerationRow",
    "SystemModelSecretTombstoneRow",
]
