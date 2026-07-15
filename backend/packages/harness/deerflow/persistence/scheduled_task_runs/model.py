from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class ScheduledTaskRunRow(Base):
    __tablename__ = "scheduled_task_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurrence_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    manual_idempotency_hash: Mapped[str | None] = mapped_column(CHAR(64))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64))
    resolved_membership_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    resolved_membership_version: Mapped[int | None] = mapped_column(BigInteger)
    launch_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "task_id",
            "occurrence_key",
            name="uq_scheduled_task_runs_occurrence",
        ),
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_scheduled_task_runs_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_scheduled_task_runs_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "task_id"],
            [
                "scheduled_tasks.project_id",
                "scheduled_tasks.owner_user_id",
                "scheduled_tasks.id",
            ],
            name="fk_scheduled_task_runs_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id"],
            [
                "threads_meta.project_id",
                "threads_meta.owner_user_id",
                "threads_meta.thread_id",
            ],
            name="fk_scheduled_task_runs_private_thread",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            [
                "runs.project_id",
                "runs.owner_user_id",
                "runs.thread_id",
                "runs.run_id",
            ],
            name="fk_scheduled_task_runs_private_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "trigger IN ('scheduled', 'manual')",
            name="ck_scheduled_task_runs_trigger",
        ),
        CheckConstraint(
            "status IN ('queued', 'launching', 'running', 'success', 'failed', 'skipped', 'interrupted', 'cancelled', 'rejected')",
            name="ck_scheduled_task_runs_status",
        ),
        CheckConstraint(
            "run_id IS NULL OR thread_id IS NOT NULL",
            name="ck_scheduled_task_runs_run_requires_thread",
        ),
        CheckConstraint(
            "launch_attempt_count >= 0 AND (resolved_membership_version IS NULL OR resolved_membership_version >= 1)",
            name="ck_scheduled_task_runs_attempt_count",
        ),
        CheckConstraint(
            "task_version >= 1",
            name="ck_scheduled_task_runs_task_version",
        ),
        Index(
            "uq_scheduled_task_runs_manual_idempotency",
            "project_id",
            "owner_user_id",
            "task_id",
            "manual_idempotency_hash",
            unique=True,
            postgresql_where=text("manual_idempotency_hash IS NOT NULL"),
        ),
        Index(
            "ix_scheduled_task_runs_active_occurrence",
            "project_id",
            "owner_user_id",
            "status",
            "scheduled_for",
            "id",
            postgresql_where=text("status IN ('queued', 'launching', 'running')"),
        ),
        Index(
            "ix_scheduled_task_runs_history",
            "project_id",
            "owner_user_id",
            "task_id",
            created_at.desc(),
            id.desc(),
        ),
    )
