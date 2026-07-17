"""Fail-closed new-database restore, tombstone replay, and recovery drill."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit.service import AuditService, _bind_recovery_audit_process
from app.audit.sinks import TrustedOperationAuditSink
from app.recovery import BackupArchiveReader, BackupAuthenticationFailed
from app.recovery.authority import (
    RecoveryAuthorityReleaseFailed,
    SourceIdentityMismatch,
)
from app.recovery.authority import (
    source_recovery_authority as _source_recovery_authority,
)
from app.recovery.cleanup import (
    SensitiveCleanupFailed,
    _cleanup_owned_workspace,
    _remove_owned_file,
    _settle_async_cleanup,
    _settle_blocking_cleanup,
)
from app.recovery.identity import source_installation_id
from app.recovery.journal import (
    TombstoneJournal,
    TombstoneJournalUnavailable,
    TombstoneRecord,
    TombstoneSnapshot,
)
from app.recovery.purge import apply_replay_entry
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.recovery.model import (
    DeletionTombstoneRow,
    RecoveryJournalStateRow,
    RestoreProofRow,
)

_RESTORE_DATABASE = re.compile(r"deerflow_restore_[0-9]+_[0-9a-f]{32}\Z")
_SCHEMA_REVISION = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
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
_PROCESS_TERM_TIMEOUT_SECONDS = 5.0


class RestoreAuthenticationFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("RESTORE_AUTHENTICATION_FAILED")


class RestoreTargetRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("RESTORE_TARGET_REJECTED")


class RestoreCommandFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("RESTORE_COMMAND_FAILED")


class RecoveryProbeFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("RECOVERY_PROBE_FAILED")


@dataclass(frozen=True, slots=True)
class _AuthenticatedArchive:
    archive_id: str
    schema_revision: str
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
    schema_revision: str
    table_count: int
    tombstones_replayed: int
    replayed_through_sequence: int
    probes_complete: bool
    status: str
    checksum: str


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


def _parse_authenticated_manifest(raw: bytes) -> tuple[str, str, str, int, int, str]:
    try:
        envelope = json.loads(raw)
        body = envelope["manifest"]
        archive_id = str(uuid.UUID(str(body["archive_id"])))
        schema_revision = str(body["schema_revision"])
        source_id = str(body["source_installation_id"])
        sequence = body["tombstone_journal_sequence"]
        table_count = body["table_count"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise RestoreAuthenticationFailed from None
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"manifest", "signature"}
        or _SCHEMA_REVISION.fullmatch(schema_revision) is None
        or _HEX_DIGEST.fullmatch(source_id) is None
        or type(sequence) is not int
        or sequence < 0
        or type(table_count) is not int
        or table_count < 1
    ):
        raise RestoreAuthenticationFailed
    return archive_id, schema_revision, source_id, sequence, table_count, hashlib.sha256(raw).hexdigest()


def _authenticate_archive(archive: Path, key: bytes, workspace: Path) -> _AuthenticatedArchive:
    before = _read_manifest_bytes(archive)
    descriptor, dump_name = tempfile.mkstemp(prefix="deerflow-restore-", suffix=".dump", dir=workspace)
    dump_path = Path(dump_name)
    try:
        os.fchmod(descriptor, 0o600)
        dump_info = os.fstat(descriptor)
        dump_identity = (dump_info.st_dev, dump_info.st_ino)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            for chunk in BackupArchiveReader(key).verified_chunks(archive):
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        after = _read_manifest_bytes(archive)
        if before != after:
            raise RestoreAuthenticationFailed
        archive_id, revision, source_id, sequence, table_count, digest = _parse_authenticated_manifest(after)
        return _AuthenticatedArchive(
            archive_id,
            revision,
            source_id,
            sequence,
            table_count,
            digest,
            dump_path,
            dump_identity,
        )
    except BackupAuthenticationFailed:
        try:
            dump_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RestoreAuthenticationFailed from None
    except BaseException:
        try:
            dump_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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


def _pgpass_escape(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise RestoreCommandFailed
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _libpq_environment(
    database_url: str,
    workspace: Path,
) -> tuple[dict[str, str], Path | None, tuple[int, int] | None]:
    try:
        parsed = make_url(database_url)
        host = parsed.host or ""
        port = str(parsed.port or 5432)
        user = parsed.username or ""
        database = parsed.database or ""
        if not user or not database:
            raise ValueError
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PGHOST": host,
            "PGPORT": port,
            "PGUSER": user,
            "PGDATABASE": database,
        }
        for query_key, env_key in {
            "sslmode": "PGSSLMODE",
            "sslrootcert": "PGSSLROOTCERT",
            "sslcert": "PGSSLCERT",
            "sslkey": "PGSSLKEY",
        }.items():
            value = parsed.query.get(query_key)
            if value:
                environment[env_key] = str(value)
        passfile: Path | None = None
        passfile_identity: tuple[int, int] | None = None
        if parsed.password:
            descriptor, name = tempfile.mkstemp(prefix=".restore-pgpass-", dir=workspace)
            passfile = Path(name)
            os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
            passfile_identity = (info.st_dev, info.st_ino)
            line = ":".join(_pgpass_escape(value) for value in (host or "*", port, database, user, parsed.password))
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            environment["PGPASSFILE"] = str(passfile)
        return environment, passfile, passfile_identity
    except RestoreCommandFailed:
        raise
    except Exception:
        raise RestoreCommandFailed from None


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_TERM_TIMEOUT_SECONDS)
    except (TimeoutError, ProcessLookupError):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def _run_pg_restore(database_url: str, dump_path: Path, workspace: Path) -> None:
    database = database_name(database_url)
    environment, passfile, passfile_identity = await asyncio.to_thread(
        _libpq_environment,
        database_url,
        workspace,
    )
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            f"--dbname={database}",
            str(dump_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
        )
        if await process.wait() != 0:
            raise RestoreCommandFailed
    except asyncio.CancelledError:
        if process is not None:
            cleanup = asyncio.create_task(_terminate_process(process))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
        raise
    except RestoreCommandFailed:
        raise
    except Exception:
        if process is not None:
            await _terminate_process(process)
        raise RestoreCommandFailed from None
    finally:
        if passfile is not None:
            if passfile_identity is None:
                raise RestoreCommandFailed
            cancelled = await _settle_blocking_cleanup(
                _remove_owned_file,
                passfile,
                passfile_identity,
            )
            if cancelled:
                raise asyncio.CancelledError


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


async def _run_recovery_probes(
    target_database_url: str,
    archive: _AuthenticatedArchive,
    replayed_through: int,
) -> None:
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
                        """SELECT source_installation_id,high_watermark,head_digest
                           FROM recovery_journal_state WHERE id=1"""
                    )
                )
            ).one_or_none()
            if state is None or state.source_installation_id != archive.source_installation_id or int(state.high_watermark) != replayed_through:
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

    async def restore(self) -> RestoreResult:
        target_database = _validate_target(self.config)
        if await _database_exists(self.config.target_database_url, target_database):
            raise RestoreTargetRejected
        workspace = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="deerflow-restore-work-"))
        await asyncio.to_thread(os.chmod, workspace, 0o700)
        workspace_info = await asyncio.to_thread(os.stat, workspace, follow_symlinks=False)
        workspace_identity = (workspace_info.st_dev, workspace_info.st_ino)
        workspace_removed = False
        authenticated: _AuthenticatedArchive | None = None
        created = False
        restore_id = uuid.uuid4()
        try:
            authenticated = await asyncio.to_thread(
                _authenticate_archive,
                self.config.archive,
                self.config.backup_key,
                workspace,
            )
            async with _source_recovery_authority(
                self.config.current_database_url,
                journal=self.config.journal,
                expected_source_installation_id=authenticated.source_installation_id,
                archive_tombstone_sequence=authenticated.tombstone_journal_sequence,
            ) as journal_snapshot:
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
                    workspace,
                    workspace_identity,
                    {authenticated.dump_path.name: authenticated.dump_identity},
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
                    replayed_through,
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
            created = False
            return RestoreResult(
                proof_id=restore_id,
                archive_id=authenticated.archive_id,
                schema_revision=authenticated.schema_revision,
                table_count=authenticated.table_count,
                tombstones_replayed=replayed,
                replayed_through_sequence=replayed_through,
                probes_complete=True,
                status="verified",
                checksum=authenticated.archive_digest,
            )
        except (RestoreTargetRejected, RestoreAuthenticationFailed, TombstoneJournalUnavailable, RecoveryProbeFailed):
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
            cleanup_error: BaseException | None = None
            if created:
                try:
                    cleanup_cancelled = await _settle_async_cleanup(
                        _drop_created_database(
                            self.config.current_database_url,
                            self.config.target_database_url,
                        )
                    )
                    if cleanup_cancelled:
                        cleanup_error = asyncio.CancelledError()
                except BaseException as exc:
                    cleanup_error = exc
            if not workspace_removed:
                try:
                    cleanup_cancelled = await _settle_blocking_cleanup(
                        _cleanup_owned_workspace,
                        workspace,
                        workspace_identity,
                    )
                    if cleanup_cancelled and cleanup_error is None:
                        cleanup_error = asyncio.CancelledError()
                except BaseException as exc:
                    cleanup_error = exc
            if cleanup_error is not None:
                if isinstance(cleanup_error, SensitiveCleanupFailed):
                    raise RestoreCommandFailed from None
                raise cleanup_error


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
    result: RestoreResult | None = None
    try:
        result = await Restorer(
            RestoreConfig(
                archive=archive,
                target_database_url=target_url,
                current_database_url=current_database_url,
                journal=journal,
                backup_key=backup_key,
                keyring=keyring,
            )
        ).restore()
        await _record_drill_completion(current_database_url, result=result, keyring=keyring)
        return result
    finally:
        await _drop_created_database(current_database_url, target_url)


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
