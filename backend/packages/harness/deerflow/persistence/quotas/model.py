from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectQuotaRow(Base):
    __tablename__ = "project_quotas"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    member_limit: Mapped[int | None] = mapped_column(Integer)
    storage_bytes_limit: Mapped[int | None] = mapped_column(BigInteger)
    concurrent_run_limit: Mapped[int | None] = mapped_column(Integer)
    mcp_calls_daily_limit: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    updated_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "(member_limit IS NULL OR member_limit >= 1) AND "
            "(storage_bytes_limit IS NULL OR storage_bytes_limit >= 0) AND "
            "(concurrent_run_limit IS NULL OR concurrent_run_limit >= 1) AND "
            "(mcp_calls_daily_limit IS NULL OR mcp_calls_daily_limit >= 0) AND version >= 1",
            name="ck_project_quotas_limits",
        ),
    )


class ProjectUsageCounterRow(Base):
    __tablename__ = "project_usage_counters"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    dimension: Mapped[str] = mapped_column(String(32), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(32), primary_key=True, default="lifetime", server_default="lifetime")
    used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    reserved: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "dimension IN ('members', 'storage_bytes', 'concurrent_runs', 'mcp_calls_daily')",
            name="ck_project_usage_counters_dimension",
        ),
        CheckConstraint("used >= 0 AND reserved >= 0 AND version >= 1", name="ck_project_usage_counters_values"),
    )


class ProjectUsageLedgerRow(Base):
    __tablename__ = "project_usage_ledger"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bucket: Mapped[str] = mapped_column(String(32), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref_hmac: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("project_id", "dimension", "idempotency_key", name="uq_project_usage_ledger_idempotency"),
        CheckConstraint(
            "dimension IN ('members', 'storage_bytes', 'concurrent_runs', 'mcp_calls_daily')",
            name="ck_project_usage_ledger_dimension",
        ),
        CheckConstraint("delta <> 0", name="ck_project_usage_ledger_delta"),
        Index("ix_project_usage_ledger_project_cursor", "project_id", occurred_at.desc(), id.desc()),
    )
