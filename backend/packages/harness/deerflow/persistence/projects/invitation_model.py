from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectInvitationRow(Base):
    __tablename__ = "project_invitations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    invited_email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    redeemed_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("role IN ('editor', 'runner', 'viewer')", name="ck_project_invitations_role"),
        CheckConstraint("status IN ('pending', 'redeemed', 'revoked', 'expired')", name="ck_project_invitations_status"),
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_project_invitations_token_hash"),
        CheckConstraint("version >= 1", name="ck_project_invitations_version"),
        UniqueConstraint("token_hash", name="uq_project_invitations_token_hash"),
        Index(
            "uq_project_invitations_pending_email",
            "project_id",
            "invited_email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )
