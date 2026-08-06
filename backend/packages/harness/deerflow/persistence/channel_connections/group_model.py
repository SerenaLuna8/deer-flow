"""Provider-neutral project group bindings and non-login member identities."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ProjectChannelGroupBindingRow(Base):
    """One externally identified group enabled for one project channel app."""

    __tablename__ = "project_channel_group_bindings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    channel_instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_group_ref: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    external_group_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    agent_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    agent_asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default="active",
    )
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    updated_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    first_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
        CheckConstraint(
            "provider ~ '^[a-z][a-z0-9_-]{0,31}$'",
            name="ck_project_channel_group_bindings_provider",
        ),
        CheckConstraint(
            "external_group_ref ~ '^[0-9a-f]{64}$'",
            name="ck_project_channel_group_bindings_external_ref",
        ),
        CheckConstraint(
            "agent_scope IN ('system', 'project')",
            name="ck_project_channel_group_bindings_agent_scope",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_project_channel_group_bindings_status",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_project_channel_group_bindings_revision",
        ),
        CheckConstraint(
            "(first_activity_at IS NULL AND last_activity_at IS NULL) OR (first_activity_at IS NOT NULL AND last_activity_at IS NOT NULL AND first_activity_at <= last_activity_at)",
            name="ck_project_channel_group_bindings_activity",
        ),
        CheckConstraint(
            "deleted_at IS NULL OR status = 'disabled'",
            name="ck_project_channel_group_bindings_deleted_status",
        ),
        UniqueConstraint(
            "project_id",
            "id",
            name="uq_project_channel_group_bindings_project_id",
        ),
        ForeignKeyConstraint(
            ["project_id", "channel_instance_id", "provider"],
            [
                "project_channel_instances.project_id",
                "project_channel_instances.id",
                "project_channel_instances.provider",
            ],
            name="fk_project_channel_group_bindings_instance",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["agent_asset_id", "agent_scope"],
            ["agents.id", "agents.scope"],
            name="fk_project_channel_group_bindings_agent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_project_channel_group_bindings_creator",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_project_channel_group_bindings_updater",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_project_channel_group_bindings_live_group",
            "channel_instance_id",
            "external_group_ref",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_project_channel_group_bindings_project_status",
            "project_id",
            "status",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class ChannelExternalPrincipalRow(Base):
    """One isolated non-login owner for a sender within a group binding."""

    __tablename__ = "channel_external_principals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    group_binding_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    external_account_ref: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    principal_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    principal_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="channel_guest",
        server_default="channel_guest",
    )
    membership_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    membership_role: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="channel_guest",
        server_default="channel_guest",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default="active",
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=text("now()"),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=text("now()"),
    )
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
        CheckConstraint(
            "external_account_ref ~ '^[0-9a-f]{64}$'",
            name="ck_channel_external_principals_external_ref",
        ),
        CheckConstraint(
            "principal_type = 'channel_guest'",
            name="ck_channel_external_principals_type",
        ),
        CheckConstraint(
            "membership_role = 'channel_guest'",
            name="ck_channel_external_principals_membership_role",
        ),
        CheckConstraint(
            "status IN ('active', 'frozen')",
            name="ck_channel_external_principals_status",
        ),
        CheckConstraint(
            "first_seen_at <= last_seen_at",
            name="ck_channel_external_principals_seen_order",
        ),
        UniqueConstraint(
            "group_binding_id",
            "external_account_ref",
            name="uq_channel_external_principals_group_account",
        ),
        ForeignKeyConstraint(
            ["project_id", "group_binding_id"],
            [
                "project_channel_group_bindings.project_id",
                "project_channel_group_bindings.id",
            ],
            name="fk_channel_external_principals_group_binding",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["principal_user_id", "principal_type"],
            ["users.id", "users.principal_type"],
            name="fk_channel_external_principals_guest_user",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "principal_user_id",
                "membership_id",
                "membership_role",
            ],
            [
                "project_memberships.project_id",
                "project_memberships.user_id",
                "project_memberships.id",
                "project_memberships.role",
            ],
            name="fk_channel_external_principals_guest_membership",
            ondelete="CASCADE",
        ),
        Index(
            "ix_channel_external_principals_project_status",
            "project_id",
            "status",
            "id",
        ),
    )
