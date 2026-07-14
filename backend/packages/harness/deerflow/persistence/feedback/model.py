"""ORM model for user feedback on runs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, synonym

from deerflow.persistence.base import Base


class FeedbackRow(Base):
    __tablename__ = "feedback"

    feedback_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    user_id = synonym("owner_user_id")
    message_id: Mapped[str | None] = mapped_column(String(64))
    # message_id is an optional RunEventStore event identifier —
    # allows feedback to target a specific message or the entire run

    rating: Mapped[int] = mapped_column(nullable=False)
    # +1 (thumbs-up) or -1 (thumbs-down)

    comment: Mapped[str | None] = mapped_column(Text)
    # Optional text feedback from the user

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("project_id", "owner_user_id", "thread_id", "run_id", name="uq_feedback_private_run_owner"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_feedback_project", ondelete="RESTRICT"),
        ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_feedback_owner", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_feedback_project_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_feedback_private_run",
            ondelete="CASCADE",
        ),
    )
