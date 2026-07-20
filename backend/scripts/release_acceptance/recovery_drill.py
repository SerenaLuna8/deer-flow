from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

import asyncpg
from pydantic import Field
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit.service import AuditService, _bind_recovery_audit_process
from app.audit.sinks import TrustedOperationAuditSink
from app.recovery.archive import BackupConfig, create_backup
from app.recovery.journal import TombstoneJournal
from app.recovery.purge import RetentionCandidate, RetentionPurger
from app.recovery.restore import RestoreConfig, Restorer, RestoreResult
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.config.database_config import DatabaseConfig
from scripts.release_acceptance.host_stack import AsyncpgHostDatabaseManager, OwnedHostStack
from scripts.release_acceptance.live_probe import RecoveryBrowserProbe, RecoverySessionAuthority
from scripts.release_acceptance.models import RecoverySummary, StrictModel
from scripts.release_acceptance.ownership import OwnedDatabase, OwnershipLedger


class RecoveryOwnershipError(RuntimeError):
    """A restore result was not issued by the exact Restorer invocation."""


class ExpectedInventory(StrictModel):
    public_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    restored_count: int = Field(gt=0)


class ArchivePoint(StrictModel):
    archive_schema_version: Literal[7]
    schema_revision: Literal["0001_project_saas_baseline"]


class RestoreVerification(StrictModel):
    proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    restored_count: int = Field(gt=0)
    rpo_outcome: Literal["archive_point_confirmed"]


class RestorerProtocol(Protocol):
    async def restore(self) -> object: ...

    def owns_verified_target(self, result: object) -> bool: ...


RegisterVerifiedTarget = Callable[[object], Awaitable[object]]


async def run_restore_phase(
    restorer: RestorerProtocol,
    register_verified_target: RegisterVerifiedTarget,
) -> object:
    """Register a target only after the creating Restorer proves ownership."""

    result = await restorer.restore()
    if not restorer.owns_verified_target(result):
        raise RecoveryOwnershipError("RESTORE_TARGET_NOT_OWNED")
    return await register_verified_target(result)


class RecoverySwitchOperations(Protocol):
    async def inventory(self) -> ExpectedInventory: ...

    async def archive(self, expected: ExpectedInventory) -> ArchivePoint: ...

    async def journal_purge(self) -> int: ...

    async def post_backup_row(self) -> None: ...

    async def source_stop(self) -> None: ...

    async def restore(self) -> object: ...

    async def restore_probe(
        self,
        restored: object,
        expected: ExpectedInventory,
    ) -> RestoreVerification: ...

    async def restore_start(self, restored: object) -> None: ...

    async def browser_probe(self) -> None: ...

    async def restore_stop(self) -> None: ...

    async def source_start(self) -> None: ...

    async def back_switch_probe(self) -> None: ...

    def monotonic_ns(self) -> int: ...


async def _settle_cleanup(operation: Callable[[], Awaitable[None]]) -> None:
    task = asyncio.create_task(operation())
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    task.result()


class RecoverySwitchDrill:
    """Sequence one archive-point restore switch and mandatory source back-switch."""

    def __init__(self, operations: RecoverySwitchOperations) -> None:
        self._operations = operations

    async def run(self) -> RecoverySummary:
        source_running = True
        restore_running = False
        primary_error: BaseException | None = None
        try:
            expected = await self._operations.inventory()
            archive = await self._operations.archive(expected)
            tombstone_count = await self._operations.journal_purge()
            if tombstone_count < 1:
                raise RecoveryOwnershipError("RECOVERY_TOMBSTONE_REQUIRED")
            await self._operations.post_backup_row()
            await self._operations.source_stop()
            source_running = False
            restore_started = self._operations.monotonic_ns()
            restored = await self._operations.restore()
            verification = await self._operations.restore_probe(
                restored,
                expected,
            )
            await self._operations.restore_start(restored)
            restore_running = True
            await self._operations.browser_probe()
            rto_ms = max(
                0,
                (self._operations.monotonic_ns() - restore_started) // 1_000_000,
            )
            await self._operations.restore_stop()
            restore_running = False
            await self._operations.source_start()
            source_running = True
            await self._operations.back_switch_probe()
            return RecoverySummary(
                archive_schema_version=archive.archive_schema_version,
                schema_revision=archive.schema_revision,
                tombstone_count=tombstone_count,
                proof_digest=verification.proof_digest,
                rto_ms=rto_ms,
                rpo_outcome=verification.rpo_outcome,
                restored_count=verification.restored_count,
            )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_error: BaseException | None = None
            if restore_running:
                try:
                    await _settle_cleanup(self._operations.restore_stop)
                except BaseException as exc:
                    cleanup_error = exc
            if not source_running:
                try:
                    await _settle_cleanup(self._operations.source_start)
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            if cleanup_error is not None:
                if primary_error is not None:
                    primary_error.add_note("recovery back-switch cleanup also failed")
                else:
                    raise cleanup_error


@dataclass(frozen=True, slots=True)
class OwnedRestoreTarget:
    result: RestoreResult
    owned_database: OwnedDatabase
    app_url: str


def _database_url(value: str, name: str) -> str:
    return make_url(value).set(database=name).render_as_string(hide_password=False)


def _inventory_digest(authority: RecoverySessionAuthority) -> str:
    payload = {
        "live": {
            "artifact_id": str(authority.live.artifact_id),
            "project_id": str(authority.live.project_id),
            "run_id": str(authority.live.run_id),
            "thread_id": str(authority.live.thread_id),
        },
        "purge": {
            "file_id": str(authority.purge_file_id),
            "project_id": str(authority.purge_project.project_id),
            "thread_id": str(authority.purge_thread_id),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"deerflow-m8-inventory-v1\0" + encoded).hexdigest()


class PostgresRecoveryOperations:
    """Real archive, journal-first purge, restore switch, and source back-switch."""

    def __init__(
        self,
        *,
        source_host: OwnedHostStack,
        database_manager: AsyncpgHostDatabaseManager,
        ledger: OwnershipLedger,
        recovery_browser: RecoveryBrowserProbe,
        runtime_root: Path,
        source_app_url: str,
        postgres_admin_url: str,
        app_role: str,
        authority: RecoverySessionAuthority,
        backup_key: bytes,
        journal_key: bytes,
        keyring: AuditHmacKeyring,
    ) -> None:
        if backup_key == journal_key:
            raise ValueError("RECOVERY_KEYS_MUST_BE_DISTINCT")
        self._host = source_host
        self._database_manager = database_manager
        self._ledger = ledger
        self._browser = recovery_browser
        self._runtime_root = runtime_root.resolve()
        self._source_app_url = source_app_url
        self._app_role = app_role
        self._authority = authority
        self._backup_key = backup_key
        self._keyring = keyring
        source_name = make_url(source_app_url).database
        if not source_name or source_name != source_host.owned_database.name:
            raise ValueError("RECOVERY_SOURCE_DATABASE_MISMATCH")
        self._source_owned = source_host.owned_database
        self._source_operator_url = _database_url(postgres_admin_url, source_name)
        self._target_name = f"deerflow_restore_{os.getpid()}_{uuid.uuid4().hex}"
        self._target_operator_url = _database_url(postgres_admin_url, self._target_name)
        self._target_app_url = _database_url(source_app_url, self._target_name)
        self._recovery_root = self._runtime_root / "recovery"
        self._archive_path = self._recovery_root / "archive.dfba"
        self._journal_directory = self._recovery_root / "journal"
        self._journal_path = self._journal_directory / "tombstones.jsonl"
        self._proof_directory = self._recovery_root / "proof"
        self._journal = TombstoneJournal(self._journal_path, journal_key)
        self._post_backup_file_id = uuid.uuid4()
        self._workspace_ready = False
        self._restored: OwnedRestoreTarget | None = None

    @staticmethod
    async def _connect(database_url: str):
        return await asyncpg.connect(
            database_url.replace("postgresql+asyncpg://", "postgresql://", 1),
            timeout=10,
        )

    async def _prepare_workspace(self) -> None:
        if self._workspace_ready:
            return
        for path in (
            self._recovery_root,
            self._journal_directory,
            self._proof_directory,
        ):
            await asyncio.to_thread(path.mkdir, mode=0o700)
            self._ledger.register_path(path, disposition="temporary")
        self._workspace_ready = True

    async def _live_inventory(self, database_url: str) -> tuple[int, int, int, int]:
        authority = self._authority
        connection = await self._connect(database_url)
        try:
            row = await connection.fetchrow(
                """SELECT
                     (SELECT count(*) FROM threads_meta
                       WHERE project_id=$1 AND owner_user_id=$2 AND thread_id=$3) AS thread_count,
                     (SELECT count(*) FROM runs
                       WHERE project_id=$1 AND owner_user_id=$2 AND thread_id=$3 AND run_id=$4 AND status='success') AS run_count,
                     (SELECT count(*) FROM artifacts
                       WHERE project_id=$1 AND owner_user_id=$2 AND thread_id=$3 AND run_id=$4 AND id=$5 AND deleted_at IS NULL) AS artifact_count,
                     (SELECT count(*) FROM files AS file
                       JOIN artifacts AS artifact
                         ON artifact.project_id=file.project_id
                        AND artifact.owner_user_id=file.owner_user_id
                        AND artifact.thread_id=file.thread_id
                        AND artifact.file_id=file.id
                      WHERE artifact.project_id=$1 AND artifact.owner_user_id=$2
                        AND artifact.thread_id=$3 AND artifact.run_id=$4 AND artifact.id=$5
                        AND artifact.deleted_at IS NULL AND file.status='ready' AND file.deleted_at IS NULL) AS file_count""",
                authority.live.project_id,
                str(authority.admin.user_id),
                str(authority.live.thread_id),
                str(authority.live.run_id),
                authority.live.artifact_id,
            )
            if row is None:
                raise RecoveryOwnershipError("RECOVERY_INVENTORY_MISSING")
            values = tuple(
                int(row[name])
                for name in (
                    "thread_count",
                    "run_count",
                    "artifact_count",
                    "file_count",
                )
            )
            return values  # type: ignore[return-value]
        finally:
            await connection.close()

    async def _file_count(self, database_url: str, file_id: uuid.UUID) -> int:
        connection = await self._connect(database_url)
        try:
            return int(await connection.fetchval("SELECT count(*) FROM files WHERE id=$1", file_id))
        finally:
            await connection.close()

    async def inventory(self) -> ExpectedInventory:
        counts = await self._live_inventory(self._source_app_url)
        purge_count = await self._file_count(self._source_app_url, self._authority.purge_file_id)
        if counts != (1, 1, 1, 1) or purge_count != 1:
            raise RecoveryOwnershipError("RECOVERY_INVENTORY_MISSING")
        return ExpectedInventory(
            public_digest=_inventory_digest(self._authority),
            restored_count=sum(counts),
        )

    async def archive(self, expected: ExpectedInventory) -> ArchivePoint:
        if expected.public_digest != _inventory_digest(self._authority):
            raise RecoveryOwnershipError("RECOVERY_INVENTORY_SUBSTITUTED")
        await self._prepare_workspace()
        manifest = await create_backup(
            BackupConfig(
                database_url=self._source_app_url,
                output=self._archive_path,
                key=self._backup_key,
                archive_id=str(uuid.uuid4()),
            )
        )
        self._ledger.register_path(self._archive_path, disposition="temporary")
        return ArchivePoint(
            archive_schema_version=manifest.archive_schema_version,
            schema_revision=manifest.schema_revision,
        )

    async def journal_purge(self) -> int:
        deleted_at = datetime.now(UTC) - timedelta(days=31)
        authority = self._authority
        connection = await self._connect(self._source_app_url)
        try:
            result = await connection.execute(
                """UPDATE files
                      SET status='deleted', deleted_at=$1, updated_at=$1
                    WHERE id=$2 AND project_id=$3 AND owner_user_id=$4
                      AND thread_id=$5 AND status='ready'""",
                deleted_at,
                authority.purge_file_id,
                authority.purge_project.project_id,
                str(authority.admin.user_id),
                str(authority.purge_thread_id),
            )
            if result != "UPDATE 1":
                raise RecoveryOwnershipError("RECOVERY_PURGE_TARGET_MISSING")
        finally:
            await connection.close()
        engine = create_async_engine(DatabaseConfig(url=self._source_app_url).sqlalchemy_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            service = AuditService(sessions, self._keyring)
            purger = RetentionPurger(
                sessions,
                journal=self._journal,
                keyring=self._keyring,
                audit=TrustedOperationAuditSink(
                    service,
                    process_context=_bind_recovery_audit_process(service),
                ),
            )
            receipt = await purger.purge(
                RetentionCandidate.file(
                    project_id=authority.purge_project.project_id,
                    owner_user_id=str(authority.admin.user_id),
                    file_id=authority.purge_file_id,
                    deleted_at=deleted_at,
                    idempotency_key=f"m8-file:{authority.purge_file_id}",
                    request_id="m8-recovery-purge",
                ),
                now=datetime.now(UTC),
            )
        finally:
            await engine.dispose()
        self._ledger.register_path(self._journal_path, disposition="temporary")
        return receipt.sequence

    async def post_backup_row(self) -> None:
        authority = self._authority
        connection = await self._connect(self._source_app_url)
        try:
            result = await connection.execute(
                """INSERT INTO files
                   (id,project_id,owner_user_id,thread_id,kind,logical_path,media_type,size,sha256,status,version,created_at,updated_at)
                   VALUES ($1,$2,$3,$4,'output',$5,'text/plain',0,$6,'ready',1,now(),now())""",
                self._post_backup_file_id,
                authority.live_project.project_id,
                str(authority.admin.user_id),
                str(authority.live.thread_id),
                f"m8-post-backup-{self._post_backup_file_id.hex}.txt",
                hashlib.sha256(b"").hexdigest(),
            )
            if result != "INSERT 0 1":
                raise RecoveryOwnershipError("RECOVERY_POST_BACKUP_INSERT_FAILED")
        finally:
            await connection.close()

    async def source_stop(self) -> None:
        await self._host.stop()

    async def _register_restored(self, result: object) -> OwnedRestoreTarget:
        if not isinstance(result, RestoreResult):
            raise RecoveryOwnershipError("RESTORE_PROOF_SUBSTITUTED")
        marker = hashlib.sha256(f"deerflow-m8-restore\0{self._ledger.acceptance_run_id}\0{self._target_name}\0{self._app_role}".encode()).hexdigest()
        identity = await self._database_manager.claim_verified_restore(
            name=self._target_name,
            marker_digest=marker,
        )
        owned = self._ledger.register_database(
            name=self._target_name,
            owner=identity.owner,
            marker_digest=marker,
        )
        await self._database_manager.grant_runtime_access(
            name=self._target_name,
            app_role=self._app_role,
        )
        target = OwnedRestoreTarget(
            result=result,
            owned_database=owned,
            app_url=self._target_app_url,
        )
        self._restored = target
        return target

    async def restore(self) -> OwnedRestoreTarget:
        restorer = Restorer(
            RestoreConfig(
                archive=self._archive_path,
                target_database_url=self._target_operator_url,
                current_database_url=self._source_operator_url,
                journal=self._journal,
                backup_key=self._backup_key,
                keyring=self._keyring,
            )
        )
        restored = await run_restore_phase(restorer, self._register_restored)
        if not isinstance(restored, OwnedRestoreTarget):
            raise RecoveryOwnershipError("RESTORE_PROOF_SUBSTITUTED")
        return restored

    async def restore_probe(
        self,
        restored: object,
        expected: ExpectedInventory,
    ) -> RestoreVerification:
        if restored is not self._restored or not isinstance(restored, OwnedRestoreTarget):
            raise RecoveryOwnershipError("RESTORE_PROOF_SUBSTITUTED")
        result = restored.result
        counts = await self._live_inventory(restored.app_url)
        purge_count = await self._file_count(restored.app_url, self._authority.purge_file_id)
        post_backup_count = await self._file_count(restored.app_url, self._post_backup_file_id)
        if (
            counts != (1, 1, 1, 1)
            or sum(counts) != expected.restored_count
            or expected.public_digest != _inventory_digest(self._authority)
            or purge_count != 0
            or post_backup_count != 0
            or result.archive_schema_version != 7
            or result.schema_revision != "0001_project_saas_baseline"
            or result.tombstones_replayed < 1
            or not result.probes_complete
            or result.status != "verified"
        ):
            raise RecoveryOwnershipError("RECOVERY_RPO_PROOF_FAILED")
        proof = {
            "archive_schema_version": result.archive_schema_version,
            "checksum": result.checksum,
            "proof_id": str(result.proof_id),
            "replayed_through_sequence": result.replayed_through_sequence,
            "schema_revision": result.schema_revision,
            "tombstones_replayed": result.tombstones_replayed,
        }
        proof_digest = hashlib.sha256(b"deerflow-m8-restore-proof-v1\0" + json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return RestoreVerification(
            proof_digest=proof_digest,
            restored_count=sum(counts),
            rpo_outcome="archive_point_confirmed",
        )

    async def restore_start(self, restored: object) -> None:
        if restored is not self._restored or not isinstance(restored, OwnedRestoreTarget):
            raise RecoveryOwnershipError("RESTORE_PROOF_SUBSTITUTED")
        await self._host.start_existing(restored.app_url, restored.owned_database)

    async def browser_probe(self) -> None:
        await self._browser.run("restore")

    async def restore_stop(self) -> None:
        await self._host.stop()

    async def source_start(self) -> None:
        await self._host.start_existing(self._source_app_url, self._source_owned)

    async def back_switch_probe(self) -> None:
        if await self._file_count(self._source_app_url, self._post_backup_file_id) != 1:
            raise RecoveryOwnershipError("RECOVERY_SOURCE_BACK_SWITCH_FAILED")
        await self._browser.run("source")

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


__all__ = [
    "ArchivePoint",
    "ExpectedInventory",
    "OwnedRestoreTarget",
    "PostgresRecoveryOperations",
    "RecoveryOwnershipError",
    "RecoverySwitchDrill",
    "RecoverySwitchOperations",
    "RestoreVerification",
    "run_restore_phase",
]
