"""One-time project group-binding challenges.

The raw code is returned once to the project Admin and is never persisted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ProjectChannelGroupBindingChallengeRow(Base):
    __tablename__ = "project_channel_group_binding_challenges"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    channel_instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    agent_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    membership_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    membership_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "id",
            name="pk_project_channel_group_binding_challenges",
        ),
        UniqueConstraint(
            "code_digest",
            name="uq_project_channel_group_binding_challenges_code_digest",
        ),
        CheckConstraint(
            "provider ~ '^[a-z][a-z0-9_-]{0,31}$'",
            name="ck_project_channel_group_binding_challenges_provider",
        ),
        CheckConstraint(
            "code_digest ~ '^[0-9a-f]{64}$'",
            name="ck_project_channel_group_binding_challenges_digest",
        ),
        CheckConstraint(
            "agent_scope IN ('project', 'system')",
            name="ck_project_channel_group_binding_challenges_agent_scope",
        ),
        CheckConstraint(
            "membership_version >= 1",
            name="ck_project_channel_group_binding_challenges_membership_version",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_project_channel_group_binding_challenges_expiry",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="ck_project_channel_group_binding_challenges_consumed",
        ),
        ForeignKeyConstraint(
            ["project_id", "channel_instance_id"],
            ["project_channel_instances.project_id", "project_channel_instances.id"],
            name="fk_project_channel_group_binding_challenges_instance",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["membership_id"],
            ["project_memberships.id"],
            name="fk_project_channel_group_binding_challenges_membership",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "created_by_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_project_channel_group_binding_challenges_creator_membership",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["agent_asset_id", "agent_scope"],
            ["agents.id", "agents.scope"],
            name="fk_project_channel_group_binding_challenges_agent",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_project_channel_group_binding_challenges_pending",
            "channel_instance_id",
            "provider",
            "expires_at",
            postgresql_where=text("consumed_at IS NULL"),
        ),
        Index(
            "ix_project_channel_group_binding_challenges_membership",
            "project_id",
            "membership_id",
            "membership_version",
        ),
    )


__all__ = ["ProjectChannelGroupBindingChallengeRow"]
