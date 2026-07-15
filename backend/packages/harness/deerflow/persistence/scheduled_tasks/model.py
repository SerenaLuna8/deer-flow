from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class ScheduledTaskRow(Base):
    __tablename__ = "scheduled_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    agent_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(16), nullable=False)
    schedule_spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="enabled")
    overlap_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="skip")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outcome: Mapped[str | None] = mapped_column(String(24))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    run_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "owner_user_id",
            "id",
            name="uq_scheduled_tasks_private_scope",
        ),
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_scheduled_tasks_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_scheduled_tasks_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_scheduled_tasks_project_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id"],
            [
                "threads_meta.project_id",
                "threads_meta.owner_user_id",
                "threads_meta.thread_id",
            ],
            name="fk_scheduled_tasks_private_thread",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["agent_asset_id", "agent_scope"],
            ["agents.id", "agents.scope"],
            name="fk_scheduled_tasks_agent_asset",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "context_mode IN ('fresh_thread_per_run', 'reuse_thread')",
            name="ck_scheduled_tasks_context_mode",
        ),
        CheckConstraint(
            "schedule_type IN ('once', 'cron')",
            name="ck_scheduled_tasks_schedule_type",
        ),
        CheckConstraint(
            "status IN ('enabled', 'paused', 'completed', 'failed', 'cancelled')",
            name="ck_scheduled_tasks_status",
        ),
        CheckConstraint(
            "overlap_policy = 'skip'",
            name="ck_scheduled_tasks_overlap_policy",
        ),
        CheckConstraint(
            "(context_mode = 'reuse_thread' AND thread_id IS NOT NULL) OR (context_mode = 'fresh_thread_per_run' AND thread_id IS NULL)",
            name="ck_scheduled_tasks_thread_mode",
        ),
        CheckConstraint(
            "agent_scope IN ('system', 'project')",
            name="ck_scheduled_tasks_agent_scope",
        ),
        CheckConstraint("version >= 1", name="ck_scheduled_tasks_version"),
        CheckConstraint("run_count >= 0", name="ck_scheduled_tasks_run_count"),
        CheckConstraint(
            "last_outcome IS NULL OR last_outcome IN ('success', 'failed', 'skipped', 'interrupted', 'cancelled', 'rejected')",
            name="ck_scheduled_tasks_last_outcome",
        ),
    )
