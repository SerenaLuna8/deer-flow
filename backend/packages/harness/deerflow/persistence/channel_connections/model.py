"""ORM models for user-owned IM channel connections."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "provider",
            "external_account_id",
            "workspace_id",
            name="uq_channel_connection_owner_provider_identity",
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
        Index("idx_channel_connections_event_lookup", "provider", "workspace_id", "bot_user_id"),
        # Enforce the single-active-owner invariant at the database layer: at most
        # one non-revoked row may exist per external identity. This makes ownership
        # transfer race-safe (concurrent connects from different owners can no
        # longer both commit a connected row). PostgreSQL enforces the partial
        # unique predicate used by the runtime schema.
        Index(
            "uq_channel_connection_active_identity",
            "provider",
            "external_account_id",
            "workspace_id",
            unique=True,
            postgresql_where=text("status = 'connected'"),
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

    __table_args__ = (
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
                "provider",
                "external_conversation_id",
                "external_topic_id",
                "thread_id",
            ],
            [
                "channel_conversations.project_id",
                "channel_conversations.owner_user_id",
                "channel_conversations.connection_id",
                "channel_conversations.provider",
                "channel_conversations.external_conversation_id",
                "channel_conversations.external_topic_id",
                "channel_conversations.thread_id",
            ],
            name="fk_channel_inbound_deliveries_conversation",
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
