"""PostgreSQL authority rows for revocable authenticated sessions."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CHAR, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class AuthSessionRow(Base):
    __tablename__ = "auth_sessions"

    # The raw unpredictable JWT ``sid`` claim is never persisted.
    session_id_hash: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "session_id_hash ~ '^[0-9a-f]{64}$'",
            name="ck_auth_sessions_hash",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_auth_sessions_expiry",
        ),
        CheckConstraint(
            "last_seen_at >= created_at AND last_seen_at <= expires_at",
            name="ck_auth_sessions_last_seen",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_auth_sessions_revoked_at",
        ),
        Index(
            "ix_auth_sessions_expires_at",
            "expires_at",
            "session_id_hash",
        ),
        Index(
            "ix_auth_sessions_revoked_at",
            "revoked_at",
            "session_id_hash",
            postgresql_where=text("revoked_at IS NOT NULL"),
        ),
        Index(
            "ix_auth_sessions_user_active",
            "user_id",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


__all__ = ["AuthSessionRow"]
