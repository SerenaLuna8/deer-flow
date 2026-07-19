"""Fail-closed new-database restore, tombstone replay, and recovery drill."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit.service import AuditService, _bind_recovery_audit_process
from app.audit.sinks import TrustedOperationAuditSink
from app.recovery import (
    BackupArchiveReader,
    BackupAuthenticationFailed,
    BackupManifest,
)
from app.recovery.archive import (
    ARCHIVE_SCHEMA_VERSION,
    M7_CANONICAL_SCHEMA_DIGEST,
    UnsupportedArchiveSchema,
    require_supported_archive,
)
from app.recovery.authority import (
    RecoveryAuthorityReleaseFailed,
    SourceIdentityMismatch,
)
from app.recovery.authority import (
    source_recovery_authority as _source_recovery_authority,
)
from app.recovery.cleanup import (
    OwnedFile,
    SensitiveCleanupFailed,
    _cleanup_owned_workspace,
    _create_owned_file,
    _create_owned_workspace,
    _settle_async_cleanup,
    _settle_blocking_cleanup,
    _settle_blocking_result,
)
from app.recovery.identity import source_installation_id
from app.recovery.journal import (
    TombstoneJournal,
    TombstoneJournalUnavailable,
    TombstoneRecord,
    TombstoneSnapshot,
)
from app.recovery.purge import apply_replay_entry
from app.recovery.restore_process import (
    RestoreCommandFailed,
)
from app.recovery.restore_process import (
    run_pg_restore as _run_pg_restore,
)
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.bootstrap import (
    M7_FINAL_SCHEMA_REVISION,
    M7RecreateRequired,
    classify_database,
)
from deerflow.persistence.recovery.model import (
    DeletionTombstoneRow,
    RecoveryJournalStateRow,
    RestoreProofRow,
)

_RESTORE_DATABASE = re.compile(r"deerflow_restore_[0-9]+_[0-9a-f]{32}\Z")
_REQUIRED_TABLES = frozenset(
    {
        "alembic_version",
        "users",
        "projects",
        "project_memberships",
        "threads_meta",
        "runs",
        "run_events",
        "files",
        "file_chunks",
        "user_project_memories",
        "user_project_memory_facts",
        "channel_connections",
        "scheduled_tasks",
        "scheduled_task_runs",
        "jobs",
        "job_attempts",
        "dead_jobs",
        "worker_nodes",
        "project_quotas",
        "project_usage_counters",
        "project_usage_ledger",
        "audit_logs",
        "deletion_tombstones",
        "recovery_journal_state",
        "restore_proofs",
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "store_migrations",
        "store",
    }
)
_REQUIRED_CONSTRAINTS = frozenset(
    {
        "uq_project_memberships_project_user",
        "uq_threads_meta_private_scope",
        "uq_runs_private_scope",
        "uq_run_events_private_seq",
        "uq_files_private_scope",
        "uq_jobs_type_idempotency",
        "uq_project_usage_ledger_idempotency",
        "ck_deletion_tombstones_sequence",
        "ck_recovery_journal_state_singleton",
        "ck_restore_proofs_sequences",
    }
)
_TARGET_REF_NAMESPACE = uuid.UUID("a0658bb5-af1b-47ae-8278-af299dd8aeed")


class RestoreAuthenticationFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("RESTORE_AUTHENTICATION_FAILED")


class RestoreTargetRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("RESTORE_TARGET_REJECTED")


class RecoveryProbeFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("RECOVERY_PROBE_FAILED")


@dataclass(frozen=True, slots=True)
class _AuthenticatedArchive:
    archive_id: str
    archive_schema_version: int
    schema_revision: str
    schema_digest: str
    source_installation_id: str
    tombstone_journal_sequence: int
    table_count: int
    archive_digest: str
    dump_path: Path
    dump_identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class RestoreResult:
    proof_id: uuid.UUID
    archive_id: str
    archive_schema_version: int
    schema_revision: str
    schema_digest: str
    table_count: int
    tombstones_replayed: int
    replayed_through_sequence: int
    probes_complete: bool
    status: str
    checksum: str
    _handoff_token: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class RestoreConfig:
    archive: Path
    target_database_url: str
    current_database_url: str
    journal: TombstoneJournal
    backup_key: bytes
    keyring: AuditHmacKeyring

    def __post_init__(self) -> None:
        if not isinstance(self.archive, Path):
            object.__setattr__(self, "archive", Path(self.archive))
        if not isinstance(self.target_database_url, str) or not self.target_database_url:
            raise RestoreTargetRejected
        if not isinstance(self.current_database_url, str) or not self.current_database_url:
            raise RestoreTargetRejected
        if type(self.journal) is not TombstoneJournal or type(self.keyring) is not AuditHmacKeyring:
            raise TypeError("restore requires journal and HMAC authority")
        if not isinstance(self.backup_key, bytes) or len(self.backup_key) != 32:
            raise RestoreAuthenticationFailed


def database_name(database_url: str) -> str:
    try:
        name = make_url(database_url).database
    except Exception:
        raise RestoreTargetRejected from None
    if not name:
        raise RestoreTargetRejected
    return name


def _same_database(left: str, right: str) -> bool:
    try:
        first, second = make_url(left), make_url(right)
        return (first.host or "") == (second.host or "") and (first.port or 5432) == (second.port or 5432) and (first.database or "") == (second.database or "")
    except Exception:
        raise RestoreTargetRejected from None


def _validate_target(config: RestoreConfig) -> str:
    target = database_name(config.target_database_url)
    if _RESTORE_DATABASE.fullmatch(target) is None:
        raise RestoreTargetRejected
    if _same_database(config.target_database_url, config.current_database_url):
        raise RestoreTargetRejected
    active = os.getenv("DATABASE_URL")
    if active and _same_database(config.target_database_url, active):
        raise RestoreTargetRejected
    return target


def _read_manifest_bytes(archive: Path) -> bytes:
    path = archive / "manifest.json"
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size < 1 or info.st_size > 16 * 1024 * 1024:
            raise RestoreAuthenticationFailed
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                raise RestoreAuthenticationFailed
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)
    except OSError:
        raise RestoreAuthenticationFailed from None
    finally:
        os.close(descriptor)


def _parse_authenticated_manifest(
    raw: bytes,
) -> tuple[str, int, str, str, str, int, int, str]:
    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {
            "manifest",
            "signature",
        }:
            raise RestoreAuthenticationFailed
        manifest = BackupManifest.model_validate(envelope["manifest"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise RestoreAuthenticationFailed from None
    return (
        manifest.archive_id,
        manifest.archive_schema_version,
        manifest.schema_revision,
        manifest.schema_digest,
        manifest.source_installation_id,
        manifest.tombstone_journal_sequence,
        manifest.table_count,
        hashlib.sha256(raw).hexdigest(),
    )


def _authenticate_archive(
    archive: Path,
    key: bytes,
    dump: OwnedFile,
) -> _AuthenticatedArchive:
    before = _read_manifest_bytes(archive)
    parent = os.open(
        dump.path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor = -1
    try:
        descriptor = os.open(
            dump.path.name,
            os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        dump_info = os.fstat(descriptor)
        if not stat.S_ISREG(dump_info.st_mode) or (dump_info.st_dev, dump_info.st_ino) != dump.identity:
            raise RestoreAuthenticationFailed
        os.ftruncate(descriptor, 0)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            for chunk in BackupArchiveReader(key).verified_chunks(archive):
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        after = _read_manifest_bytes(archive)
        if before != after:
            raise RestoreAuthenticationFailed
        (
            archive_id,
            archive_schema_version,
            revision,
            schema_digest,
            source_id,
            sequence,
            table_count,
            digest,
        ) = _parse_authenticated_manifest(after)
        authenticated = _AuthenticatedArchive(
            archive_id,
            archive_schema_version,
            revision,
            schema_digest,
            source_id,
            sequence,
            table_count,
            digest,
            dump.path,
            dump.identity,
        )
        require_supported_archive(authenticated)
        return authenticated
    except BackupAuthenticationFailed:
        raise RestoreAuthenticationFailed from None
    except OSError:
        raise RestoreAuthenticationFailed from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _maintenance_url(database_url: str) -> str:
    try:
        parsed = make_url(database_url)
        if parsed.drivername == "postgresql":
            parsed = parsed.set(drivername="postgresql+asyncpg")
        return parsed.set(database="postgres").render_as_string(hide_password=False)
    except Exception:
        raise RestoreTargetRejected from None


async def _database_exists(database_url: str, database: str) -> bool:
    engine = create_async_engine(_maintenance_url(database_url), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            return bool(
                await connection.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=:database)"),
                    {"database": database},
                )
            )
    except DBAPIError:
        raise RestoreTargetRejected from None
    finally:
        await engine.dispose()


async def _create_empty_database(database_url: str, database: str) -> None:
    if _RESTORE_DATABASE.fullmatch(database) is None:
        raise RestoreTargetRejected
    created = False
    try:
        engine = create_async_engine(_maintenance_url(database_url), isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                if await connection.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=:database)"),
                    {"database": database},
                ):
                    raise RestoreTargetRejected
                creation = asyncio.create_task(connection.execute(text(f"CREATE DATABASE \"{database}\" TEMPLATE template0 ENCODING 'UTF8'")))
                cancelled = False
                while not creation.done():
                    try:
                        await asyncio.shield(creation)
                    except asyncio.CancelledError:
                        cancelled = True
                try:
                    creation.result()
                except DBAPIError:
                    raise RestoreTargetRejected from None
                created = True
                if cancelled:
                    raise asyncio.CancelledError
        finally:
            await engine.dispose()
        target = create_async_engine(DatabaseConfig(url=database_url).sqlalchemy_url)
        try:
            async with target.connect() as connection:
                count = await connection.scalar(
                    text(
                        """SELECT count(*) FROM pg_class
                             WHERE relnamespace=current_schema()::regnamespace
                               AND relkind IN ('r','p')"""
                    )
                )
                if int(count or 0) != 0:
                    raise RestoreTargetRejected
        finally:
            await target.dispose()
    except BaseException:
        if created:
            cleanup = asyncio.create_task(_drop_database_on_target(database_url, database))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
        raise


async def _drop_database_on_target(target_url: str, database: str) -> None:
    if _RESTORE_DATABASE.fullmatch(database) is None:
        raise RestoreTargetRejected
    engine = create_async_engine(_maintenance_url(target_url), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:database AND pid<>pg_backend_pid()"),
                {"database": database},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
    finally:
        await engine.dispose()


async def _drop_created_database(current_url: str, target_url: str) -> None:
    database = database_name(target_url)
    if _RESTORE_DATABASE.fullmatch(database) is None or _same_database(current_url, target_url):
        raise RestoreTargetRejected
    await _drop_database_on_target(target_url, database)


async def _validate_tombstone_prefix(
    session,
    snapshot: TombstoneSnapshot,
    archive_high_watermark: int,
) -> tuple[int, RecoveryJournalStateRow]:
    entries = snapshot.entries
    rows = (await session.execute(select(DeletionTombstoneRow).order_by(DeletionTombstoneRow.journal_sequence))).scalars().all()
    if len(rows) > len(entries):
        raise TombstoneJournalUnavailable
    for expected, row in enumerate(rows, start=1):
        if row.journal_sequence != expected:
            raise TombstoneJournalUnavailable
        journal_entry = entries[expected - 1]
        if row.ciphertext_digest != journal_entry.ciphertext_digest or row.record_digest != journal_entry.record_digest or row.resource_kind != journal_entry.record.resource_kind or row.purge_status != "purged":
            raise TombstoneJournalUnavailable
    if len(rows) < archive_high_watermark:
        raise TombstoneJournalUnavailable
    state = await session.scalar(select(RecoveryJournalStateRow).where(RecoveryJournalStateRow.id == 1).with_for_update())
    if state is None:
        if rows or archive_high_watermark:
            raise TombstoneJournalUnavailable
        state = RecoveryJournalStateRow(
            id=1,
            source_installation_id=snapshot.source_installation_id,
            journal_id=uuid.UUID(snapshot.journal_id),
            high_watermark=0,
            head_digest="0" * 64,
        )
        session.add(state)
        await session.flush()
    expected_head = entries[len(rows) - 1].record_digest if rows else "0" * 64
    if state.source_installation_id != snapshot.source_installation_id or str(state.journal_id) != snapshot.journal_id or state.high_watermark != len(rows) or state.head_digest != expected_head:
        raise TombstoneJournalUnavailable
    return len(rows), state


async def replay_tombstones(
    target_database_url: str,
    journal: TombstoneJournal,
    *,
    archive_high_watermark: int,
    keyring: AuditHmacKeyring | None = None,
    snapshot: TombstoneSnapshot | None = None,
) -> int:
    active_snapshot = snapshot or await asyncio.to_thread(
        journal.snapshot,
        require_existing=True,
    )
    if archive_high_watermark > active_snapshot.high_watermark:
        raise TombstoneJournalUnavailable
    entries = active_snapshot.entries[archive_high_watermark:]
    active_keyring = keyring or AuditHmacKeyring.from_environment()
    engine = create_async_engine(DatabaseConfig(url=target_database_url).sqlalchemy_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    replayed = 0
    try:
        async with factory() as session, session.begin():
            existing_prefix, state = await _validate_tombstone_prefix(
                session,
                active_snapshot,
                archive_high_watermark,
            )
            for entry in entries:
                if entry.sequence <= existing_prefix:
                    continue
                if await apply_replay_entry(session, entry, keyring=active_keyring):
                    replayed += 1
                state.high_watermark = entry.sequence
                state.head_digest = entry.record_digest
                state.updated_at = datetime.now(UTC)
        return replayed
    finally:
        await engine.dispose()


async def _require_exact_m7_database(database_url: str) -> None:
    engine = create_async_engine(DatabaseConfig(url=database_url).sqlalchemy_url)
    try:
        async with engine.connect() as connection:
            if await classify_database(connection) != "m7":
                raise RecoveryProbeFailed
    except RecoveryProbeFailed:
        raise
    except M7RecreateRequired:
        raise RecoveryProbeFailed from None
    except Exception:
        raise RecoveryProbeFailed from None
    finally:
        await engine.dispose()


async def _run_recovery_probes(
    target_database_url: str,
    archive: _AuthenticatedArchive,
    journal_snapshot: TombstoneSnapshot,
) -> None:
    if archive.archive_schema_version != ARCHIVE_SCHEMA_VERSION or archive.schema_revision != M7_FINAL_SCHEMA_REVISION or archive.schema_digest != M7_CANONICAL_SCHEMA_DIGEST:
        raise RecoveryProbeFailed
    await _require_exact_m7_database(target_database_url)
    replayed_through = journal_snapshot.high_watermark
    expected_head = journal_snapshot.entries[-1].record_digest if journal_snapshot.entries else "0" * 64
    engine = create_async_engine(DatabaseConfig(url=target_database_url).sqlalchemy_url)
    try:
        async with engine.connect() as connection:
            tables = frozenset(
                (
                    await connection.execute(
                        text(
                            """SELECT tablename FROM pg_tables
                                 WHERE schemaname=current_schema()"""
                        )
                    )
                ).scalars()
            )
            if not _REQUIRED_TABLES.issubset(tables):
                raise RecoveryProbeFailed
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
            if revision != archive.schema_revision:
                raise RecoveryProbeFailed
            table_count = await connection.scalar(
                text(
                    """SELECT count(*) FROM pg_class
                         WHERE relnamespace=current_schema()::regnamespace
                           AND relkind IN ('r','p')"""
                )
            )
            if int(table_count or 0) != archive.table_count:
                raise RecoveryProbeFailed
            constraints = frozenset((await connection.execute(text("SELECT conname FROM pg_constraint WHERE connamespace=current_schema()::regnamespace"))).scalars())
            if not _REQUIRED_CONSTRAINTS.issubset(constraints):
                raise RecoveryProbeFailed
            if await source_installation_id(connection) == archive.source_installation_id:
                raise RecoveryProbeFailed
            orphan_queries = (
                """SELECT count(*) FROM threads_meta child
                    LEFT JOIN project_memberships membership
                      ON membership.project_id=child.project_id AND membership.user_id=child.owner_user_id
                    WHERE membership.id IS NULL""",
                """SELECT count(*) FROM runs child
                    LEFT JOIN threads_meta parent
                      ON parent.project_id=child.project_id AND parent.owner_user_id=child.owner_user_id
                     AND parent.thread_id=child.thread_id
                    WHERE parent.thread_id IS NULL""",
                """SELECT count(*) FROM files child
                    LEFT JOIN threads_meta parent
                      ON parent.project_id=child.project_id AND parent.owner_user_id=child.owner_user_id
                     AND parent.thread_id=child.thread_id
                    WHERE parent.thread_id IS NULL""",
                """SELECT count(*) FROM run_events child
                    LEFT JOIN runs parent
                      ON parent.project_id=child.project_id AND parent.owner_user_id=child.owner_user_id
                     AND parent.thread_id=child.thread_id AND parent.run_id=child.run_id
                    WHERE parent.run_id IS NULL""",
                """SELECT count(*) FROM jobs child
                    LEFT JOIN projects project ON project.id=child.project_id
                    WHERE project.id IS NULL""",
            )
            for query in orphan_queries:
                if int(await connection.scalar(text(query)) or 0) != 0:
                    raise RecoveryProbeFailed
            row = (
                await connection.execute(
                    text(
                        """SELECT count(*)::bigint AS count,
                                  COALESCE(min(journal_sequence),0)::bigint AS minimum,
                                  COALESCE(max(journal_sequence),0)::bigint AS maximum
                             FROM deletion_tombstones"""
                    )
                )
            ).one()
            if int(row.count) != replayed_through or (replayed_through and (int(row.minimum) != 1 or int(row.maximum) != replayed_through)):
                raise RecoveryProbeFailed
            state = (
                await connection.execute(
                    text(
                        """SELECT source_installation_id,journal_id,
                                  high_watermark,head_digest
                           FROM recovery_journal_state WHERE id=1"""
                    )
                )
            ).one_or_none()
            if state is None or state.source_installation_id != archive.source_installation_id or str(state.journal_id) != journal_snapshot.journal_id or int(state.high_watermark) != replayed_through or state.head_digest != expected_head:
                raise RecoveryProbeFailed
    except RecoveryProbeFailed:
        raise
    except Exception:
        raise RecoveryProbeFailed from None
    finally:
        await engine.dispose()


async def _record_source_restore_started(
    database_url: str,
    *,
    restore_id: uuid.UUID,
    archive: _AuthenticatedArchive,
    keyring: AuditHmacKeyring,
) -> None:
    engine = create_async_engine(DatabaseConfig(url=database_url).sqlalchemy_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        service = AuditService(factory, keyring)
        sink = TrustedOperationAuditSink(service, process_context=_bind_recovery_audit_process(service))
        async with factory() as session, session.begin():
            await sink.restore_started(
                session,
                restore_id=restore_id,
                table_count=archive.table_count,
                tombstones_replayed=0,
                request_id=f"restore-{restore_id}",
            )
    finally:
        await engine.dispose()


async def _write_proof_and_completion(
    target_url: str,
    *,
    restore_id: uuid.UUID,
    archive: _AuthenticatedArchive,
    replayed: int,
    replayed_through: int,
    journal_snapshot: TombstoneSnapshot,
    keyring: AuditHmacKeyring,
) -> None:
    engine = create_async_engine(DatabaseConfig(url=target_url).sqlalchemy_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        service = AuditService(factory, keyring)
        sink = TrustedOperationAuditSink(service, process_context=_bind_recovery_audit_process(service))
        target_ref = keyring.audit_target_ref(
            "restore",
            uuid.uuid5(_TARGET_REF_NAMESPACE, database_name(target_url)),
        )
        async with factory() as session, session.begin():
            session.add(
                RestoreProofRow(
                    id=restore_id,
                    archive_id=uuid.UUID(archive.archive_id),
                    archive_digest=archive.archive_digest,
                    archive_schema_version=archive.archive_schema_version,
                    schema_digest=archive.schema_digest,
                    target_database_ref_key_id=target_ref.key_id,
                    target_database_ref_hmac=target_ref.hmac_hex,
                    schema_revision=archive.schema_revision,
                    archive_tombstone_sequence=archive.tombstone_journal_sequence,
                    replayed_through_sequence=replayed_through,
                    journal_id=uuid.UUID(journal_snapshot.journal_id),
                    final_journal_head_digest=(journal_snapshot.entries[-1].record_digest if journal_snapshot.entries else "0" * 64),
                    probes_complete=True,
                )
            )
            await session.flush()
            await sink.restore_completed(
                session,
                restore_id=restore_id,
                table_count=archive.table_count,
                tombstones_replayed=replayed,
                request_id=f"restore-{restore_id}",
            )
    finally:
        await engine.dispose()


class Restorer:
    def __init__(self, config: RestoreConfig) -> None:
        self.config = config
        self._handoff_token = object()
        self._verified_result: RestoreResult | None = None

    def owns_verified_target(self, result: RestoreResult) -> bool:
        return self._verified_result is result and result._handoff_token is self._handoff_token

    async def restore(self) -> RestoreResult:
        self._verified_result = None
        workspace, workspace_creation_cancelled = await _settle_blocking_result(
            _create_owned_workspace,
            prefix="deerflow-restore-work-",
        )
        workspace_removed = False
        authenticated: _AuthenticatedArchive | None = None
        created = False
        restore_id = uuid.uuid4()

        async def cleanup_invocation_resources() -> None:
            nonlocal created, workspace_removed
            cleanup_error: BaseException | None = None
            cleanup_cancelled = False
            if created:
                try:
                    cancelled = await _settle_async_cleanup(
                        _drop_created_database(
                            self.config.current_database_url,
                            self.config.target_database_url,
                        )
                    )
                    created = False
                    cleanup_cancelled = cleanup_cancelled or cancelled
                except BaseException as exc:
                    cleanup_error = exc
            if not workspace_removed:
                try:
                    cancelled = await _settle_blocking_cleanup(
                        _cleanup_owned_workspace,
                        workspace.path,
                        workspace.identity,
                        dict(workspace.files),
                    )
                    workspace_removed = True
                    cleanup_cancelled = cleanup_cancelled or cancelled
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            if cleanup_error is not None:
                if isinstance(cleanup_error, asyncio.CancelledError):
                    raise cleanup_error
                raise RestoreCommandFailed from None
            if cleanup_cancelled:
                raise asyncio.CancelledError

        try:
            if workspace_creation_cancelled:
                raise asyncio.CancelledError
            dump, dump_creation_cancelled = await _settle_blocking_result(
                _create_owned_file,
                workspace.path,
                prefix="deerflow-restore-",
                suffix=".dump",
            )
            workspace.register(dump)
            if dump_creation_cancelled:
                raise asyncio.CancelledError
            authenticated, authentication_cancelled = await _settle_blocking_result(
                _authenticate_archive,
                self.config.archive,
                self.config.backup_key,
                dump,
            )
            if authentication_cancelled:
                raise asyncio.CancelledError
            require_supported_archive(authenticated)
            async with _source_recovery_authority(
                self.config.current_database_url,
                journal=self.config.journal,
                expected_source_installation_id=authenticated.source_installation_id,
                archive_tombstone_sequence=authenticated.tombstone_journal_sequence,
            ) as journal_snapshot:
                try:
                    await _require_exact_m7_database(
                        self.config.current_database_url,
                    )
                    target_database = _validate_target(self.config)
                    if await _database_exists(
                        self.config.target_database_url,
                        target_database,
                    ):
                        raise RestoreTargetRejected
                    await _record_source_restore_started(
                        self.config.current_database_url,
                        restore_id=restore_id,
                        archive=authenticated,
                        keyring=self.config.keyring,
                    )
                    await _create_empty_database(
                        self.config.target_database_url,
                        target_database,
                    )
                    created = True
                    await _run_pg_restore(
                        self.config.target_database_url,
                        authenticated.dump_path,
                        workspace,
                    )
                    cleanup_cancelled = await _settle_blocking_cleanup(
                        _cleanup_owned_workspace,
                        workspace.path,
                        workspace.identity,
                        dict(workspace.files),
                    )
                    workspace_removed = True
                    if cleanup_cancelled:
                        raise asyncio.CancelledError
                    replayed = await replay_tombstones(
                        self.config.target_database_url,
                        self.config.journal,
                        archive_high_watermark=authenticated.tombstone_journal_sequence,
                        keyring=self.config.keyring,
                        snapshot=journal_snapshot,
                    )
                    replayed_through = journal_snapshot.high_watermark
                    await _run_recovery_probes(
                        self.config.target_database_url,
                        authenticated,
                        journal_snapshot,
                    )
                    await _write_proof_and_completion(
                        self.config.target_database_url,
                        restore_id=restore_id,
                        archive=authenticated,
                        replayed=replayed,
                        replayed_through=replayed_through,
                        journal_snapshot=journal_snapshot,
                        keyring=self.config.keyring,
                    )
                except BaseException:
                    await cleanup_invocation_resources()
                    raise
            result = RestoreResult(
                proof_id=restore_id,
                archive_id=authenticated.archive_id,
                archive_schema_version=authenticated.archive_schema_version,
                schema_revision=authenticated.schema_revision,
                schema_digest=authenticated.schema_digest,
                table_count=authenticated.table_count,
                tombstones_replayed=replayed,
                replayed_through_sequence=replayed_through,
                probes_complete=True,
                status="verified",
                checksum=authenticated.archive_digest,
                _handoff_token=self._handoff_token,
            )
            self._verified_result = result
            created = False
            return result
        except (
            RestoreTargetRejected,
            RestoreAuthenticationFailed,
            UnsupportedArchiveSchema,
            TombstoneJournalUnavailable,
            RecoveryProbeFailed,
        ):
            raise
        except SourceIdentityMismatch:
            raise RestoreAuthenticationFailed from None
        except asyncio.CancelledError:
            raise
        except (
            RecoveryAuthorityReleaseFailed,
            RestoreCommandFailed,
            SensitiveCleanupFailed,
        ):
            raise RestoreCommandFailed from None
        except Exception:
            raise RestoreCommandFailed from None
        finally:
            if created or not workspace_removed:
                await cleanup_invocation_resources()


async def _record_drill_completion(
    database_url: str,
    *,
    result: RestoreResult,
    keyring: AuditHmacKeyring,
) -> None:
    engine = create_async_engine(DatabaseConfig(url=database_url).sqlalchemy_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        service = AuditService(factory, keyring)
        sink = TrustedOperationAuditSink(service, process_context=_bind_recovery_audit_process(service))
        async with factory() as session, session.begin():
            await sink.recovery_drill_completed(
                session,
                restore_id=result.proof_id,
                table_count=result.table_count,
                tombstones_replayed=result.tombstones_replayed,
                request_id=f"drill-{result.proof_id}",
            )
    finally:
        await engine.dispose()


async def drill_restore(
    *,
    current_database_url: str,
    archive: Path,
    journal: TombstoneJournal,
    backup_key: bytes,
    keyring: AuditHmacKeyring,
) -> RestoreResult:
    target_url = make_url(current_database_url).set(database=f"deerflow_restore_{os.getpid()}_{uuid.uuid4().hex}").render_as_string(hide_password=False)
    restorer = Restorer(
        RestoreConfig(
            archive=archive,
            target_database_url=target_url,
            current_database_url=current_database_url,
            journal=journal,
            backup_key=backup_key,
            keyring=keyring,
        )
    )
    owned = False
    try:
        result = await restorer.restore()
        if not restorer.owns_verified_target(result):
            raise RestoreCommandFailed
        owned = True
        await _record_drill_completion(current_database_url, result=result, keyring=keyring)
        return result
    finally:
        if owned:
            cancelled = await _settle_async_cleanup(_drop_created_database(current_database_url, target_url))
            if cancelled:
                raise asyncio.CancelledError


__all__ = [
    "RecoveryProbeFailed",
    "RestoreAuthenticationFailed",
    "RestoreCommandFailed",
    "RestoreConfig",
    "RestoreResult",
    "RestoreTargetRejected",
    "Restorer",
    "TombstoneRecord",
    "database_name",
    "drill_restore",
    "replay_tombstones",
]
