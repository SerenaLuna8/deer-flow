"""ORM model for run events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKeyConstraint, Index, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, synonym

from deerflow.persistence.base import Base


class RunEventRow(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    user_id = synonym("owner_user_id")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    # "message" | "trace" | "lifecycle"
    content: Mapped[str] = mapped_column(Text, default="")
    event_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    seq: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_events_thread_seq"),
        UniqueConstraint("project_id", "owner_user_id", "thread_id", "run_id", "seq", name="uq_run_events_private_seq"),
        Index("ix_events_thread_cat_seq", "thread_id", "category", "seq"),
        Index("ix_events_run", "thread_id", "run_id", "seq"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_run_events_project", ondelete="RESTRICT"),
        ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_run_events_owner", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_run_events_project_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"],
            name="fk_run_events_private_run",
            ondelete="CASCADE",
        ),
    )
