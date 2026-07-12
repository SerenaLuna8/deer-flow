from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CHAR, CheckConstraint, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectInvitationRateLimitRow(Base):
    __tablename__ = "project_invitation_rate_limits"

    key_hash: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (
        CheckConstraint(
            "key_hash ~ '^[0-9a-f]{64}$'",
            name="ck_project_invitation_rate_limits_key_hash",
        ),
        CheckConstraint(
            "failure_count >= 1",
            name="ck_project_invitation_rate_limits_failure_count",
        ),
    )
