from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class AutomationMigrationRunRow(Base):
    __tablename__ = "automation_migration_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", server_default="running")
    source_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    owner_map_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_task_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    source_run_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    source_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    scope_relation_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "mode IN ('dry_run', 'execute')",
            name="ck_automation_migration_runs_mode",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_automation_migration_runs_status",
        ),
        CheckConstraint(
            "source_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_automation_migration_runs_source",
        ),
        CheckConstraint(
            "owner_map_digest ~ '^[0-9a-f]{64}$'",
            name="ck_automation_migration_runs_owner_map",
        ),
        CheckConstraint(
            "source_task_count >= 0 AND source_run_count >= 0",
            name="ck_automation_migration_runs_counts",
        ),
    )


class AutomationMigrationLedgerRow(Base):
    __tablename__ = "automation_migration_ledger"

    migration_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("automation_migration_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    domain: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    target_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="complete", server_default="complete")
    source_row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    target_row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "domain IN ('scheduled_tasks', 'scheduled_task_runs')",
            name="ck_automation_migration_ledger_domain",
        ),
        CheckConstraint(
            "status = 'complete'",
            name="ck_automation_migration_ledger_status",
        ),
        CheckConstraint(
            "source_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_automation_migration_ledger_source",
        ),
        CheckConstraint(
            "target_digest ~ '^[0-9a-f]{64}$'",
            name="ck_automation_migration_ledger_target",
        ),
        CheckConstraint(
            "source_row_count >= 0 AND target_row_count >= 0",
            name="ck_automation_migration_ledger_counts",
        ),
    )


class AutomationCutoverStateRow(Base):
    __tablename__ = "automation_cutover_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1, server_default=text("1"))
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    migration_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("automation_migration_runs.id", ondelete="RESTRICT"))
    empty_domain_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    final_schema_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    cutover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_automation_cutover_state_singleton"),
        CheckConstraint(
            "stage IN ('empty_install', 'migration_ready', 'cutover_complete')",
            name="ck_automation_cutover_state_stage",
        ),
        CheckConstraint(
            "stage != 'migration_ready' OR migration_run_id IS NOT NULL",
            name="ck_automation_cutover_state_migration_ready",
        ),
        CheckConstraint(
            "stage != 'cutover_complete' OR ((empty_domain_probe_complete OR migration_run_id IS NOT NULL) AND final_schema_probe_complete AND cutover_at IS NOT NULL)",
            name="ck_automation_cutover_state_complete",
        ),
    )
