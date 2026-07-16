from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, BigInteger, Boolean, CheckConstraint, DateTime, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class DeletionTombstoneRow(Base):
    __tablename__ = "deletion_tombstones"

    journal_sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ciphertext_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_ref_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_ref_hmac: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    purge_status: Mapped[str] = mapped_column(String(16), nullable=False, default="journaled", server_default="journaled")
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("journal_sequence >= 1", name="ck_deletion_tombstones_sequence"),
        CheckConstraint("purge_status IN ('journaled', 'purged')", name="ck_deletion_tombstones_status"),
        Index("ix_deletion_tombstones_committed", committed_at, "journal_sequence"),
    )


class RestoreProofRow(Base):
    __tablename__ = "restore_proofs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    archive_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    archive_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    target_database_ref_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_database_ref_hmac: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    schema_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_tombstone_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    replayed_through_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    probes_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    restored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "archive_tombstone_sequence >= 0 AND replayed_through_sequence >= archive_tombstone_sequence",
            name="ck_restore_proofs_sequences",
        ),
        Index("ix_restore_proofs_archive", "archive_id", restored_at.desc()),
    )
