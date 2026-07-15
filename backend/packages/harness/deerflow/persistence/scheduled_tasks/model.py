from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DDL,
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
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


_CREATE_AGENT_PROJECT_INTEGRITY_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_scheduled_task_agent_project()
RETURNS trigger AS $$
BEGIN
    IF TG_TABLE_NAME = 'scheduled_tasks' THEN
        IF NEW.agent_scope = 'project' THEN
            PERFORM 1
            FROM agents
            WHERE id = NEW.agent_asset_id
              AND scope = 'project'
              AND project_id = NEW.project_id
            FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'project Agent must belong to the scheduled task project'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'agents'
       AND NEW.project_id IS DISTINCT FROM OLD.project_id
       AND EXISTS (
           SELECT 1
           FROM scheduled_tasks task
           WHERE task.agent_asset_id = OLD.id
             AND task.agent_scope = 'project'
             AND task.project_id IS DISTINCT FROM NEW.project_id
       ) THEN
        RAISE EXCEPTION 'cannot move a project Agent referenced by scheduled tasks'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_AGENT_PROJECT_TRIGGER_DDL = (
    _CREATE_AGENT_PROJECT_INTEGRITY_FUNCTION,
    "CREATE TRIGGER trg_scheduled_tasks_agent_project BEFORE INSERT OR UPDATE OF project_id, agent_asset_id, agent_scope ON scheduled_tasks FOR EACH ROW EXECUTE FUNCTION enforce_scheduled_task_agent_project()",
    "CREATE TRIGGER trg_agents_scheduled_task_project BEFORE UPDATE OF project_id ON agents FOR EACH ROW EXECUTE FUNCTION enforce_scheduled_task_agent_project()",
)


def _install_agent_project_integrity(_target, connection, **kwargs) -> None:
    created_tables = {table.name for table in kwargs.get("tables", ())}
    if not {"agents", "scheduled_tasks"} <= created_tables or connection.dialect.name != "postgresql":
        return
    for statement in _AGENT_PROJECT_TRIGGER_DDL:
        connection.execute(DDL(statement))


event.listen(Base.metadata, "after_create", _install_agent_project_integrity)
