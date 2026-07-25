from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectRow(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="")
    icon: Mapped[str] = mapped_column(String(32), nullable=False, default="folder", server_default="folder")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deletion_effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deletion_requested_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", name="fk_projects_deletion_requested_by_user_id_users"),
        nullable=True,
    )
    is_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    membership_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    # users.id is intentionally VARCHAR(36); project-owned identifiers are native UUID.
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("char_length(slug) BETWEEN 3 AND 63", name="ck_projects_slug_length"),
        CheckConstraint("slug = lower(slug)", name="ck_projects_slug_lowercase"),
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="ck_projects_slug_format"),
        CheckConstraint("status IN ('active', 'pending_deletion')", name="ck_projects_status"),
        CheckConstraint("membership_version >= 1", name="ck_projects_membership_version"),
        UniqueConstraint("slug", name="uq_projects_slug"),
    )


class ProjectMembershipRow(Base):
    __tablename__ = "project_memberships"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", name="fk_project_memberships_ended_by_user_id_users"),
        nullable=True,
    )
    end_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    activation_generation: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    last_entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("role IN ('admin', 'editor', 'runner', 'viewer')", name="ck_project_memberships_role"),
        CheckConstraint("status IN ('active', 'left', 'removed')", name="ck_project_memberships_status"),
        CheckConstraint("end_reason IS NULL OR end_reason IN ('left', 'removed')", name="ck_project_memberships_end_reason"),
        CheckConstraint("version >= 1", name="ck_project_memberships_version"),
        CheckConstraint(
            "activation_generation >= 1",
            name="ck_project_memberships_activation_generation",
        ),
        UniqueConstraint("project_id", "user_id", name="uq_project_memberships_project_user"),
        Index("ix_project_memberships_user_id", "user_id"),
    )
