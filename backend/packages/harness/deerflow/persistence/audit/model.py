from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, JSON, CheckConstraint, DateTime, ForeignKey, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class AuditLogRow(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    actor_process: Mapped[str | None] = mapped_column(String(32))
    actor_platform_role: Mapped[str | None] = mapped_column(String(32))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_ref_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_ref_hmac: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    public_error_code: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128))
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"))
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("job_attempts.id", ondelete="RESTRICT"))
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))

    __table_args__ = (
        CheckConstraint(
            "(actor_user_id IS NOT NULL AND actor_process IS NULL) OR (actor_user_id IS NULL AND actor_process IS NOT NULL)",
            name="ck_audit_logs_actor",
        ),
        CheckConstraint("outcome IN ('success', 'rejected', 'failed')", name="ck_audit_logs_outcome"),
        Index("ix_audit_logs_project_cursor", "project_id", occurred_at.desc(), id.desc()),
        Index("ix_audit_logs_platform_cursor", occurred_at.desc(), id.desc()),
    )
