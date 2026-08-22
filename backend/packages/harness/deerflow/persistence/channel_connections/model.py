"""ORM models for user-owned IM channel connections."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ProjectChannelInstanceRow(Base):
    """One dynamically managed provider application for one project.

    ``public_config`` is deliberately separate from the domain-owned protected
    secret bundle.
    Provider secrets never belong in this row.
    """

    __tablename__ = "project_channel_instances"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    desired_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="disabled",
        server_default="disabled",
    )
    observed_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="stopped",
        server_default="stopped",
    )
    public_config: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    provider_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    updated_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
        server_default=text("now()"),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_project_channel_instances"),
        CheckConstraint(
            "provider ~ '^[a-z][a-z0-9_-]{0,31}$'",
            name="ck_project_channel_instances_provider",
        ),
        CheckConstraint(
            "desired_status IN ('enabled', 'disabled')",
            name="ck_project_channel_instances_desired_status",
        ),
        CheckConstraint(
            "observed_status IN ('stopped', 'starting', 'running', 'stopping', 'error')",
            name="ck_project_channel_instances_observed_status",
        ),
        CheckConstraint(
            "jsonb_typeof(public_config) = 'object' AND public_config::text !~* '\"[^\"]*(secret|token|password|api_key|private_key)[^\"]*\"[[:space:]]*:'",
            name="ck_project_channel_instances_public_config",
        ),
        CheckConstraint(
            "provider_identity_digest ~ '^[0-9a-f]{64}$'",
            name="ck_project_channel_instances_identity_digest",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_project_channel_instances_revision",
        ),
        UniqueConstraint(
            "project_id",
            "id",
            name="uq_project_channel_instances_project_id",
        ),
        UniqueConstraint(
            "project_id",
            "id",
            "provider",
            name="uq_project_channel_instances_project_provider",
        ),
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_channel_instances_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_project_channel_instances_creator",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_project_channel_instances_updater",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_project_channel_instances_live_provider",
            "project_id",
            "provider",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_project_channel_instances_live_identity",
            "provider",
            "provider_identity_digest",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_project_channel_instances_runtime",
            "desired_status",
            "observed_status",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class ProjectChannelInstanceLeaseRow(Base):
    """Single-writer lease with monotonic fencing for a channel instance."""

    __tablename__ = "project_channel_instance_leases"

    channel_instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    holder_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    lease_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fencing_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "channel_instance_id",
            name="pk_project_channel_instance_leases",
        ),
        CheckConstraint(
            "lease_token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_project_channel_instance_leases_token_hash",
        ),
        CheckConstraint(
            "fencing_generation >= 1",
            name="ck_project_channel_instance_leases_generation",
        ),
        ForeignKeyConstraint(
            ["project_id", "channel_instance_id"],
            ["project_channel_instances.project_id", "project_channel_instances.id"],
            name="fk_project_channel_instance_leases_instance",
            ondelete="CASCADE",
        ),
        Index(
            "ix_project_channel_instance_leases_expiry",
            "lease_expires_at",
            "channel_instance_id",
        ),
    )


class ProjectChannelSecretStateRow(Base):
    """Write-only secret-bundle state owned by one Channel Instance."""

    __tablename__ = "project_channel_secret_states"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    channel_instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    current_generation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    updated_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "project_id",
            "channel_instance_id",
            name="pk_project_channel_secret_states",
        ),
        CheckConstraint(
            "revision >= 0",
            name="ck_project_channel_secret_states_revision",
        ),
        ForeignKeyConstraint(
            ["project_id", "channel_instance_id"],
            ["project_channel_instances.project_id", "project_channel_instances.id"],
            name="fk_project_channel_secret_states_instance",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "channel_instance_id", "current_generation_id"],
            [
                "project_channel_secret_generations.project_id",
                "project_channel_secret_generations.channel_instance_id",
                "project_channel_secret_generations.id",
            ],
            name="fk_project_channel_secret_states_current_generation",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_project_channel_secret_states_updater",
            ondelete="RESTRICT",
        ),
    )


class ProjectChannelSecretGenerationRow(Base):
    __tablename__ = "project_channel_secret_generations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    channel_instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    envelope_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "channel_instance_id"],
            ["project_channel_instances.project_id", "project_channel_instances.id"],
            name="fk_project_channel_secret_generations_instance",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_project_channel_secret_generations_creator",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "project_id",
            "channel_instance_id",
            "id",
            name="uq_project_channel_secret_generations_owner_id",
        ),
        UniqueConstraint(
            "project_id",
            "channel_instance_id",
            "revision",
            name="uq_project_channel_secret_generations_revision",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_project_channel_secret_generations_revision",
        ),
        CheckConstraint(
            "octet_length(nonce) = 12 AND octet_length(ciphertext) >= 16",
            name="ck_project_channel_secret_generations_envelope",
        ),
        CheckConstraint(
            "envelope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_project_channel_secret_generations_digest",
        ),
    )


class ProjectChannelSecretTombstoneRow(Base):
    __tablename__ = "project_channel_secret_tombstones"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    channel_instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    destroyed_generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    envelope_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(24), nullable=False)
    destroyed_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    destroyed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_channel_secret_tombstones_project",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["destroyed_by_user_id"],
            ["users.id"],
            name="fk_project_channel_secret_tombstones_actor",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "project_id",
            "channel_instance_id",
            "destroyed_generation_id",
            name="uq_project_channel_secret_tombstones_generation",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_project_channel_secret_tombstones_revision",
        ),
        CheckConstraint(
            "envelope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_project_channel_secret_tombstones_digest",
        ),
        CheckConstraint(
            "reason IN ('replace', 'clear', 'delete', 'recipient_change')",
            name="ck_project_channel_secret_tombstones_reason",
        ),
    )


class ChannelConnectionRow(Base):
    __tablename__ = "channel_connections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="connected")

    external_account_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    external_account_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    workspace_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    bot_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    scopes_json: Mapped[list] = mapped_column(JSON, default=list)
    capabilities_json: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    # NULL is a migration-only compatibility boundary for deployment-owned
    # ``config.yaml`` providers. Every project-managed UI/API connection must
    # carry a concrete instance UUID and satisfy the composite project FK.
    channel_instance_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "channel_instance_id"],
            ["project_channel_instances.project_id", "project_channel_instances.id"],
            name="fk_channel_connections_project_instance",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_channel_connection_owner_legacy_identity",
            "project_id",
            "owner_user_id",
            "provider",
            "external_account_id",
            "workspace_id",
            unique=True,
            postgresql_where=text("channel_instance_id IS NULL"),
        ),
        Index(
            "uq_channel_connection_owner_instance_identity",
            "project_id",
            "owner_user_id",
            "channel_instance_id",
            "external_account_id",
            "workspace_id",
            unique=True,
            postgresql_where=text("channel_instance_id IS NOT NULL"),
        ),
        UniqueConstraint("project_id", "owner_user_id", "id", name="uq_channel_connections_private_scope"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_channel_connections_project", ondelete="RESTRICT"),
        ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_channel_connections_owner", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_channel_connections_project_membership",
            ondelete="RESTRICT",
        ),
        CheckConstraint("status IN ('connected', 'frozen', 'revoked')", name="ck_channel_connections_status"),
        Index("idx_channel_connections_event_lookup", "channel_instance_id", "provider", "workspace_id", "bot_user_id"),
        # Enforce the single-active-owner invariant at the database layer: at most
        # one non-revoked row may exist per external identity. This makes ownership
        # transfer race-safe (concurrent connects from different owners can no
        # longer both commit a connected row). PostgreSQL enforces the partial
        # unique predicate used by the runtime schema.
        Index(
            "uq_channel_connection_active_legacy_identity",
            "provider",
            "external_account_id",
            "workspace_id",
            unique=True,
            postgresql_where=text("status = 'connected' AND channel_instance_id IS NULL"),
        ),
        Index(
            "uq_channel_connection_active_instance_identity",
            "channel_instance_id",
            "external_account_id",
            "workspace_id",
            unique=True,
            postgresql_where=text("status = 'connected' AND channel_instance_id IS NOT NULL"),
        ),
    )


class ChannelCredentialRow(Base):
    __tablename__ = "channel_credentials"

    connection_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("channel_connections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    encrypted_access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    encrypted_extra_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)


class ChannelOAuthStateRow(Base):
    __tablename__ = "channel_oauth_states"

    state_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    code_verifier_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    nonce_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    redirect_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_scopes_json: Mapped[list] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    # See ChannelConnectionRow.channel_instance_id: NULL is legacy-only.
    channel_instance_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "channel_instance_id"],
            ["project_channel_instances.project_id", "project_channel_instances.id"],
            name="fk_channel_oauth_states_project_instance",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_channel_oauth_states_project", ondelete="RESTRICT"),
        ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_channel_oauth_states_owner", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_channel_oauth_states_project_membership",
            ondelete="RESTRICT",
        ),
    )


class ChannelConversationRow(Base):
    __tablename__ = "channel_conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("channel_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_topic_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "external_conversation_id",
            "external_topic_id",
            name="uq_channel_conversation_connection_external",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "connection_id",
            "provider",
            "external_conversation_id",
            "external_topic_id",
            "thread_id",
            name="uq_channel_conversation_delivery_scope",
        ),
        ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_channel_conversations_project", ondelete="RESTRICT"),
        ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_channel_conversations_owner", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_channel_conversations_project_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "connection_id"],
            ["channel_connections.project_id", "channel_connections.owner_user_id", "channel_connections.id"],
            name="fk_channel_conversations_private_connection",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id"],
            ["threads_meta.project_id", "threads_meta.owner_user_id", "threads_meta.thread_id"],
            name="fk_channel_conversations_private_thread",
            ondelete="CASCADE",
        ),
    )


class ChannelInboundDeliveryRow(Base):
    """One provider delivery atomically bound to one private Run."""

    __tablename__ = "channel_inbound_deliveries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_conversation_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )
    external_topic_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="",
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_delivery_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "connection_id",
            "provider",
            "external_conversation_id",
            "external_topic_id",
            "provider_delivery_digest",
            name="uq_channel_inbound_deliveries_scope",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "owner_user_id",
                "connection_id",
            ],
            [
                "channel_connections.project_id",
                "channel_connections.owner_user_id",
                "channel_connections.id",
            ],
            name="fk_channel_inbound_deliveries_connection",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            [
                "runs.project_id",
                "runs.owner_user_id",
                "runs.thread_id",
                "runs.run_id",
            ],
            name="fk_channel_inbound_deliveries_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "provider_delivery_digest <> ''",
            name="ck_channel_inbound_deliveries_digest",
        ),
        Index(
            "ix_channel_inbound_deliveries_run",
            "project_id",
            "owner_user_id",
            "run_id",
        ),
    )
