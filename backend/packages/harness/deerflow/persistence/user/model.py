"""ORM model for the users table.

Lives in the harness persistence package so it is picked up by
``Base.metadata.create_all()`` alongside ``threads_meta``, ``runs``,
``run_events``, and ``feedback``. Using the shared engine means:

- One PostgreSQL database and connection pool
- One schema initialisation codepath
- Consistent async sessions across auth and persistence reads
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class UserRow(Base):
    __tablename__ = "users"

    # Preserve the existing UUID-string storage used by persisted data and APIs.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # Channel guests are deliberately non-login principals and therefore have
    # no email identity.  Every public/authenticated account remains ``human``
    # and keeps the canonical non-null email contract.
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    principal_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="human",
        server_default=text("'human'"),
    )

    # "system_admin" | "user" — kept as plain string to avoid enum migrations
    # when new roles are introduced.
    system_role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    # OAuth linkage (optional). A partial unique index enforces one
    # account per (provider, oauth_id) pair, leaving NULL/NULL rows
    # unconstrained so plain password accounts can coexist.
    oauth_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    oauth_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Auth lifecycle flags
    needs_setup: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    token_version: Mapped[int] = mapped_column(nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("system_role IN ('system_admin', 'user')", name="ck_users_system_role"),
        CheckConstraint(
            "principal_type IN ('human', 'channel_guest')",
            name="ck_users_principal_type",
        ),
        CheckConstraint(
            "(oauth_provider IS NULL AND oauth_id IS NULL) OR (oauth_provider IS NOT NULL AND oauth_id IS NOT NULL)",
            name="ck_users_oauth_identity_shape",
        ),
        CheckConstraint(
            "(principal_type = 'human' AND email IS NOT NULL) OR "
            "(principal_type = 'channel_guest' AND email IS NULL AND password_hash IS NULL "
            "AND oauth_provider IS NULL AND oauth_id IS NULL AND system_role = 'user' "
            "AND needs_setup IS FALSE AND token_version = 0)",
            name="ck_users_channel_guest_identity",
        ),
        UniqueConstraint(
            "id",
            "principal_type",
            name="uq_users_id_principal_type",
        ),
        Index(
            "ix_users_email",
            func.lower(email),
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        Index(
            "idx_users_oauth_identity",
            "oauth_provider",
            "oauth_id",
            unique=True,
            postgresql_where=text("oauth_provider IS NOT NULL AND oauth_id IS NOT NULL"),
        ),
    )
