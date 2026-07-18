#!/usr/bin/env python3
"""Run the explicit, resumable M6 reliability cutover."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.audit.service import AuditService, _bind_operator_audit_process
from app.audit.sinks import TrustedOperationAuditSink
from app.quotas.models import _issue_quota_reconciliation_authority
from app.quotas.reconciliation import QuotaReconciler
from app.quotas.service import QuotaService
from app.recovery import (
    BackupArchiveReader,
    BackupAuthenticationFailed,
    BackupKeyInvalid,
    BackupKeyMissing,
    load_backup_key,
)
from app.recovery.identity import source_installation_id
from app.recovery.pre_cutover_backup import (
    PreCutoverBackupCommitError,
    PublishedPreCutoverBackupCommit,
    publish_external_proof,
    verify_pre_cutover_backup_commit,
)
from app.reliability.jobs import (
    automation_run_idempotency_key,
    private_run_idempotency_key,
)
from app.reliability.owner_refs import AuditHmacKeyring, AuditHmacKeyringInvalid
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.bootstrap import _get_alembic_config


class ReliabilityMigrationError(RuntimeError):
    """Credential- and content-safe migration failure."""


@dataclass(frozen=True, slots=True)
class ReliabilityMigrationReport:
    mode: str
    revision: str
    cutover_complete: bool
    source_count: int
    source_key_hash: str
    noop: bool = False


@dataclass(frozen=True, slots=True)
class _ValidatedBackupProof:
    digest: str
    archive_id: uuid.UUID
    table_count: int
    tombstone_journal_sequence: int


_PRE_EXPAND_REVISION = "0013_project_automation_finalize"
_EXPAND_REVISION = "0014_project_reliability_expand"
_FINAL_REVISION = "0015_project_reliability_finalize"
_ALLOWED_REVISIONS = frozenset({_PRE_EXPAND_REVISION, _EXPAND_REVISION, _FINAL_REVISION})
_MIGRATION_LOCK_KEY = 0x0DEE_12F1_0A55_0018
_PROOF_FORMAT = "deerflow.m6.backup-proof.v1"
_PROOF_DOMAIN = b"deerflow.m6.backup-proof.v1\x00"
_PROOF_KEY_INFO = b"deerflow-recovery-archive-v1:m6-backup-attestation-v1:"
_MIGRATION_NAMESPACE = uuid.UUID("2cc26b31-006b-41c2-a931-56b31cf5e905")
_LEDGER_DOMAINS = ("jobs", "quotas", "audit", "stream", "recovery")
_SOURCE_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "projects",
        "SELECT id,status,is_suspended FROM projects ORDER BY id",
    ),
    (
        "memberships",
        """SELECT id,project_id,user_id,status,version FROM project_memberships
           ORDER BY project_id,user_id,id""",
    ),
    (
        "files",
        """SELECT id,project_id,owner_user_id,size,status FROM files
           ORDER BY project_id,owner_user_id,id""",
    ),
    (
        "runs",
        """SELECT project_id,owner_user_id,run_id,thread_id,status FROM runs
           ORDER BY project_id,owner_user_id,run_id""",
    ),
    (
        "occurrences",
        """SELECT project_id,owner_user_id,id,run_id,status
           FROM scheduled_task_runs ORDER BY project_id,owner_user_id,id""",
    ),
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")


async def _table_exists(connection: AsyncConnection, table: str) -> bool:
    return (
        await connection.scalar(
            text("SELECT to_regclass(:table) IS NOT NULL"),
            {"table": table},
        )
        is True
    )


async def _current_revision(connection: AsyncConnection) -> str:
    if not await _table_exists(connection, "alembic_version"):
        raise ReliabilityMigrationError("versioned PostgreSQL database is required")
    revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    if not isinstance(revision, str) or revision not in _ALLOWED_REVISIONS:
        raise ReliabilityMigrationError("M6 reliability migration revision is unsupported")
    return revision


async def _source_payload(connection: AsyncConnection) -> dict[str, object]:
    payload: dict[str, object] = {}
    for name, query in _SOURCE_QUERIES:
        payload[name] = [dict(row) for row in (await connection.execute(text(query))).mappings()]
    return payload


async def source_fingerprint(database_url: str) -> str:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return hashlib.sha256(_canonical_json(await _source_payload(connection))).hexdigest()
    finally:
        await engine.dispose()


async def read_source_installation_id(database_url: str) -> str:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await source_installation_id(connection)
    finally:
        await engine.dispose()


def _proof_key(key: bytes, archive_id: object) -> bytes:
    try:
        parsed = uuid.UUID(str(archive_id))
    except (AttributeError, TypeError, ValueError):
        raise ReliabilityMigrationError("authenticated backup proof is invalid") from None
    if str(parsed) != str(archive_id):
        raise ReliabilityMigrationError("authenticated backup proof is invalid")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_PROOF_KEY_INFO + parsed.bytes,
    ).derive(key)


async def _settle_thread_task(task: asyncio.Task[object]) -> tuple[object, bool]:
    cancelled = False
    current = asyncio.current_task()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if current is not None:
                current.uncancel()
    try:
        result = task.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    return result, cancelled


async def _publish_backup_proof(path: Path, value: dict[str, object]) -> None:
    task = asyncio.create_task(asyncio.to_thread(publish_external_proof, Path(path), _canonical_json(value)))
    try:
        result, cancelled = await _settle_thread_task(task)
    except PreCutoverBackupCommitError:
        raise ReliabilityMigrationError("authenticated backup proof commit failed") from None
    if not isinstance(result, PublishedPreCutoverBackupCommit):
        raise ReliabilityMigrationError("authenticated backup proof commit failed")
    if cancelled:
        cleanup = asyncio.create_task(asyncio.to_thread(result.remove))
        try:
            await _settle_thread_task(cleanup)
        except BaseException:
            raise ReliabilityMigrationError("authenticated backup proof cleanup failed") from None
        raise asyncio.CancelledError
    result.commit()


def _read_manifest_envelope(archive: Path) -> dict[str, Any]:
    try:
        raw = (archive / "manifest.json").read_bytes()
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {"manifest", "signature"}:
            raise ValueError
        manifest = envelope["manifest"]
        if not isinstance(manifest, dict):
            raise ValueError
        envelope["_digest"] = hashlib.sha256(raw).hexdigest()
        envelope["_raw"] = raw
        return envelope
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        raise ReliabilityMigrationError("authenticated backup proof is invalid") from None


def _verify_archive(archive: Path, key: bytes) -> dict[str, Any]:
    try:
        if archive.is_symlink() or not archive.is_dir():
            raise ReliabilityMigrationError("authenticated backup proof is invalid")
        total = 0
        for chunk in BackupArchiveReader(key).verified_chunks(archive):
            total += len(chunk)
        envelope = _read_manifest_envelope(archive)
        if total < 5:
            raise ReliabilityMigrationError("authenticated backup proof is invalid")
        return envelope
    except ReliabilityMigrationError:
        raise
    except BackupAuthenticationFailed:
        raise ReliabilityMigrationError("authenticated backup proof is invalid") from None


async def create_authenticated_backup_proof(
    database_url: str,
    *,
    archive: Path,
    backup_commit: Path,
    output: Path,
    source_digest: str,
    restore_verified: bool,
) -> Path:
    """Create an HMAC-authenticated operator attestation for a Task16 archive."""

    if not restore_verified or not isinstance(source_digest, str) or len(source_digest) != 64:
        raise ReliabilityMigrationError("verified backup restore proof is required")
    try:
        key = load_backup_key(database_url=database_url)
    except (BackupKeyInvalid, BackupKeyMissing):
        raise ReliabilityMigrationError("authenticated backup proof key is unavailable") from None
    archive = Path(archive)
    backup_commit = Path(backup_commit)
    if backup_commit.resolve() != (archive.parent / f"{archive.name}.commit.json").resolve():
        raise ReliabilityMigrationError("authenticated backup commit is invalid")
    envelope = await asyncio.to_thread(_verify_archive, archive, key)
    try:
        commit_digest = await asyncio.to_thread(
            verify_pre_cutover_backup_commit,
            proof=backup_commit,
            manifest=envelope["manifest"],
            manifest_envelope=envelope["_raw"],
            key=key,
        )
    except PreCutoverBackupCommitError:
        raise ReliabilityMigrationError("authenticated backup commit is invalid") from None
    manifest = envelope["manifest"]
    source_id = await read_source_installation_id(database_url)
    if manifest.get("source_installation_id") != source_id or manifest.get("schema_revision") != _PRE_EXPAND_REVISION:
        raise ReliabilityMigrationError("authenticated backup proof source is invalid")
    body: dict[str, object] = {
        "format": _PROOF_FORMAT,
        "archive_path": str(Path(archive).expanduser().resolve()),
        "archive_id": manifest.get("archive_id"),
        "archive_manifest_sha256": envelope["_digest"],
        "backup_commit_path": str(Path(backup_commit).expanduser().resolve()),
        "backup_commit_sha256": commit_digest,
        "schema_revision": manifest.get("schema_revision"),
        "source_installation_id": source_id,
        "source_fingerprint": source_digest,
        "restore_verified": True,
    }
    body["signature"] = hmac.new(
        _proof_key(key, body["archive_id"]),
        _PROOF_DOMAIN + _canonical_json(body),
        hashlib.sha256,
    ).hexdigest()
    await _publish_backup_proof(Path(output), body)
    return Path(output)


async def _validate_backup_proof(
    database_url: str,
    proof_path: Path,
    *,
    expected_source: str,
) -> _ValidatedBackupProof:
    try:
        if proof_path.is_symlink() or not proof_path.is_file():
            raise ValueError
        value = json.loads(proof_path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError
        expected_fields = {
            "format",
            "archive_path",
            "archive_id",
            "archive_manifest_sha256",
            "backup_commit_path",
            "backup_commit_sha256",
            "schema_revision",
            "source_installation_id",
            "source_fingerprint",
            "restore_verified",
            "signature",
        }
        if set(value) != expected_fields or value["format"] != _PROOF_FORMAT or value["restore_verified"] is not True:
            raise ValueError
        signature = value.pop("signature")
        if not isinstance(signature, str):
            raise ValueError
        key = load_backup_key(database_url=database_url)
        expected_signature = hmac.new(
            _proof_key(key, value.get("archive_id")),
            _PROOF_DOMAIN + _canonical_json(value),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError
        source_id = await read_source_installation_id(database_url)
        if value["source_installation_id"] != source_id or value["source_fingerprint"] != expected_source or value["schema_revision"] != _PRE_EXPAND_REVISION:
            raise ValueError
        archive = Path(str(value["archive_path"]))
        envelope = await asyncio.to_thread(_verify_archive, archive, key)
        manifest = envelope["manifest"]
        commit_path = Path(str(value["backup_commit_path"]))
        if commit_path.resolve() != (archive.parent / f"{archive.name}.commit.json").resolve():
            raise ValueError
        try:
            commit_digest = await asyncio.to_thread(
                verify_pre_cutover_backup_commit,
                proof=commit_path,
                manifest=manifest,
                manifest_envelope=envelope["_raw"],
                key=key,
            )
        except PreCutoverBackupCommitError:
            raise ValueError from None
        if (
            envelope["_digest"] != value["archive_manifest_sha256"]
            or manifest.get("archive_id") != value["archive_id"]
            or manifest.get("schema_revision") != value["schema_revision"]
            or manifest.get("source_installation_id") != source_id
            or commit_digest != value["backup_commit_sha256"]
        ):
            raise ValueError
        archive_id = uuid.UUID(str(manifest["archive_id"]))
        table_count = int(manifest["table_count"])
        tombstone_sequence = int(manifest["tombstone_journal_sequence"])
        if table_count < 1 or tombstone_sequence != 0:
            raise ValueError
        return _ValidatedBackupProof(
            digest=hashlib.sha256(proof_path.read_bytes()).hexdigest(),
            archive_id=archive_id,
            table_count=table_count,
            tombstone_journal_sequence=tombstone_sequence,
        )
    except (BackupKeyInvalid, BackupKeyMissing, OSError, json.JSONDecodeError, TypeError, ValueError):
        raise ReliabilityMigrationError("authenticated backup proof is invalid") from None


async def catalog_digest(database_url: str) -> str:
    """Hash schema/catalog and M6 mutable controls for dry-run zero-write tests."""

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            payload: dict[str, object] = {}
            payload["revision"] = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            payload["columns"] = [
                tuple(row)
                for row in await connection.execute(
                    text(
                        """SELECT table_name,column_name,ordinal_position,data_type,
                                  is_nullable,column_default
                           FROM information_schema.columns
                           WHERE table_schema=current_schema()
                           ORDER BY table_name,ordinal_position"""
                    )
                )
            ]
            payload["constraints"] = [
                tuple(row)
                for row in await connection.execute(
                    text(
                        """SELECT c.relname,con.conname,pg_get_constraintdef(con.oid)
                           FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
                           JOIN pg_namespace n ON n.oid=c.relnamespace
                           WHERE n.nspname=current_schema() ORDER BY c.relname,con.conname"""
                    )
                )
            ]
            payload["sequences"] = [
                tuple(row)
                for row in await connection.execute(
                    text(
                        """SELECT sequencename,last_value FROM pg_sequences
                           WHERE schemaname=current_schema() ORDER BY sequencename"""
                    )
                )
            ]
            for table in (
                "reliability_migration_runs",
                "reliability_migration_ledger",
                "reliability_cutover_state",
                "project_usage_counters",
                "project_usage_ledger",
            ):
                if await _table_exists(connection, table):
                    payload[table] = int(await connection.scalar(text(f'SELECT count(*) FROM "{table}"')) or 0)
            return hashlib.sha256(_canonical_json(payload)).hexdigest()
    finally:
        await engine.dispose()


async def _assert_project_cutovers(connection: AsyncConnection) -> None:
    row = (
        await connection.execute(
            text(
                """SELECT
                       (SELECT stage='cutover_complete' AND cutover_at IS NOT NULL
                          FROM private_work_cutover_state WHERE id=1) AS m4,
                       (SELECT stage='cutover_complete' AND final_schema_probe_complete
                               AND cutover_at IS NOT NULL
                          FROM automation_cutover_state WHERE id=1) AS m5"""
            )
        )
    ).one()
    if row.m4 is not True or row.m5 is not True:
        raise ReliabilityMigrationError("M4 and M5 cutovers must be complete")


async def _assert_no_running_execution(connection: AsyncConnection) -> None:
    running = await connection.scalar(
        text(
            """SELECT EXISTS (SELECT 1 FROM runs WHERE status='running')
               OR EXISTS (SELECT 1 FROM scheduled_task_runs
                          WHERE status IN ('launching','running'))"""
        )
    )
    if running:
        raise ReliabilityMigrationError("active execution must be drained before M6 migration")


async def _upgrade(engine: AsyncEngine, revision: str) -> None:
    await asyncio.to_thread(command.upgrade, _get_alembic_config(engine), revision)


async def _backfill_jobs(session: AsyncSession) -> None:
    rows = (
        (
            await session.execute(
                text(
                    """SELECT project_id,owner_user_id,run_id,status,job_id
                   FROM runs WHERE status='pending'
                   ORDER BY project_id,owner_user_id,run_id FOR UPDATE"""
                )
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        occurrence = (
            (
                await session.execute(
                    text(
                        """SELECT id,status,job_id FROM scheduled_task_runs
                       WHERE project_id=:project AND owner_user_id=:owner
                         AND run_id=:run AND status='queued'
                       ORDER BY id LIMIT 1 FOR UPDATE"""
                    ),
                    {"project": row["project_id"], "owner": row["owner_user_id"], "run": row["run_id"]},
                )
            )
            .mappings()
            .one_or_none()
        )
        job_type = "automation_run" if occurrence is not None else "private_run"
        key = automation_run_idempotency_key(str(occurrence["id"])) if occurrence is not None else private_run_idempotency_key(str(row["run_id"]))
        requested_id = uuid.uuid5(_MIGRATION_NAMESPACE, f"{job_type}:{key}")
        await session.execute(
            text(
                """INSERT INTO jobs
                   (id,job_type,project_id,owner_user_id,run_id,
                    automation_occurrence_id,idempotency_key,status,priority,
                    available_at,attempt_count,max_attempts,retry_safety,
                    created_at,updated_at)
                   VALUES (:id,:type,:project,:owner,:run,:occurrence,:key,'queued',0,
                           now(),0,3,'safe',now(),now())
                   ON CONFLICT (job_type,idempotency_key) DO NOTHING"""
            ),
            {
                "id": requested_id,
                "type": job_type,
                "project": row["project_id"],
                "owner": row["owner_user_id"],
                "run": row["run_id"],
                "occurrence": occurrence["id"] if occurrence is not None else None,
                "key": key,
            },
        )
        job = (
            (
                await session.execute(
                    text(
                        """SELECT id,project_id,owner_user_id,run_id,automation_occurrence_id
                       FROM jobs WHERE job_type=:type AND idempotency_key=:key"""
                    ),
                    {"type": job_type, "key": key},
                )
            )
            .mappings()
            .one()
        )
        expected_occurrence = occurrence["id"] if occurrence is not None else None
        if (
            job["project_id"] != row["project_id"]
            or job["owner_user_id"] != row["owner_user_id"]
            or job["run_id"] != row["run_id"]
            or job["automation_occurrence_id"] != expected_occurrence
            or (row["job_id"] is not None and row["job_id"] != job["id"])
        ):
            raise ReliabilityMigrationError("durable job backfill authority conflicts")
        await session.execute(
            text(
                """UPDATE runs SET job_id=:job
                   WHERE project_id=:project AND owner_user_id=:owner AND run_id=:run
                     AND (job_id IS NULL OR job_id=:job)"""
            ),
            {"job": job["id"], "project": row["project_id"], "owner": row["owner_user_id"], "run": row["run_id"]},
        )
        if occurrence is not None:
            if occurrence["job_id"] is not None and occurrence["job_id"] != job["id"]:
                raise ReliabilityMigrationError("Automation job backfill authority conflicts")
            await session.execute(
                text(
                    """UPDATE scheduled_task_runs SET job_id=:job
                       WHERE project_id=:project AND owner_user_id=:owner AND id=:occurrence
                         AND (job_id IS NULL OR job_id=:job)"""
                ),
                {"job": job["id"], "project": row["project_id"], "owner": row["owner_user_id"], "occurrence": occurrence["id"]},
            )
    orphan = await session.scalar(
        text(
            """SELECT EXISTS (SELECT 1 FROM scheduled_task_runs
                               WHERE status='queued' AND (run_id IS NULL OR job_id IS NULL))"""
        )
    )
    if orphan:
        raise ReliabilityMigrationError("queued Automation occurrence cannot be migrated")


def _quota_service(factory: async_sessionmaker[AsyncSession]) -> QuotaService:
    try:
        keyring = AuditHmacKeyring.from_environment()
    except AuditHmacKeyringInvalid:
        raise ReliabilityMigrationError("audit HMAC keyring is unavailable") from None
    return QuotaService(factory, QuotaConfig(), source_ref_hasher=keyring)


async def _backfill_exact_reservations(database_url: str) -> None:
    """Backfill exact online-compatible reservation identities, idempotently."""

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = _quota_service(factory)
    try:
        async with factory() as session, session.begin():
            projects = list((await session.execute(text("SELECT id FROM projects ORDER BY id FOR UPDATE"))).scalars())
            await session.execute(text("LOCK TABLE project_memberships,files,runs IN SHARE ROW EXCLUSIVE MODE"))
            for project_id in projects:
                await session.execute(
                    text(
                        """INSERT INTO project_quotas (project_id,version,updated_at)
                           VALUES (:project,1,now()) ON CONFLICT (project_id) DO NOTHING"""
                    ),
                    {"project": project_id},
                )
                for dimension in ("members", "storage_bytes", "concurrent_runs", "mcp_calls_daily"):
                    await session.execute(
                        text(
                            """INSERT INTO project_usage_counters
                               (project_id,dimension,bucket,used,reserved,version,updated_at)
                               VALUES (:project,:dimension,'lifetime',0,0,1,now())
                               ON CONFLICT (project_id,dimension,bucket) DO NOTHING"""
                        ),
                        {"project": project_id, "dimension": dimension},
                    )

            desired: list[tuple[uuid.UUID, str, str, int, str]] = []
            members = (
                await session.execute(
                    text(
                        """SELECT project_id,user_id,id,version FROM project_memberships
                           WHERE status='active' ORDER BY project_id,user_id,id"""
                    )
                )
            ).mappings()
            for row in members:
                desired.append((row["project_id"], row["user_id"], "members", 1, f"member:{row['id']}:version:{row['version']}"))
            files = (
                await session.execute(
                    text(
                        """SELECT project_id,owner_user_id,id,size FROM files
                           WHERE status='ready' AND size>0 ORDER BY project_id,owner_user_id,id"""
                    )
                )
            ).mappings()
            for row in files:
                desired.append((row["project_id"], row["owner_user_id"], "storage_bytes", int(row["size"]), f"file:{row['id']}"))
            runs = (
                await session.execute(
                    text(
                        """SELECT project_id,owner_user_id,run_id FROM runs
                           WHERE status IN ('pending','running') ORDER BY project_id,owner_user_id,run_id"""
                    )
                )
            ).mappings()
            for row in runs:
                desired.append((row["project_id"], row["owner_user_id"], "concurrent_runs", 1, f"run:{row['run_id']}"))

            existing_totals: dict[tuple[uuid.UUID, str], int] = {}
            planned: list[tuple[uuid.UUID, str, str, int, str, object, str, bool]] = []
            for project_id, owner, dimension, amount, key in desired:
                refs = service._source_refs(
                    project_id=project_id,
                    owner_user_id=owner,
                    dimension=dimension,  # type: ignore[arg-type]
                    bucket="lifetime",
                    operation="reserve",
                    key=key,
                )
                matches = []
                for ref in refs:
                    idempotency = service._idempotency_digest(source_ref=ref)
                    existing = (
                        (
                            await session.execute(
                                text(
                                    """SELECT delta,bucket,source_kind,source_ref_key_id,source_ref_hmac
                                   FROM project_usage_ledger
                                   WHERE project_id=:project AND dimension=:dimension
                                     AND idempotency_key=:idempotency"""
                                ),
                                {"project": project_id, "dimension": dimension, "idempotency": idempotency},
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if existing is not None:
                        matches.append((ref, idempotency, existing))
                if len(matches) > 1:
                    raise ReliabilityMigrationError("quota reservation authority conflicts")
                if matches:
                    ref, idempotency, existing = matches[0]
                    if (
                        existing["delta"] != amount
                        or existing["bucket"] != "lifetime"
                        or existing["source_kind"] not in {"reserve", "reserve_threshold"}
                        or existing["source_ref_key_id"] != ref.key_id
                        or existing["source_ref_hmac"] != ref.hmac_hex
                    ):
                        raise ReliabilityMigrationError("quota reservation authority conflicts")
                    existing_totals[(project_id, dimension)] = existing_totals.get((project_id, dimension), 0) + amount
                    planned.append((project_id, owner, dimension, amount, key, ref, idempotency, True))
                else:
                    ref = refs[0]
                    planned.append((project_id, owner, dimension, amount, key, ref, service._idempotency_digest(source_ref=ref), False))

            counters = (
                await session.execute(
                    text(
                        """SELECT project_id,dimension,used,reserved FROM project_usage_counters
                           WHERE dimension IN ('members','storage_bytes','concurrent_runs')
                           FOR UPDATE"""
                    )
                )
            ).mappings()
            for counter in counters:
                if int(counter["used"]) != 0 or int(counter["reserved"]) != existing_totals.get((counter["project_id"], counter["dimension"]), 0):
                    raise ReliabilityMigrationError("aggregate-only quota history cannot be migrated")

            for project_id, _owner, dimension, amount, _key, ref, idempotency, exists in planned:
                if exists:
                    continue
                await session.execute(
                    text(
                        """INSERT INTO project_usage_ledger
                           (id,project_id,dimension,delta,bucket,source_kind,
                            source_ref_key_id,source_ref_hmac,idempotency_key,occurred_at)
                           VALUES (:id,:project,:dimension,:amount,'lifetime','reserve',
                                   :key_id,:hmac,:idempotency,now())"""
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project": project_id,
                        "dimension": dimension,
                        "amount": amount,
                        "key_id": ref.key_id,
                        "hmac": ref.hmac_hex,
                        "idempotency": idempotency,
                    },
                )
                await session.execute(
                    text(
                        """UPDATE project_usage_counters
                           SET reserved=reserved+:amount,version=version+1,updated_at=now()
                           WHERE project_id=:project AND dimension=:dimension
                             AND bucket='lifetime'"""
                    ),
                    {"amount": amount, "project": project_id, "dimension": dimension},
                )

        reconciler = QuotaReconciler(factory, service)
        for project_id in projects:
            report = await reconciler.execute(_issue_quota_reconciliation_authority(project_id, operation="quota_repair"))
            if report.differences:
                raise ReliabilityMigrationError("quota reconciliation required an aggregate repair")
    finally:
        await engine.dispose()


async def _initialize_recovery(connection: AsyncConnection) -> None:
    source_id = await source_installation_id(connection)
    await connection.execute(
        text(
            """INSERT INTO recovery_journal_state
               (id,source_installation_id,journal_id,high_watermark,head_digest,updated_at)
               VALUES (1,:source,:journal,0,:head,now()) ON CONFLICT (id) DO NOTHING"""
        ),
        {"source": source_id, "journal": uuid.uuid5(_MIGRATION_NAMESPACE, source_id), "head": "0" * 64},
    )
    existing = await connection.scalar(text("SELECT source_installation_id FROM recovery_journal_state WHERE id=1"))
    if existing != source_id:
        raise ReliabilityMigrationError("recovery journal source conflicts")


async def _probe_jobs(connection: AsyncConnection) -> None:
    invalid = await connection.scalar(
        text(
            """SELECT EXISTS (SELECT 1 FROM runs
                               WHERE status IN ('pending','running') AND job_id IS NULL)
               OR EXISTS (SELECT 1 FROM scheduled_task_runs
                          WHERE status IN ('queued','launching','running') AND job_id IS NULL)
               OR EXISTS (SELECT 1 FROM jobs job
                          LEFT JOIN projects project ON project.id=job.project_id
                          LEFT JOIN runs run ON run.project_id=job.project_id
                            AND run.owner_user_id=job.owner_user_id AND run.run_id=job.run_id
                          WHERE project.id IS NULL OR (job.run_id IS NOT NULL AND run.run_id IS NULL))"""
        )
    )
    if invalid:
        raise ReliabilityMigrationError("durable job relation probe failed")


async def _probe_quota(database_url: str) -> None:
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        service = _quota_service(factory)
        reconciler = QuotaReconciler(factory, service)
        async with engine.connect() as connection:
            projects = list((await connection.execute(text("SELECT id FROM projects ORDER BY id"))).scalars())
        for project_id in projects:
            report = await reconciler.preview(_issue_quota_reconciliation_authority(project_id, operation="quota_repair"))
            if report.differences:
                raise ReliabilityMigrationError("quota backfill probe failed")
    finally:
        await engine.dispose()


async def _probe_audit(connection: AsyncConnection) -> None:
    try:
        AuditHmacKeyring.from_environment()
    except AuditHmacKeyringInvalid:
        raise ReliabilityMigrationError("audit HMAC probe failed") from None
    if not await _table_exists(connection, "audit_logs"):
        raise ReliabilityMigrationError("audit schema probe failed")


async def _probe_stream(connection: AsyncConnection) -> None:
    mismatch = await connection.scalar(
        text(
            """SELECT EXISTS (
                 SELECT 1 FROM threads_meta thread
                 LEFT JOIN thread_event_sequences sequence
                   ON sequence.project_id=thread.project_id
                  AND sequence.owner_user_id=thread.owner_user_id
                  AND sequence.thread_id=thread.thread_id
                 LEFT JOIN LATERAL (
                   SELECT COALESCE(MAX(event.seq),0) AS high_watermark
                   FROM run_events event
                   WHERE event.project_id=thread.project_id
                     AND event.owner_user_id=thread.owner_user_id
                     AND event.thread_id=thread.thread_id
                 ) event ON true
                 WHERE sequence.high_watermark IS NULL
                    OR sequence.high_watermark<>event.high_watermark)"""
        )
    )
    if mismatch:
        raise ReliabilityMigrationError("durable stream probe failed")


async def _probe_recovery(connection: AsyncConnection) -> None:
    source_id = await source_installation_id(connection)
    ready = await connection.scalar(
        text(
            """SELECT EXISTS (SELECT 1 FROM recovery_journal_state
                               WHERE id=1 AND source_installation_id=:source
                                 AND high_watermark>=0)"""
        ),
        {"source": source_id},
    )
    if not ready:
        raise ReliabilityMigrationError("recovery probe failed")


async def _domain_receipts(connection: AsyncConnection) -> dict[str, tuple[str, int]]:
    values = {
        "jobs": int(await connection.scalar(text("SELECT count(*) FROM jobs")) or 0),
        "quotas": int(await connection.scalar(text("SELECT count(*) FROM project_usage_ledger")) or 0),
        "audit": int(await connection.scalar(text("SELECT count(*) FROM audit_logs")) or 0),
        "stream": int(await connection.scalar(text("SELECT count(*) FROM thread_event_sequences")) or 0),
        "recovery": int(await connection.scalar(text("SELECT count(*) FROM recovery_journal_state")) or 0),
    }
    return {domain: (hashlib.sha256(_canonical_json({"domain": domain, "count": count})).hexdigest(), count) for domain, count in values.items()}


async def _record_migration_ready(
    connection: AsyncConnection,
    *,
    run_id: uuid.UUID,
    source_digest: str,
    source_count: int,
    proof_digest: str,
) -> None:
    receipts = await _domain_receipts(connection)
    await connection.execute(
        text(
            """INSERT INTO reliability_migration_runs
               (id,mode,status,source_fingerprint,backup_proof_digest,source_row_count,
                source_probe_complete,active_run_probe_complete,started_at,completed_at)
               VALUES (:id,'execute','completed',:source,:proof,:count,true,true,now(),now())
               ON CONFLICT (id) DO UPDATE SET status='completed',completed_at=now(),
                 source_probe_complete=true,active_run_probe_complete=true"""
        ),
        {"id": run_id, "source": source_digest, "proof": proof_digest, "count": source_count},
    )
    for domain in _LEDGER_DOMAINS:
        digest, count = receipts[domain]
        await connection.execute(
            text(
                """INSERT INTO reliability_migration_ledger
                   (migration_run_id,domain,source_digest,target_digest,
                    source_row_count,target_row_count,status,completed_at)
                   VALUES (:run,:domain,:digest,:digest,:count,:count,'complete',now())
                   ON CONFLICT (migration_run_id,domain) DO NOTHING"""
            ),
            {"run": run_id, "domain": domain, "digest": digest, "count": count},
        )
    await connection.execute(
        text(
            """INSERT INTO reliability_cutover_state
               (id,stage,migration_run_id,empty_domain_probe_complete,
                source_probe_complete,active_run_probe_complete,
                quota_backfill_probe_complete,job_relation_probe_complete,
                audit_trigger_probe_complete,stream_probe_complete,
                recovery_probe_complete,final_schema_probe_complete,
                schema_revision,updated_at)
               VALUES (1,'migration_ready',:run,false,true,true,true,true,true,true,true,
                       false,:revision,now())
               ON CONFLICT (id) DO UPDATE SET stage='migration_ready',migration_run_id=:run,
                 empty_domain_probe_complete=false,source_probe_complete=true,
                 active_run_probe_complete=true,quota_backfill_probe_complete=true,
                 job_relation_probe_complete=true,audit_trigger_probe_complete=true,
                 stream_probe_complete=true,recovery_probe_complete=true,
                 final_schema_probe_complete=false,schema_revision=:revision,
                 cutover_at=NULL,updated_at=now()"""
        ),
        {"run": run_id, "revision": _EXPAND_REVISION},
    )


async def _ensure_running_receipt(
    engine: AsyncEngine,
    *,
    run_id: uuid.UUID,
    source_digest: str,
    source_count: int,
    proof_digest: str,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO reliability_migration_runs
                   (id,mode,status,source_fingerprint,backup_proof_digest,source_row_count,
                    source_probe_complete,active_run_probe_complete,started_at)
                   VALUES (:id,'execute','running',:source,:proof,:count,true,true,now())
                   ON CONFLICT (id) DO UPDATE SET status='running',completed_at=NULL"""
            ),
            {"id": run_id, "source": source_digest, "proof": proof_digest, "count": source_count},
        )


async def _record_authenticated_pre_m6_backup(
    engine: AsyncEngine,
    proof: _ValidatedBackupProof,
) -> None:
    try:
        keyring = AuditHmacKeyring.from_environment()
    except AuditHmacKeyringInvalid:
        raise ReliabilityMigrationError("audit HMAC keyring is unavailable") from None
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = AuditService(factory, keyring)
    sink = TrustedOperationAuditSink(
        service,
        process_context=_bind_operator_audit_process(service),
    )
    target_refs = keyring.audit_target_refs("backup", proof.archive_id)
    async with factory() as session, session.begin():
        exists = await session.scalar(
            text(
                """SELECT EXISTS (
                       SELECT 1
                       FROM audit_logs AS existing
                       JOIN unnest(
                           CAST(:key_ids AS text[]),
                           CAST(:target_refs AS text[])
                       ) AS candidate(key_id, target_ref)
                         ON existing.target_ref_key_id=candidate.key_id
                        AND existing.target_ref_hmac=candidate.target_ref
                       WHERE existing.action='backup.created'
                         AND existing.target_kind='backup')"""
            ),
            {
                "key_ids": [target_ref.key_id for target_ref in target_refs],
                "target_refs": [target_ref.hmac_hex for target_ref in target_refs],
            },
        )
        if exists is not True:
            await sink.backup_created(
                session,
                backup_id=proof.archive_id,
                table_count=proof.table_count,
                tombstone_high_watermark=proof.tombstone_journal_sequence,
                request_id=f"m6-pre-cutover-backup-{proof.archive_id}",
            )


async def _execute_migration(
    database_url: str,
    engine: AsyncEngine,
    *,
    backup_proof: Path,
    source_digest: str,
    source_count: int,
) -> ReliabilityMigrationReport:
    proof = await _validate_backup_proof(database_url, backup_proof, expected_source=source_digest)
    async with engine.connect() as connection:
        await _assert_project_cutovers(connection)
        await _assert_no_running_execution(connection)
        revision = await _current_revision(connection)
    if revision == _PRE_EXPAND_REVISION:
        await _upgrade(engine, _EXPAND_REVISION)
    async with engine.connect() as connection:
        if await _current_revision(connection) != _EXPAND_REVISION:
            raise ReliabilityMigrationError("M6 expand revision is required")
    run_id = uuid.uuid5(_MIGRATION_NAMESPACE, f"{source_digest}:{proof.digest}")
    await _ensure_running_receipt(
        engine,
        run_id=run_id,
        source_digest=source_digest,
        source_count=source_count,
        proof_digest=proof.digest,
    )
    await _record_authenticated_pre_m6_backup(engine, proof)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await _backfill_jobs(session)
    await _backfill_exact_reservations(database_url)
    async with engine.begin() as connection:
        await _initialize_recovery(connection)
    async with engine.connect() as connection:
        await _assert_project_cutovers(connection)
        await _assert_no_running_execution(connection)
        await _probe_jobs(connection)
        await _probe_audit(connection)
        await _probe_stream(connection)
        await _probe_recovery(connection)
    await _probe_quota(database_url)
    if await source_fingerprint(database_url) != source_digest:
        raise ReliabilityMigrationError("migration source fingerprint changed")
    async with engine.begin() as connection:
        await _record_migration_ready(
            connection,
            run_id=run_id,
            source_digest=source_digest,
            source_count=source_count,
            proof_digest=proof.digest,
        )
    await _upgrade(engine, _FINAL_REVISION)
    async with engine.connect() as connection:
        if await _current_revision(connection) != _FINAL_REVISION:
            raise ReliabilityMigrationError("M6 final revision probe failed")
        complete = await connection.scalar(
            text(
                """SELECT EXISTS (SELECT 1 FROM reliability_cutover_state
                                   WHERE id=1 AND stage='cutover_complete'
                                     AND final_schema_probe_complete AND cutover_at IS NOT NULL)"""
            )
        )
        if not complete:
            raise ReliabilityMigrationError("M6 final cutover marker is incomplete")
    return ReliabilityMigrationReport(
        mode="execute",
        revision=_FINAL_REVISION,
        cutover_complete=True,
        source_count=source_count,
        source_key_hash=source_digest[:12],
    )


async def run_reliability_migration(
    database_url: str,
    *,
    backup_proof: Path,
    execute: bool,
    maintenance_acknowledged: bool,
) -> ReliabilityMigrationReport:
    engine = create_async_engine(database_url)
    lock_engine: AsyncEngine | None = None
    try:
        async with engine.connect() as connection:
            revision = await _current_revision(connection)
            if revision == _FINAL_REVISION:
                complete = await connection.scalar(text("SELECT stage='cutover_complete' FROM reliability_cutover_state WHERE id=1"))
                if complete is not True:
                    raise ReliabilityMigrationError("M6 cutover marker is incomplete")
                return ReliabilityMigrationReport("execute" if execute else "dry-run", revision, True, 0, "", noop=True)
            await _assert_project_cutovers(connection)
            payload = await _source_payload(connection)
        source_digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
        source_count = sum(len(value) for value in payload.values() if isinstance(value, list))
        if not execute:
            await _validate_backup_proof(
                database_url,
                Path(backup_proof),
                expected_source=source_digest,
            )
            return ReliabilityMigrationReport("dry-run", revision, False, source_count, source_digest[:12])
        if not maintenance_acknowledged:
            raise ReliabilityMigrationError("maintenance window acknowledgement is required")
        lock_engine = create_async_engine(database_url, poolclass=NullPool)
        async with lock_engine.connect() as lock_connection:
            acquired = await lock_connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
            await lock_connection.commit()
            if acquired is not True:
                raise ReliabilityMigrationError("another reliability migration is active")
            try:
                return await _execute_migration(
                    database_url,
                    engine,
                    backup_proof=Path(backup_proof),
                    source_digest=source_digest,
                    source_count=source_count,
                )
            finally:
                try:
                    await lock_connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY})
                    await lock_connection.commit()
                except Exception:
                    await lock_connection.invalidate()
    except ReliabilityMigrationError:
        raise
    except Exception as error:
        raise ReliabilityMigrationError(f"M6 reliability migration failed: {type(error).__name__}") from None
    finally:
        await engine.dispose()
        if lock_engine is not None:
            await lock_engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--attest-backup", action="store_true")
    parser.add_argument("--backup-proof", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--backup-commit", type=Path)
    parser.add_argument("--proof-output", type=Path)
    parser.add_argument("--restore-verified", action="store_true")
    parser.add_argument("--maintenance-acknowledged", action="store_true")
    return parser


def _render(report: ReliabilityMigrationReport) -> str:
    return json.dumps(
        {
            "cutover_complete": report.cutover_complete,
            "mode": report.mode,
            "noop": report.noop,
            "revision": report.revision,
            "source_count": report.source_count,
            "source_key_hash": report.source_key_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


async def _main_async(args: argparse.Namespace) -> ReliabilityMigrationReport:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ReliabilityMigrationError("DATABASE_URL is required")
    if args.backup_proof is None:
        raise ReliabilityMigrationError("--backup-proof is required")
    return await run_reliability_migration(
        database_url,
        backup_proof=args.backup_proof,
        execute=args.execute,
        maintenance_acknowledged=args.maintenance_acknowledged,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.attest_backup:
            database_url = os.environ.get("DATABASE_URL")
            if not database_url:
                raise ReliabilityMigrationError("DATABASE_URL is required")
            if args.archive is None or args.backup_commit is None or args.proof_output is None:
                raise ReliabilityMigrationError("--attest-backup requires --archive, --backup-commit, and --proof-output")

            async def attest() -> None:
                await create_authenticated_backup_proof(
                    database_url,
                    archive=args.archive,
                    backup_commit=args.backup_commit,
                    output=args.proof_output,
                    source_digest=await source_fingerprint(database_url),
                    restore_verified=args.restore_verified,
                )

            asyncio.run(attest())
            print('{"backup_proof":"created"}')
            return 0
        print(_render(asyncio.run(_main_async(args))))
        return 0
    except ReliabilityMigrationError as error:
        print(f"error: {error}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ReliabilityMigrationError",
    "ReliabilityMigrationReport",
    "_backfill_exact_reservations",
    "_probe_recovery",
    "catalog_digest",
    "create_authenticated_backup_proof",
    "read_source_installation_id",
    "run_reliability_migration",
    "source_fingerprint",
]
