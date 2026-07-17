from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from support.m4_private_threads import seed_m4_thread_database

from app.audit.service import AuditService, _bind_recovery_audit_process
from app.audit.sinks import TrustedOperationAuditSink
from app.private_work.retention import PrivateWorkRetentionService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.recovery.journal import TombstoneJournal, TombstoneJournalUnavailable
from app.recovery.purge import (
    RetentionCandidate,
    RetentionNotEligible,
    RetentionPurger,
    RetentionPurgeRepository,
)
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.private_work.model import PrivateFileRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.recovery.model import DeletionTombstoneRow
from deerflow.persistence.user.model import UserRow

NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)
EXPIRED = NOW - timedelta(days=31)
NOT_EXPIRED = NOW - timedelta(days=29)


@pytest.mark.anyio
async def test_private_work_retention_service_exposes_the_journal_first_purge_boundary() -> None:
    candidate = RetentionCandidate.file(
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        file_id=uuid.uuid4(),
        deleted_at=EXPIRED,
        idempotency_key="retention-service-boundary",
        request_id="task17-retention-service",
    )
    calls: list[tuple[RetentionCandidate, datetime | None]] = []

    class _Purger:
        async def purge(self, value: RetentionCandidate, *, now: datetime | None = None) -> str:
            calls.append((value, now))
            return "durable-receipt"

    result = await PrivateWorkRetentionService.purge_expired(
        _Purger(),  # type: ignore[arg-type]
        candidate,
        now=NOW,
    )

    assert result == "durable-receipt"
    assert calls == [(candidate, NOW)]


def _keyring() -> AuditHmacKeyring:
    return AuditHmacKeyring(active_key_id="audit-v1", _keys={"audit-v1": b"a" * 32})


def _audit(factory) -> TrustedOperationAuditSink:
    service = AuditService(factory, _keyring())
    return TrustedOperationAuditSink(
        service,
        process_context=_bind_recovery_audit_process(service),
    )


async def _seed_deleted_file(seed, *, context, thread_id: str, deleted_at: datetime) -> uuid.UUID:
    file_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=context.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        session.add(
            PrivateFileRow(
                id=file_id,
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                kind="upload",
                logical_path=f"{file_id}.txt",
                media_type="text/plain",
                size=4,
                sha256="0" * 64,
                status="deleted",
                deleted_at=deleted_at,
            )
        )
        await session.flush()
        await session.execute(
            text(
                """INSERT INTO file_chunks (file_id,chunk_index,content,size,sha256)
                   VALUES (:file_id,0,:content,4,:sha256)"""
            ),
            {"file_id": file_id, "content": b"data", "sha256": "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7"},
        )
    return file_id


def _file_candidate(seed, file_id: uuid.UUID, *, deleted_at: datetime = EXPIRED) -> RetentionCandidate:
    return RetentionCandidate.file(
        project_id=seed.owner_a.project_id,
        owner_user_id=str(seed.owner_a.user_id),
        file_id=file_id,
        deleted_at=deleted_at,
        idempotency_key=f"file:{file_id}",
        request_id="task17-file-purge",
    )


def _purger(seed, tmp_path: Path) -> RetentionPurger:
    return RetentionPurger(
        seed.factory,
        journal=TombstoneJournal(tmp_path / "operator" / "tombstones.jsonl", b"j" * 32),
        keyring=_keyring(),
        audit=_audit(seed.factory),
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_purge_aborts_when_journal_fsync_fails_and_keeps_private_rows(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        file_id = await _seed_deleted_file(seed, context=seed.owner_a, thread_id=f"purge-{uuid.uuid4()}", deleted_at=EXPIRED)
        purger = _purger(seed, tmp_path)

        def fail(*_args, **_kwargs):
            raise TombstoneJournalUnavailable()

        monkeypatch.setattr(purger.journal, "append_and_fsync", fail)
        with pytest.raises(TombstoneJournalUnavailable):
            await purger.purge(_file_candidate(seed, file_id), now=NOW)

        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, file_id) is not None
            assert (await session.execute(select(DeletionTombstoneRow))).scalars().all() == []
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_file_purge_relocks_30_day_state_and_deletes_only_exact_scope(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        target = await _seed_deleted_file(seed, context=seed.owner_a, thread_id=f"target-{uuid.uuid4()}", deleted_at=EXPIRED)
        same_owner = await _seed_deleted_file(seed, context=seed.owner_a, thread_id=f"same-{uuid.uuid4()}", deleted_at=EXPIRED)
        other_owner = await _seed_deleted_file(seed, context=seed.owner_b, thread_id=f"other-{uuid.uuid4()}", deleted_at=EXPIRED)
        too_early = await _seed_deleted_file(seed, context=seed.owner_a, thread_id=f"early-{uuid.uuid4()}", deleted_at=NOT_EXPIRED)
        purger = _purger(seed, tmp_path)

        with pytest.raises(RetentionNotEligible):
            await purger.purge(_file_candidate(seed, too_early, deleted_at=NOT_EXPIRED), now=NOW)
        receipt = await purger.purge(_file_candidate(seed, target), now=NOW)

        assert receipt.sequence == 1
        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, target) is None
            assert await session.get(PrivateFileRow, same_owner) is not None
            assert await session.get(PrivateFileRow, other_owner) is not None
            assert await session.get(PrivateFileRow, too_early) is not None
            row = await session.get(DeletionTombstoneRow, 1)
            assert row is not None
            assert row.resource_kind == "file"
            assert row.purge_status == "purged"
            assert row.ciphertext_digest == receipt.ciphertext_digest
            assert str(target) not in repr(row.__dict__)
            audit = (await session.execute(select(AuditLogRow).where(AuditLogRow.action == "purge.completed"))).scalar_one()
            assert audit.metadata_json == {"resource_kind": "file", "purged_count": 1}
            assert str(target) not in repr(audit.__dict__)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_journal_success_database_failure_is_retryable_with_same_sequence(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        file_id = await _seed_deleted_file(seed, context=seed.owner_a, thread_id=f"retry-{uuid.uuid4()}", deleted_at=EXPIRED)
        purger = _purger(seed, tmp_path)
        real_purge = RetentionPurgeRepository.physically_purge
        attempts = 0

        async def fail_once(self, session, candidate):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("database write failed")
            return await real_purge(self, session, candidate)

        monkeypatch.setattr(RetentionPurgeRepository, "physically_purge", fail_once)
        with pytest.raises(RuntimeError, match="database write failed"):
            await purger.purge(_file_candidate(seed, file_id), now=NOW)
        assert purger.journal.snapshot().high_watermark == 1
        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, file_id) is not None
            assert await session.get(DeletionTombstoneRow, 1) is None

        receipt = await purger.purge(_file_candidate(seed, file_id), now=NOW)
        assert receipt.sequence == 1
        assert purger.journal.snapshot().high_watermark == 1
        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, file_id) is None
            assert await session.get(DeletionTombstoneRow, 1) is not None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_purge_revalidates_pending_deletion_and_preserves_other_project(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        project_file = await _seed_deleted_file(seed, context=seed.owner_a, thread_id=f"project-a-{uuid.uuid4()}", deleted_at=EXPIRED)
        other_file = await _seed_deleted_file(seed, context=seed.project_b_owner_a, thread_id=f"project-b-{uuid.uuid4()}", deleted_at=EXPIRED)
        purger = _purger(seed, tmp_path)
        candidate = RetentionCandidate.project(
            project_id=seed.owner_a.project_id,
            deletion_effective_at=EXPIRED,
            idempotency_key=f"project:{seed.owner_a.project_id}",
            request_id="task17-project-purge",
        )

        with pytest.raises(RetentionNotEligible):
            await purger.purge(candidate, now=NOW)
        async with seed.factory() as session, session.begin():
            await session.execute(update(ProjectRow).where(ProjectRow.id == seed.owner_a.project_id).values(status="pending_deletion", deletion_requested_at=EXPIRED, deletion_effective_at=EXPIRED))

        await purger.purge(candidate, now=NOW)
        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, project_file) is None
            assert await session.get(PrivateFileRow, other_file) is not None
            # Governance/audit shells are retained; online private rows are purged.
            assert await session.get(ProjectRow, seed.owner_a.project_id) is not None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_purge_is_recovery_only_and_requires_every_membership_expired(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        project_a_file = await _seed_deleted_file(seed, context=seed.owner_a, thread_id=f"account-a-{uuid.uuid4()}", deleted_at=EXPIRED)
        project_b_file = await _seed_deleted_file(seed, context=seed.project_b_owner_a, thread_id=f"account-b-{uuid.uuid4()}", deleted_at=EXPIRED)
        other_owner_file = await _seed_deleted_file(seed, context=seed.owner_b, thread_id=f"account-other-{uuid.uuid4()}", deleted_at=EXPIRED)
        project_ids = tuple(sorted((seed.owner_a.project_id, seed.project_b_owner_a.project_id), key=str))
        candidate = RetentionCandidate.account(
            owner_user_id=str(seed.owner_a.user_id),
            project_ids=project_ids,
            retention_until=EXPIRED,
            idempotency_key=f"account:{seed.owner_a.user_id}",
            request_id="task17-account-purge",
        )
        purger = _purger(seed, tmp_path)

        with pytest.raises(RetentionNotEligible):
            await purger.purge(candidate, now=NOW)

        async with seed.factory() as session, session.begin():
            await session.execute(
                update(ProjectMembershipRow).where(ProjectMembershipRow.user_id == str(seed.owner_a.user_id)).values(status="left", ended_at=EXPIRED, retention_until=EXPIRED, end_reason="left", version=ProjectMembershipRow.version + 1)
            )

        await purger.purge(candidate, now=NOW)
        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, project_a_file) is None
            assert await session.get(PrivateFileRow, project_b_file) is None
            assert await session.get(PrivateFileRow, other_owner_file) is not None
            assert await session.get(UserRow, str(seed.owner_a.user_id)) is not None
            memberships = (await session.execute(select(ProjectMembershipRow).where(ProjectMembershipRow.user_id == str(seed.owner_a.user_id)))).scalars().all()
            assert len(memberships) == 2
            audit = (await session.execute(select(AuditLogRow).where(AuditLogRow.action == "purge.completed"))).scalar_one()
            assert audit.project_id is None
            assert audit.metadata_json == {"resource_kind": "account", "purged_count": 2}
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_rejoin_race_fails_closed_after_candidate_creation(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        file_id = await _seed_deleted_file(seed, context=seed.owner_a, thread_id=f"race-{uuid.uuid4()}", deleted_at=EXPIRED)
        async with seed.factory() as session, session.begin():
            await session.execute(
                update(ProjectMembershipRow).where(ProjectMembershipRow.user_id == str(seed.owner_a.user_id)).values(status="left", ended_at=EXPIRED, retention_until=EXPIRED, end_reason="left", version=ProjectMembershipRow.version + 1)
            )
        project_ids = tuple(sorted((seed.owner_a.project_id, seed.project_b_owner_a.project_id), key=str))
        candidate = RetentionCandidate.account(
            owner_user_id=str(seed.owner_a.user_id),
            project_ids=project_ids,
            retention_until=EXPIRED,
            idempotency_key=f"account-race:{seed.owner_a.user_id}",
            request_id="task17-account-race",
        )
        async with seed.factory() as session, session.begin():
            await session.execute(
                update(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.user_id == str(seed.owner_a.user_id),
                    ProjectMembershipRow.project_id == seed.owner_a.project_id,
                )
                .values(status="active", ended_at=None, retention_until=None, end_reason=None, version=ProjectMembershipRow.version + 1)
            )

        with pytest.raises(RetentionNotEligible):
            await _purger(seed, tmp_path).purge(candidate, now=NOW)
        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, file_id) is not None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_cancellation_during_journal_fsync_rolls_back_and_keeps_rows(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        file_id = await _seed_deleted_file(seed, context=seed.owner_a, thread_id=f"cancel-{uuid.uuid4()}", deleted_at=EXPIRED)
        purger = _purger(seed, tmp_path)
        entered = threading.Event()
        release = threading.Event()
        real_append = purger.journal.append_and_fsync

        def delayed(record, *, committed_sequence):
            entered.set()
            release.wait(timeout=5)
            return real_append(record, committed_sequence=committed_sequence)

        monkeypatch.setattr(purger.journal, "append_and_fsync", delayed)
        task = asyncio.create_task(purger.purge(_file_candidate(seed, file_id), now=NOW))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, file_id) is not None
            assert (await session.execute(select(DeletionTombstoneRow))).scalars().all() == []
    finally:
        await seed.engine.dispose()
