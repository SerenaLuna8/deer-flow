"""ORM model for run events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
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


class RunEventPartitionStateRow(Base):
    """Singleton retention watermark for monthly ``run_events`` partitions."""

    __tablename__ = "run_event_partition_state"

    singleton: Mapped[bool] = mapped_column(
        Boolean,
        primary_key=True,
        default=True,
        server_default=text("true"),
    )
    retained_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "singleton",
            name="ck_run_event_partition_state_singleton",
        ),
    )


class RunEventInvariantRow(Base):
    """Narrow global-key ledger for the monthly ``run_events`` partitions."""

    __tablename__ = "run_event_invariants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_stream_terminal: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )

    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "seq",
            name="uq_events_thread_seq",
        ),
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "thread_id",
            "run_id",
            "seq",
            name="uq_run_events_private_seq",
        ),
        Index(
            "uq_run_events_stream_terminal",
            "project_id",
            "owner_user_id",
            "thread_id",
            "run_id",
            unique=True,
            postgresql_where=text("is_stream_terminal"),
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            [
                "runs.project_id",
                "runs.owner_user_id",
                "runs.thread_id",
                "runs.run_id",
            ],
            name="fk_run_event_invariants_private_run",
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        default=lambda: datetime.now(UTC),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    __table_args__ = (
        Index("ix_events_thread_cat_seq", "thread_id", "category", "seq"),
        Index("ix_events_run", "thread_id", "run_id", "seq"),
        Index(
            "ix_run_events_stream_terminal",
            "project_id",
            "owner_user_id",
            "thread_id",
            "run_id",
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
        {"postgresql_partition_by": "RANGE (created_at)"},
    )


@event.listens_for(RunEventRow, "before_insert")
def _ensure_run_event_month_partition(_mapper, connection, target: RunEventRow) -> None:
    """Create the target UTC month before PostgreSQL routes the ORM insert."""

    if connection.dialect.name == "postgresql":
        if target.created_at is None:
            target.created_at = datetime.now(UTC)
        connection.execute(
            text("SELECT ensure_run_events_month_partition(:created_at)"),
            {"created_at": target.created_at},
        )
