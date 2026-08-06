from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class UserNotificationRow(Base):
    __tablename__ = "user_notifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    recipient_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    project_invitation_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("project_invitations.id", ondelete="CASCADE"), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("kind = 'project_invitation'", name="ck_user_notifications_kind"),
        CheckConstraint("version >= 1", name="ck_user_notifications_version"),
        CheckConstraint("read_at IS NULL OR read_at >= created_at", name="ck_user_notifications_read_at"),
        CheckConstraint("acted_at IS NULL OR acted_at >= created_at", name="ck_user_notifications_acted_at"),
        CheckConstraint("acted_at IS NULL OR read_at IS NOT NULL", name="ck_user_notifications_acted_is_read"),
        UniqueConstraint("project_invitation_id", name="uq_user_notifications_project_invitation_id"),
        Index("ix_user_notifications_recipient_cursor", "recipient_user_id", created_at.desc(), id.desc()),
        Index(
            "ix_user_notifications_recipient_unread",
            "recipient_user_id",
            "created_at",
            postgresql_where=text("read_at IS NULL"),
        ),
    )
