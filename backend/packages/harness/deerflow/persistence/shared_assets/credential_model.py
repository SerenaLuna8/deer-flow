from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
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


class CredentialRow(Base):
    __tablename__ = "credentials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    credential_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    source_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "(scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)",
            name="ck_credentials_scope_project",
        ),
        CheckConstraint("status IN ('active', 'revoked')", name="ck_credentials_status"),
        CheckConstraint("version >= 1", name="ck_credentials_version"),
        UniqueConstraint("id", "scope", name="uq_credentials_id_scope"),
        UniqueConstraint("source_key", name="uq_credentials_source_key"),
        ForeignKeyConstraint(
            ["id", "current_version_id"],
            ["credential_versions.credential_id", "credential_versions.id"],
            name="fk_credentials_current_version",
            use_alter=True,
        ),
        Index(
            "uq_credentials_system_name",
            func.lower(name),
            unique=True,
            postgresql_where=text("scope = 'system'"),
        ),
        Index(
            "uq_credentials_project_name",
            project_id,
            func.lower(name),
            unique=True,
            postgresql_where=text("scope = 'project'"),
        ),
    )


class CredentialVersionRow(Base):
    __tablename__ = "credential_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    credential_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("credentials.id", ondelete="RESTRICT"), nullable=False)
    version_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    payload_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    payload_schema: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credential_versions.id", ondelete="RESTRICT"), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("version_number >= 1", name="ck_credential_versions_number"),
        CheckConstraint("status IN ('active', 'retired', 'revoked')", name="ck_credential_versions_status"),
        CheckConstraint("payload_schema_version >= 1", name="ck_credential_versions_payload_schema_version"),
        UniqueConstraint("credential_id", "version_number", name="uq_credential_versions_asset_number"),
        UniqueConstraint("credential_id", "id", name="uq_credential_versions_asset_id"),
    )


class CredentialEnvelopeRow(Base):
    __tablename__ = "credential_envelopes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    credential_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("credential_versions.id", ondelete="RESTRICT"), nullable=False)
    envelope_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    key_id: Mapped[str] = mapped_column(String(255), nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    rotated_from_envelope_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credential_envelopes.id", ondelete="RESTRICT"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("envelope_generation >= 1", name="ck_credential_envelopes_generation"),
        CheckConstraint("octet_length(nonce) = 12", name="ck_credential_envelopes_nonce_size"),
        CheckConstraint("octet_length(ciphertext) >= 16", name="ck_credential_envelopes_ciphertext_size"),
        UniqueConstraint("credential_version_id", "envelope_generation", name="uq_credential_envelopes_version_generation"),
        Index(
            "uq_credential_envelopes_active_version",
            credential_version_id,
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )


class CredentialGrantRow(Base):
    __tablename__ = "credential_grants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    mcp_server_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    credential_slot_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    credential_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("credential_versions.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["mcp_server_version_id", "credential_slot_id"],
            ["mcp_version_credential_slots.mcp_server_version_id", "mcp_version_credential_slots.id"],
            name="fk_credential_grants_slot_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("status IN ('active', 'revoked')", name="ck_credential_grants_status"),
        CheckConstraint("version >= 1", name="ck_credential_grants_version"),
        Index(
            "uq_credential_grants_active_slot",
            mcp_server_version_id,
            credential_slot_id,
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
