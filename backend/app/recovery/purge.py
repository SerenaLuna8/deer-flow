"""Journal-first retention purge transactions and replay deletion primitives."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.sinks import TrustedOperationAuditSink
from app.recovery.journal import (
    TombstoneEntry,
    TombstoneJournal,
    TombstoneJournalUnavailable,
    TombstoneReceipt,
    TombstoneRecord,
)
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.persistence.private_work.model import (
    PrivateArtifactRow,
    PrivateFileChunkRow,
    PrivateFileRow,
    RunAssetVersionRow,
    RunMcpGrantSnapshotRow,
    UserProjectMemoryFactRow,
    UserProjectMemoryRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.recovery.model import DeletionTombstoneRow

_RETENTION_DAYS = 30
_PURGE_ADVISORY_LOCK = 0x44465245434F5652
_PURGE_NAMESPACE = uuid.UUID("1960a83e-df43-4f8c-85f4-b7193c08a9d0")


class RetentionNotEligible(RuntimeError):
    def __init__(self) -> None:
        super().__init__("RETENTION_NOT_ELIGIBLE")


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retention timestamp must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    resource_kind: str
    project_id: uuid.UUID | None
    owner_user_id: str | None
    file_id: uuid.UUID | None
    project_ids: tuple[uuid.UUID, ...]
    eligibility_at: datetime
    idempotency_key: str
    request_id: str

    def __post_init__(self) -> None:
        if self.resource_kind not in {"project", "account", "file"}:
            raise ValueError("invalid retention resource kind")
        if not isinstance(self.idempotency_key, str) or not 1 <= len(self.idempotency_key) <= 256:
            raise ValueError("invalid retention idempotency key")
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 128:
            raise ValueError("invalid retention request id")
        object.__setattr__(self, "eligibility_at", _aware(self.eligibility_at))

    @classmethod
    def file(
        cls,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        file_id: uuid.UUID,
        deleted_at: datetime,
        idempotency_key: str,
        request_id: str,
    ) -> RetentionCandidate:
        return cls(
            "file",
            uuid.UUID(str(project_id)),
            str(uuid.UUID(str(owner_user_id))),
            uuid.UUID(str(file_id)),
            (),
            _aware(deleted_at) + timedelta(days=_RETENTION_DAYS),
            idempotency_key,
            request_id,
        )

    @classmethod
    def project(
        cls,
        *,
        project_id: uuid.UUID,
        deletion_effective_at: datetime,
        idempotency_key: str,
        request_id: str,
    ) -> RetentionCandidate:
        return cls(
            "project",
            uuid.UUID(str(project_id)),
            None,
            None,
            (),
            deletion_effective_at,
            idempotency_key,
            request_id,
        )

    @classmethod
    def account(
        cls,
        *,
        owner_user_id: str,
        project_ids: tuple[uuid.UUID, ...],
        retention_until: datetime,
        idempotency_key: str,
        request_id: str,
    ) -> RetentionCandidate:
        projects = tuple(sorted({uuid.UUID(str(value)) for value in project_ids}, key=str))
        if not projects:
            raise ValueError("account purge requires retained project scopes")
        return cls(
            "account",
            None,
            str(uuid.UUID(str(owner_user_id))),
            None,
            projects,
            retention_until,
            idempotency_key,
            request_id,
        )

    def tombstone(self) -> TombstoneRecord:
        return TombstoneRecord(
            resource_kind=self.resource_kind,
            project_id=self.project_id,
            owner_user_id=self.owner_user_id,
            file_id=self.file_id,
            project_ids=tuple(str(value) for value in self.project_ids),
            idempotency_key=self.idempotency_key,
        )

    @property
    def authority_id(self) -> uuid.UUID:
        if self.resource_kind == "project":
            assert self.project_id is not None
            return self.project_id
        if self.resource_kind == "file":
            assert self.file_id is not None
            return self.file_id
        assert self.owner_user_id is not None
        return uuid.UUID(self.owner_user_id)


class RetentionPurgeRepository:
    """Session-bound validation and deletion without transaction ownership."""

    async def verify_still_eligible(
        self,
        session: AsyncSession,
        candidate: RetentionCandidate,
        *,
        now: datetime,
    ) -> tuple[tuple[uuid.UUID, str | None], ...]:
        now = _aware(now)
        if now < candidate.eligibility_at:
            raise RetentionNotEligible
        if candidate.resource_kind == "file":
            assert candidate.project_id is not None
            assert candidate.owner_user_id is not None
            assert candidate.file_id is not None
            project = await session.scalar(select(ProjectRow).where(ProjectRow.id == candidate.project_id).with_for_update())
            if project is None:
                raise RetentionNotEligible
            membership = await session.scalar(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == candidate.project_id,
                    ProjectMembershipRow.user_id == candidate.owner_user_id,
                )
                .with_for_update()
            )
            row = await session.scalar(
                select(PrivateFileRow)
                .where(
                    PrivateFileRow.project_id == candidate.project_id,
                    PrivateFileRow.owner_user_id == candidate.owner_user_id,
                    PrivateFileRow.id == candidate.file_id,
                )
                .with_for_update()
            )
            if membership is None or row is None or row.status != "deleted" or row.deleted_at is None:
                raise RetentionNotEligible
            if _aware(row.deleted_at) + timedelta(days=_RETENTION_DAYS) != candidate.eligibility_at:
                raise RetentionNotEligible
            referenced = await session.scalar(
                select(func.count())
                .select_from(PrivateFileRow)
                .where(
                    PrivateFileRow.project_id == candidate.project_id,
                    PrivateFileRow.owner_user_id == candidate.owner_user_id,
                    PrivateFileRow.source_file_id == candidate.file_id,
                    PrivateFileRow.id != candidate.file_id,
                )
            )
            if referenced:
                raise RetentionNotEligible
            return ((candidate.project_id, candidate.owner_user_id),)

        if candidate.resource_kind == "project":
            assert candidate.project_id is not None
            project = await session.scalar(select(ProjectRow).where(ProjectRow.id == candidate.project_id).with_for_update())
            if project is None or project.status != "pending_deletion" or project.deletion_effective_at is None or _aware(project.deletion_effective_at) != candidate.eligibility_at or _aware(project.deletion_effective_at) > now:
                raise RetentionNotEligible
            memberships = (
                (await session.execute(select(ProjectMembershipRow).where(ProjectMembershipRow.project_id == candidate.project_id).order_by(ProjectMembershipRow.project_id, ProjectMembershipRow.user_id).with_for_update())).scalars().all()
            )
            return tuple((candidate.project_id, membership.user_id) for membership in memberships)

        assert candidate.owner_user_id is not None
        memberships = (
            (await session.execute(select(ProjectMembershipRow).where(ProjectMembershipRow.user_id == candidate.owner_user_id).order_by(ProjectMembershipRow.project_id, ProjectMembershipRow.user_id).with_for_update())).scalars().all()
        )
        actual_projects = tuple(sorted((membership.project_id for membership in memberships), key=str))
        if actual_projects != candidate.project_ids or not memberships:
            raise RetentionNotEligible
        if any(membership.status == "active" or membership.retention_until is None or _aware(membership.retention_until) > now for membership in memberships):
            raise RetentionNotEligible
        maximum_retention = max(_aware(membership.retention_until) for membership in memberships if membership.retention_until is not None)
        if maximum_retention != candidate.eligibility_at:
            raise RetentionNotEligible
        return tuple((membership.project_id, candidate.owner_user_id) for membership in memberships)

    async def physically_purge(
        self,
        session: AsyncSession,
        candidate: RetentionCandidate,
    ) -> int:
        if candidate.resource_kind == "file":
            assert candidate.project_id is not None
            assert candidate.owner_user_id is not None
            assert candidate.file_id is not None
            await session.execute(
                delete(PrivateArtifactRow).where(
                    PrivateArtifactRow.project_id == candidate.project_id,
                    PrivateArtifactRow.owner_user_id == candidate.owner_user_id,
                    PrivateArtifactRow.file_id == candidate.file_id,
                )
            )
            await session.execute(delete(PrivateFileChunkRow).where(PrivateFileChunkRow.file_id == candidate.file_id))
            result = await session.execute(
                delete(PrivateFileRow).where(
                    PrivateFileRow.project_id == candidate.project_id,
                    PrivateFileRow.owner_user_id == candidate.owner_user_id,
                    PrivateFileRow.id == candidate.file_id,
                )
            )
            if result.rowcount != 1:
                raise RetentionNotEligible
            return 1
        if candidate.resource_kind == "project":
            assert candidate.project_id is not None
            await purge_private_scope(session, project_id=candidate.project_id, owner_user_id=None)
            return 1
        assert candidate.owner_user_id is not None
        for project_id in candidate.project_ids:
            await purge_private_scope(
                session,
                project_id=project_id,
                owner_user_id=candidate.owner_user_id,
            )
        return len(candidate.project_ids)


async def purge_private_scope(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    owner_user_id: str | None,
) -> None:
    """Delete/scrub private payload for one exact project or project+owner scope.

    Immutable jobs, audit rows, governance rows, and their minimal FK shells are
    retained.  Rows with no immutable references are physically removed.
    """

    parameters: dict[str, object] = {"project_id": project_id, "purged_at": datetime.now(UTC)}
    owner_clause = ""
    if owner_user_id is not None:
        owner_clause = " AND owner_user_id = :owner_user_id"
        parameters["owner_user_id"] = owner_user_id

    def owner_for(alias: str) -> str:
        return "" if owner_user_id is None else f" AND {alias}.owner_user_id = :owner_user_id"

    # Connection credentials/conversations cascade from exact connection rows.
    await session.execute(
        text(f"DELETE FROM channel_oauth_states WHERE project_id=:project_id{owner_clause}"),
        parameters,
    )
    await session.execute(
        text(f"DELETE FROM channel_conversations WHERE project_id=:project_id{owner_clause}"),
        parameters,
    )
    await session.execute(
        text(f"DELETE FROM channel_connections WHERE project_id=:project_id{owner_clause}"),
        parameters,
    )

    await session.execute(
        delete(UserProjectMemoryFactRow).where(
            UserProjectMemoryFactRow.project_id == project_id,
            *(() if owner_user_id is None else (UserProjectMemoryFactRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(UserProjectMemoryRow).where(
            UserProjectMemoryRow.project_id == project_id,
            *(() if owner_user_id is None else (UserProjectMemoryRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(RunMcpGrantSnapshotRow).where(
            RunMcpGrantSnapshotRow.project_id == project_id,
            *(() if owner_user_id is None else (RunMcpGrantSnapshotRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(RunAssetVersionRow).where(
            RunAssetVersionRow.project_id == project_id,
            *(() if owner_user_id is None else (RunAssetVersionRow.owner_user_id == owner_user_id,)),
        )
    )
    await session.execute(
        delete(PrivateArtifactRow).where(
            PrivateArtifactRow.project_id == project_id,
            *(() if owner_user_id is None else (PrivateArtifactRow.owner_user_id == owner_user_id,)),
        )
    )
    file_ids = select(PrivateFileRow.id).where(
        PrivateFileRow.project_id == project_id,
        *(() if owner_user_id is None else (PrivateFileRow.owner_user_id == owner_user_id,)),
    )
    await session.execute(delete(PrivateFileChunkRow).where(PrivateFileChunkRow.file_id.in_(file_ids)))
    await session.execute(
        update(PrivateFileRow)
        .where(
            PrivateFileRow.project_id == project_id,
            *(() if owner_user_id is None else (PrivateFileRow.owner_user_id == owner_user_id,)),
        )
        .values(source_file_id=None)
    )
    await session.execute(
        delete(PrivateFileRow).where(
            PrivateFileRow.project_id == project_id,
            *(() if owner_user_id is None else (PrivateFileRow.owner_user_id == owner_user_id,)),
        )
    )

    # Checkpoint tables are LangGraph-owned and intentionally addressed only by
    # the exact private Thread coordinates collected in this scope.
    thread_predicate = "project_id=:project_id" + owner_clause
    thread_ids = f"SELECT thread_id FROM threads_meta WHERE {thread_predicate}"
    for checkpoint_table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        if await session.scalar(text("SELECT to_regclass(:table_name)"), {"table_name": checkpoint_table}) is not None:
            await session.execute(
                text(f"DELETE FROM {checkpoint_table} WHERE thread_id IN ({thread_ids})"),
                parameters,
            )

    await session.execute(text(f"DELETE FROM run_events WHERE project_id=:project_id{owner_clause}"), parameters)
    await session.execute(text(f"DELETE FROM feedback WHERE project_id=:project_id{owner_clause}"), parameters)

    # Automation rows with immutable job references retain a scrubbed shell;
    # unreferenced rows are physically removed.
    await session.execute(
        text(
            f"""DELETE FROM scheduled_task_runs occurrence
                 WHERE occurrence.project_id=:project_id{owner_for("occurrence")}
                   AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.automation_occurrence_id=occurrence.id)"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""DELETE FROM scheduled_tasks task
                 WHERE task.project_id=:project_id{owner_for("task")}
                   AND NOT EXISTS (SELECT 1 FROM scheduled_task_runs occurrence WHERE occurrence.task_id=task.id)"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""UPDATE scheduled_tasks task
                    SET title='purged', prompt='', schedule_spec='{{}}'::json,
                        status='cancelled', next_run_at=NULL, deleted_at=:purged_at,
                        updated_at=:purged_at
                  WHERE task.project_id=:project_id{owner_for("task")}"""
        ),
        parameters,
    )

    # Runs referenced by immutable jobs/audit are scrubbed, while unreferenced
    # Runs and then empty Thread shells are physically removed.
    await session.execute(
        text(
            f"""DELETE FROM runs run
                 WHERE run.project_id=:project_id{owner_for("run")}
                   AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.run_id=run.run_id AND jobs.project_id=run.project_id)"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""UPDATE runs run
                    SET assistant_id=NULL, metadata_json='{{}}'::json, kwargs_json='{{}}'::json,
                        error=NULL, first_human_message=NULL, last_ai_message=NULL
                  WHERE run.project_id=:project_id{owner_for("run")}"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""DELETE FROM threads_meta thread
                 WHERE thread.project_id=:project_id{owner_for("thread")}
                   AND NOT EXISTS (SELECT 1 FROM runs WHERE runs.thread_id=thread.thread_id)
                   AND NOT EXISTS (SELECT 1 FROM scheduled_tasks WHERE scheduled_tasks.thread_id=thread.thread_id)
                   AND NOT EXISTS (SELECT 1 FROM channel_conversations WHERE channel_conversations.thread_id=thread.thread_id)"""
        ),
        parameters,
    )
    await session.execute(
        text(
            f"""UPDATE threads_meta thread
                    SET assistant_id=NULL, display_name=NULL, metadata_json='{{}}'::json,
                        frozen_at=:purged_at, deleted_at=:purged_at,
                        checkpoint_delete_status='complete', updated_at=:purged_at
                  WHERE thread.project_id=:project_id{owner_for("thread")}"""
        ),
        parameters,
    )


async def apply_tombstone_record(
    session: AsyncSession,
    record: TombstoneRecord,
) -> int:
    """Apply authenticated replay coordinates without retention re-evaluation."""

    if record.resource_kind == "file":
        project_id = uuid.UUID(str(record.project_id))
        owner_user_id = str(record.owner_user_id)
        file_id = uuid.UUID(str(record.file_id))
        await session.execute(
            delete(PrivateArtifactRow).where(
                PrivateArtifactRow.project_id == project_id,
                PrivateArtifactRow.owner_user_id == owner_user_id,
                PrivateArtifactRow.file_id == file_id,
            )
        )
        await session.execute(delete(PrivateFileChunkRow).where(PrivateFileChunkRow.file_id == file_id))
        await session.execute(
            delete(PrivateFileRow).where(
                PrivateFileRow.project_id == project_id,
                PrivateFileRow.owner_user_id == owner_user_id,
                PrivateFileRow.id == file_id,
            )
        )
        return 1
    if record.resource_kind == "project":
        await purge_private_scope(
            session,
            project_id=uuid.UUID(str(record.project_id)),
            owner_user_id=None,
        )
        return 1
    for project in record.project_ids:
        await purge_private_scope(
            session,
            project_id=uuid.UUID(str(project)),
            owner_user_id=str(record.owner_user_id),
        )
    return len(record.project_ids)


class RetentionPurger:
    def __init__(
        self,
        sessions: async_sessionmaker,
        *,
        journal: TombstoneJournal,
        keyring: AuditHmacKeyring,
        audit: TrustedOperationAuditSink,
        repository: RetentionPurgeRepository | None = None,
    ) -> None:
        if type(journal) is not TombstoneJournal or type(keyring) is not AuditHmacKeyring or type(audit) is not TrustedOperationAuditSink:
            raise TypeError("retention purge requires journal, HMAC, and audit authority")
        self._sessions = sessions
        self.journal = journal
        self._keyring = keyring
        self._audit = audit
        self.repository = repository or RetentionPurgeRepository()

    @staticmethod
    async def _database_prefix(session: AsyncSession) -> int:
        row = (
            await session.execute(
                select(
                    func.count(DeletionTombstoneRow.journal_sequence),
                    func.min(DeletionTombstoneRow.journal_sequence),
                    func.max(DeletionTombstoneRow.journal_sequence),
                )
            )
        ).one()
        count, minimum, maximum = int(row[0]), row[1], row[2]
        if count == 0:
            if minimum is not None or maximum is not None:
                raise TombstoneJournalUnavailable
            return 0
        if int(minimum) != 1 or int(maximum) != count:
            raise TombstoneJournalUnavailable
        return count

    async def purge(
        self,
        candidate: RetentionCandidate,
        *,
        now: datetime | None = None,
    ) -> TombstoneReceipt:
        if type(candidate) is not RetentionCandidate:
            raise TypeError("retention candidate is required")
        purged_at = _aware(now or datetime.now(UTC))
        record = candidate.tombstone()
        async with self._sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _PURGE_ADVISORY_LOCK},
            )
            database_prefix = await self._database_prefix(session)
            snapshot = await asyncio.to_thread(self.journal.snapshot)
            if snapshot.high_watermark < database_prefix or snapshot.high_watermark > database_prefix + 1:
                raise TombstoneJournalUnavailable
            pending = next(
                (entry for entry in snapshot.entries if entry.record.idempotency_key == candidate.idempotency_key),
                None,
            )
            if pending is not None and pending.record != record:
                raise TombstoneJournalUnavailable
            if pending is not None and pending.sequence <= database_prefix:
                row = await session.get(DeletionTombstoneRow, pending.sequence)
                if row is None or row.ciphertext_digest != pending.ciphertext_digest or row.resource_kind != candidate.resource_kind or row.purge_status != "purged":
                    raise TombstoneJournalUnavailable
                return pending.receipt
            if snapshot.high_watermark == database_prefix + 1 and pending is None:
                raise TombstoneJournalUnavailable

            await self.repository.verify_still_eligible(session, candidate, now=purged_at)
            receipt = await asyncio.to_thread(
                self.journal.append_and_fsync,
                record,
                committed_sequence=database_prefix,
            )
            if receipt.sequence != database_prefix + 1:
                raise TombstoneJournalUnavailable
            purged_count = await self.repository.physically_purge(session, candidate)
            resource_ref = self._keyring.audit_target_ref("purge", candidate.authority_id)
            session.add(
                DeletionTombstoneRow(
                    journal_sequence=receipt.sequence,
                    ciphertext_digest=receipt.ciphertext_digest,
                    resource_kind=candidate.resource_kind,
                    resource_ref_key_id=resource_ref.key_id,
                    resource_ref_hmac=resource_ref.hmac_hex,
                    purge_status="purged",
                    committed_at=purged_at,
                    purged_at=purged_at,
                )
            )
            await session.flush()
            await self._audit.purge_completed(
                session,
                purge_id=uuid.uuid5(_PURGE_NAMESPACE, receipt.record_digest),
                project_id=None if candidate.resource_kind == "account" else candidate.project_id,
                resource_kind=candidate.resource_kind,
                purged_count=purged_count,
                request_id=candidate.request_id,
            )
            return receipt


async def apply_replay_entry(
    session: AsyncSession,
    entry: TombstoneEntry,
    *,
    keyring: AuditHmacKeyring,
) -> bool:
    existing = await session.get(DeletionTombstoneRow, entry.sequence)
    if existing is not None:
        if existing.ciphertext_digest != entry.ciphertext_digest or existing.resource_kind != entry.record.resource_kind or existing.purge_status != "purged":
            raise TombstoneJournalUnavailable
        return False
    await apply_tombstone_record(session, entry.record)
    authority = uuid.UUID(str(entry.record.project_id)) if entry.record.resource_kind == "project" else uuid.UUID(str(entry.record.file_id)) if entry.record.resource_kind == "file" else uuid.UUID(str(entry.record.owner_user_id))
    reference = keyring.audit_target_ref("purge", authority)
    session.add(
        DeletionTombstoneRow(
            journal_sequence=entry.sequence,
            ciphertext_digest=entry.ciphertext_digest,
            resource_kind=entry.record.resource_kind,
            resource_ref_key_id=reference.key_id,
            resource_ref_hmac=reference.hmac_hex,
            purge_status="purged",
            committed_at=datetime.now(UTC),
            purged_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return True


__all__ = [
    "RetentionCandidate",
    "RetentionNotEligible",
    "RetentionPurgeRepository",
    "RetentionPurger",
    "apply_replay_entry",
    "apply_tombstone_record",
    "purge_private_scope",
]
