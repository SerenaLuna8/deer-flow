"""ORM model for run events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, synonym

from deerflow.persistence.base import Base


class ThreadEventSequenceRow(Base):
    """Deletion-stable high-watermark for one private Thread event log."""

    __tablename__ = "thread_event_sequences"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    high_watermark: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    __table_args__ = (
        CheckConstraint(
            "high_watermark >= 0",
            name="ck_thread_event_sequences_high_watermark",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id"],
            [
                "threads_meta.project_id",
                "threads_meta.owner_user_id",
                "threads_meta.thread_id",
            ],
            name="fk_thread_event_sequences_thread",
            ondelete="CASCADE",
        ),
    )


class RunEventRow(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    user_id = synonym("owner_user_id")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    # Includes message/trace/lifecycle plus M6's durable "stream" category.
    content: Mapped[str] = mapped_column(Text, default="")
    event_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_events_thread_seq"),
        UniqueConstraint("project_id", "owner_user_id", "thread_id", "run_id", "seq", name="uq_run_events_private_seq"),
        Index("ix_events_thread_cat_seq", "thread_id", "category", "seq"),
        Index("ix_events_run", "thread_id", "run_id", "seq"),
        Index(
            "uq_run_events_stream_terminal",
            "project_id",
            "owner_user_id",
            "thread_id",
            "run_id",
            unique=True,
            postgresql_where=text("category = 'stream' AND event_type = 'stream.end'"),
        ),
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
