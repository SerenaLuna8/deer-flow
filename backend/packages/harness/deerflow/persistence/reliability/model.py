from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, DDL, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, SmallInteger, String, Uuid, event, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ReliabilityMigrationRunRow(Base):
    __tablename__ = "reliability_migration_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", server_default="running")
    source_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    backup_proof_digest: Mapped[str | None] = mapped_column(CHAR(64))
    source_row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    source_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    active_run_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("mode IN ('dry_run', 'execute')", name="ck_reliability_migration_runs_mode"),
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_reliability_migration_runs_status"),
        CheckConstraint("source_row_count >= 0", name="ck_reliability_migration_runs_count"),
    )


class ReliabilityMigrationLedgerRow(Base):
    __tablename__ = "reliability_migration_ledger"

    migration_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reliability_migration_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    domain: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    target_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="complete", server_default="complete")
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "domain IN ('jobs', 'quotas', 'audit', 'stream', 'recovery')",
            name="ck_reliability_migration_ledger_domain",
        ),
        CheckConstraint("status = 'complete'", name="ck_reliability_migration_ledger_status"),
        CheckConstraint(
            "source_row_count >= 0 AND target_row_count >= 0",
            name="ck_reliability_migration_ledger_counts",
        ),
    )


class ReliabilityCutoverStateRow(Base):
    __tablename__ = "reliability_cutover_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1, server_default=text("1"))
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    migration_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reliability_migration_runs.id", ondelete="RESTRICT"))
    empty_domain_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    source_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    active_run_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    quota_backfill_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    job_relation_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    audit_trigger_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    stream_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    recovery_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    final_schema_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    schema_revision: Mapped[str | None] = mapped_column(String(64))
    cutover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_reliability_cutover_state_singleton"),
        CheckConstraint(
            "stage IN ('expand_ready', 'empty_install', 'migration_ready', 'cutover_complete')",
            name="ck_reliability_cutover_state_stage",
        ),
        CheckConstraint(
            "stage != 'cutover_complete' OR (((empty_domain_probe_complete AND migration_run_id IS NULL) OR "
            "(NOT empty_domain_probe_complete AND migration_run_id IS NOT NULL)) AND source_probe_complete AND "
            "active_run_probe_complete AND quota_backfill_probe_complete AND job_relation_probe_complete AND "
            "audit_trigger_probe_complete AND stream_probe_complete AND recovery_probe_complete AND "
            "final_schema_probe_complete AND schema_revision IS NOT NULL AND cutover_at IS NOT NULL)",
            name="ck_reliability_cutover_state_complete",
        ),
    )


_APPEND_ONLY_TABLES = ("project_usage_ledger", "audit_logs", "dead_jobs")
_APPEND_ONLY_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION reject_m6_append_only_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'M6 append-only rows cannot be updated or deleted'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql
"""


def _install_m6_append_only_triggers(_target, connection, **kwargs) -> None:
    created_tables = {table.name for table in kwargs.get("tables", ())}
    if not set(_APPEND_ONLY_TABLES) <= created_tables or connection.dialect.name != "postgresql":
        return
    connection.execute(DDL(_APPEND_ONLY_FUNCTION_DDL))
    for table in _APPEND_ONLY_TABLES:
        connection.execute(
            DDL(
                f"""CREATE TRIGGER trg_{table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_m6_append_only_mutation()"""
            )
        )


event.listen(Base.metadata, "after_create", _install_m6_append_only_triggers)
