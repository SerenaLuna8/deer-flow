"""PostgreSQL authority that freezes a source tombstone head during restore."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.recovery.identity import source_installation_id
from app.recovery.journal import (
    TombstoneJournal,
    TombstoneJournalUnavailable,
    TombstoneSnapshot,
)
from app.recovery.purge import RECOVERY_PURGE_ADVISORY_LOCK
from deerflow.config.database_config import DatabaseConfig


class SourceIdentityMismatch(RuntimeError):
    pass


class RecoveryAuthorityReleaseFailed(RuntimeError):
    pass


async def _settle_statement(awaitable):
    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _release_source_recovery_authority(connection) -> bool:
    try:
        result, cancelled = await _settle_statement(
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": RECOVERY_PURGE_ADVISORY_LOCK},
            )
        )
    except BaseException:
        raise RecoveryAuthorityReleaseFailed from None
    if not bool(result.scalar_one()):
        raise RecoveryAuthorityReleaseFailed
    return cancelled


async def _verify_source_recovery_anchor(
    connection,
    *,
    journal: TombstoneJournal,
    expected_source_installation_id: str,
    archive_tombstone_sequence: int,
) -> TombstoneSnapshot:
    actual_source = await source_installation_id(connection)
    if actual_source != expected_source_installation_id:
        raise SourceIdentityMismatch
    journal.bind_source_installation(actual_source)
    snapshot = await asyncio.to_thread(journal.snapshot, require_existing=True)
    state = (
        await connection.execute(
            text(
                """SELECT source_installation_id,journal_id,high_watermark,head_digest
                     FROM recovery_journal_state WHERE id=1"""
            )
        )
    ).one_or_none()
    rows = (
        await connection.execute(
            text(
                """SELECT journal_sequence,ciphertext_digest,record_digest,resource_kind,purge_status
                     FROM deletion_tombstones ORDER BY journal_sequence"""
            )
        )
    ).all()
    if state is None:
        if rows or snapshot.high_watermark != 0:
            raise TombstoneJournalUnavailable
        high_watermark = 0
        head_digest = "0" * 64
    else:
        high_watermark = int(state.high_watermark)
        head_digest = str(state.head_digest)
        if state.source_installation_id != actual_source or str(state.journal_id) != snapshot.journal_id or high_watermark != snapshot.high_watermark or len(rows) != high_watermark:
            raise TombstoneJournalUnavailable
    expected_head = "0" * 64
    for sequence, row in enumerate(rows, start=1):
        entry = snapshot.entries[sequence - 1]
        if int(row.journal_sequence) != sequence or row.ciphertext_digest != entry.ciphertext_digest or row.record_digest != entry.record_digest or row.resource_kind != entry.record.resource_kind or row.purge_status != "purged":
            raise TombstoneJournalUnavailable
        expected_head = entry.record_digest
    if head_digest != expected_head or archive_tombstone_sequence > high_watermark:
        raise TombstoneJournalUnavailable
    return snapshot


@asynccontextmanager
async def source_recovery_authority(
    database_url: str,
    *,
    journal: TombstoneJournal,
    expected_source_installation_id: str,
    archive_tombstone_sequence: int,
):
    engine = create_async_engine(DatabaseConfig(url=database_url).sqlalchemy_url)
    try:
        async with engine.connect() as connection:
            acquired = False
            try:
                _result, acquisition_cancelled = await _settle_statement(
                    connection.execute(
                        text("SELECT pg_advisory_lock(:lock_key)"),
                        {"lock_key": RECOVERY_PURGE_ADVISORY_LOCK},
                    )
                )
                acquired = True
                if acquisition_cancelled:
                    raise asyncio.CancelledError
                snapshot = await _verify_source_recovery_anchor(
                    connection,
                    journal=journal,
                    expected_source_installation_id=expected_source_installation_id,
                    archive_tombstone_sequence=archive_tombstone_sequence,
                )
                yield snapshot
            finally:
                if acquired:
                    release_cancelled = await _release_source_recovery_authority(connection)
                    if release_cancelled:
                        raise asyncio.CancelledError
    finally:
        await engine.dispose()


__all__ = [
    "RecoveryAuthorityReleaseFailed",
    "SourceIdentityMismatch",
    "source_recovery_authority",
]
