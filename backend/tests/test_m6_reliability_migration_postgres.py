from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from support.m5_automation import (
    M5LegacyMigrationDatabase,
    isolated_m5_legacy_migration_database,
)

from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.recovery import BackupArchiveWriter
from app.recovery.pre_cutover_backup import publish_pre_cutover_backup_commit
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.config.quota_config import QuotaConfig
from scripts.migrate_automations import run_automation_migration
from scripts.migrate_reliability import (
    ReliabilityMigrationError,
    catalog_digest,
    create_authenticated_backup_proof,
    read_source_installation_id,
    run_reliability_migration,
    source_fingerprint,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@dataclass(frozen=True)
class M6MigrationDatabase:
    database: M5LegacyMigrationDatabase
    proof: Path
    archive: Path
    backup_commit: Path


async def _refresh_backup_proof(database: M6MigrationDatabase) -> None:
    database.proof.unlink(missing_ok=True)
    await create_authenticated_backup_proof(
        database.database.url,
        archive=database.archive,
        backup_commit=database.backup_commit,
        output=database.proof,
        source_digest=await source_fingerprint(database.database.url),
        restore_verified=True,
    )


@pytest_asyncio.fixture()
async def m6_migration_database(
    postgres_admin_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> M6MigrationDatabase:
    async with isolated_m5_legacy_migration_database(
        postgres_admin_url,
        tmp_path / "m5-backup-proof",
    ) as database:
        result = await run_automation_migration(
            database.url,
            owner_map=database.owner_map,
            backup_dir=database.backup_dir,
            execute=True,
        )
        assert result.cutover_complete is True
        assert await database.current_revision() == "0013_project_automation_finalize"

        backup_key = hashlib.sha256(b"m6-task18-backup-key-material").digest()
        monkeypatch.setenv(
            "DEER_FLOW_BACKUP_KEY",
            base64.b64encode(backup_key).decode("ascii"),
        )
        archive = tmp_path / "m6-task18.dfba"
        source_id = await read_source_installation_id(database.url)
        with BackupArchiveWriter.atomic(
            archive,
            backup_key,
            source_installation_id=source_id,
            schema_revision="0013_project_automation_finalize",
            archive_id=str(uuid.uuid4()),
        ) as writer:
            writer.write_chunk(b"PGDMPtask18-proof")
            manifest = writer.finalize(
                database_high_watermark=0,
                tombstone_journal_sequence=0,
            )
        backup_commit = tmp_path / "m6-task18.dfba.commit.json"
        parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            commit_handle = publish_pre_cutover_backup_commit(
                parent_fd=parent_fd,
                name=backup_commit.name,
                manifest=manifest.as_dict(),
                manifest_envelope=(archive / "manifest.json").read_bytes(),
                key=backup_key,
            )
            commit_handle.commit()
        finally:
            os.close(parent_fd)
        proof = tmp_path / "m6-backup-proof.json"
        await create_authenticated_backup_proof(
            database.url,
            archive=archive,
            backup_commit=backup_commit,
            output=proof,
            source_digest=await source_fingerprint(database.url),
            restore_verified=True,
        )
        attestation = json.loads(proof.read_bytes())
        signature = attestation.pop("signature")
        raw_key_signature = hmac.new(
            backup_key,
            b"deerflow.m6.backup-proof.v1\x00"
            + json.dumps(
                attestation,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert not hmac.compare_digest(signature, raw_key_signature)
        yield M6MigrationDatabase(
            database=database,
            proof=proof,
            archive=archive,
            backup_commit=backup_commit,
        )


async def test_dry_run_preserves_catalog_counters_ledgers_and_sequences(
    m6_migration_database: M6MigrationDatabase,
) -> None:
    database = m6_migration_database.database
    before = await catalog_digest(database.url)

    report = await run_reliability_migration(
        database.url,
        backup_proof=m6_migration_database.proof,
        execute=False,
        maintenance_acknowledged=False,
    )

    assert report.mode == "dry-run"
    assert report.revision == "0013_project_automation_finalize"
    assert report.cutover_complete is False
    assert await catalog_digest(database.url) == before


async def test_execute_requires_maintenance_and_backup_before_expand_ddl(
    m6_migration_database: M6MigrationDatabase,
    tmp_path: Path,
) -> None:
    database = m6_migration_database.database
    for maintenance, proof in (
        (False, m6_migration_database.proof),
        (True, tmp_path / "missing-proof"),
    ):
        with pytest.raises(ReliabilityMigrationError):
            await run_reliability_migration(
                database.url,
                backup_proof=proof,
                execute=True,
                maintenance_acknowledged=maintenance,
            )
        assert await database.current_revision() == "0013_project_automation_finalize"
        async with database.engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('reliability_migration_runs')")) is None


async def test_active_legacy_run_blocks_before_expand(
    m6_migration_database: M6MigrationDatabase,
) -> None:
    database = m6_migration_database.database
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE runs SET status='running'
                   WHERE project_id=:project_id AND run_id=:run_id"""
            ),
            {
                "project_id": database.seed.owner_a.project_id,
                "run_id": database.history_run_id,
            },
        )
    await _refresh_backup_proof(m6_migration_database)
    with pytest.raises(ReliabilityMigrationError):
        await run_reliability_migration(
            database.url,
            backup_proof=m6_migration_database.proof,
            execute=True,
            maintenance_acknowledged=True,
        )
    assert await database.current_revision() == "0013_project_automation_finalize"


async def test_execute_writes_exact_reservations_and_online_releases_are_idempotent(
    m6_migration_database: M6MigrationDatabase,
) -> None:
    database = m6_migration_database.database
    async with database.engine.begin() as connection:
        file_id = await connection.scalar(
            text(
                """INSERT INTO files
                   (id,project_id,owner_user_id,thread_id,kind,logical_path,media_type,
                    size,sha256,status,created_at,updated_at)
                   VALUES (:id,:project_id,:owner,:thread_id,'upload','m6.bin',
                           'application/octet-stream',37,:digest,'ready',now(),now())
                   RETURNING id"""
            ),
            {
                "id": uuid.uuid4(),
                "project_id": database.seed.owner_a.project_id,
                "owner": str(database.seed.owner_a.user_id),
                "thread_id": database.reuse_thread_id,
                "digest": hashlib.sha256(b"m6").hexdigest(),
            },
        )

    await _refresh_backup_proof(m6_migration_database)

    first = await run_reliability_migration(
        database.url,
        backup_proof=m6_migration_database.proof,
        execute=True,
        maintenance_acknowledged=True,
    )
    assert first.cutover_complete is True
    assert first.revision == "0015_project_reliability_finalize"

    async with database.engine.connect() as connection:
        marker = (
            await connection.execute(
                text(
                    """SELECT stage,source_probe_complete,active_run_probe_complete,
                              quota_backfill_probe_complete,job_relation_probe_complete,
                              audit_trigger_probe_complete,stream_probe_complete,
                              recovery_probe_complete,final_schema_probe_complete,
                              schema_revision,cutover_at
                       FROM reliability_cutover_state WHERE id=1"""
                )
            )
        ).one()
        assert marker.stage == "cutover_complete"
        assert all(marker[1:9])
        assert marker.schema_revision == "0015_project_reliability_finalize"
        assert marker.cutover_at is not None
        assert set((await connection.execute(text("SELECT domain FROM reliability_migration_ledger"))).scalars()) == {"jobs", "quotas", "audit", "stream", "recovery"}
        assert (
            await connection.scalar(
                text(
                    """SELECT count(*) FROM project_usage_ledger
                   WHERE project_id=:project_id
                     AND source_kind IN ('reserve','reserve_threshold')"""
                ),
                {"project_id": database.seed.owner_a.project_id},
            )
            >= 4
        )
        assert (
            await connection.scalar(
                text(
                    """SELECT count(*) FROM audit_logs
                   WHERE action='backup.created' AND actor_process='operator'
                     AND target_kind='backup'"""
                )
            )
            == 1
        )

    keyring = AuditHmacKeyring.from_environment()
    enforcer = ProjectQuotaEnforcer(
        QuotaService(
            database.seed.factory,
            QuotaConfig(),
            source_ref_hasher=keyring,
        )
    )
    for request_id in ("migration-release", "migration-release-retry"):
        async with database.seed.factory() as session, session.begin():
            await enforcer.release_file(
                session,
                database.seed.owner_a_scope,
                file_id=file_id,
                size=37,
                request_id=request_id,
            )
            await enforcer.release_member(
                session,
                database.seed.owner_a_scope,
                membership_id=database.seed.owner_a.membership_id,
                membership_version=database.seed.owner_a.membership_version,
            )
    async with database.engine.connect() as connection:
        assert (
            await connection.scalar(
                text(
                    """SELECT reserved FROM project_usage_counters
                   WHERE project_id=:project_id AND dimension='storage_bytes'
                     AND bucket='lifetime'"""
                ),
                {"project_id": database.seed.owner_a.project_id},
            )
            == 0
        )
        assert (
            await connection.scalar(
                text(
                    """SELECT count(*) FROM project_usage_ledger
                   WHERE project_id=:project_id AND source_kind='release'"""
                ),
                {"project_id": database.seed.owner_a.project_id},
            )
            == 2
        )

    second = await run_reliability_migration(
        database.url,
        backup_proof=m6_migration_database.proof,
        execute=True,
        maintenance_acknowledged=True,
    )
    assert second.noop is True


async def test_recovery_probe_failure_stops_at_expand_and_resume_is_idempotent(
    m6_migration_database: M6MigrationDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.migrate_reliability as migration

    database = m6_migration_database.database
    original = migration._probe_recovery

    async def fail_recovery(*_args, **_kwargs):
        raise ReliabilityMigrationError("RECOVERY_PROBE_FAILED")

    monkeypatch.setattr(migration, "_probe_recovery", fail_recovery)
    with pytest.raises(ReliabilityMigrationError, match="RECOVERY_PROBE_FAILED"):
        await run_reliability_migration(
            database.url,
            backup_proof=m6_migration_database.proof,
            execute=True,
            maintenance_acknowledged=True,
        )
    assert await database.current_revision() == "0014_project_reliability_expand"
    async with database.engine.connect() as connection:
        assert not await connection.scalar(
            text(
                """SELECT EXISTS (
                       SELECT 1 FROM reliability_cutover_state
                       WHERE stage='cutover_complete')"""
            )
        )
        first_exact = await connection.scalar(text("SELECT count(*) FROM project_usage_ledger WHERE source_kind='reserve'"))

    monkeypatch.setattr(migration, "_probe_recovery", original)
    result = await run_reliability_migration(
        database.url,
        backup_proof=m6_migration_database.proof,
        execute=True,
        maintenance_acknowledged=True,
    )
    assert result.cutover_complete is True
    async with database.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM project_usage_ledger WHERE source_kind='reserve'")) == first_exact
        assert await connection.scalar(text("SELECT count(*) FROM reliability_migration_runs")) == 1
        assert await connection.scalar(text("SELECT count(*) FROM audit_logs WHERE action='backup.created'")) == 1


async def test_backup_proof_tamper_and_wrong_source_fail_before_expand(
    m6_migration_database: M6MigrationDatabase,
) -> None:
    database = m6_migration_database.database
    original = m6_migration_database.proof.read_bytes()
    replacements = (
        original[:-1] + (b"0" if original[-1:] != b"0" else b"1"),
        original.replace(
            b'"source_fingerprint":"',
            b'"source_fingerprint":"00',
            1,
        ),
    )
    for replacement in replacements:
        m6_migration_database.proof.write_bytes(replacement)
        with pytest.raises(ReliabilityMigrationError):
            await run_reliability_migration(
                database.url,
                backup_proof=m6_migration_database.proof,
                execute=True,
                maintenance_acknowledged=True,
            )
        assert await database.current_revision() == "0013_project_automation_finalize"
    m6_migration_database.proof.write_bytes(original)
    commit = m6_migration_database.backup_commit.read_bytes()
    m6_migration_database.backup_commit.write_bytes(commit[:-1] + b"0")
    with pytest.raises(ReliabilityMigrationError):
        await run_reliability_migration(
            database.url,
            backup_proof=m6_migration_database.proof,
            execute=True,
            maintenance_acknowledged=True,
        )
    assert await database.current_revision() == "0013_project_automation_finalize"
    m6_migration_database.backup_commit.write_bytes(commit)


async def test_pending_run_is_backfilled_end_to_end_and_terminal_release_is_idempotent(
    m6_migration_database: M6MigrationDatabase,
) -> None:
    database = m6_migration_database.database
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE runs SET status='pending'
                   WHERE project_id=:project_id AND run_id=:run_id"""
            ),
            {
                "project_id": database.seed.owner_a.project_id,
                "run_id": database.history_run_id,
            },
        )

    await _refresh_backup_proof(m6_migration_database)

    result = await run_reliability_migration(
        database.url,
        backup_proof=m6_migration_database.proof,
        execute=True,
        maintenance_acknowledged=True,
    )
    assert result.cutover_complete is True
    async with database.engine.connect() as connection:
        run = (
            await connection.execute(
                text(
                    """SELECT run.status,run.job_id,job.status AS job_status
                       FROM runs run JOIN jobs job ON job.id=run.job_id
                       WHERE run.project_id=:project_id AND run.run_id=:run_id"""
                ),
                {
                    "project_id": database.seed.owner_a.project_id,
                    "run_id": database.history_run_id,
                },
            )
        ).one()
        assert run.status == "pending"
        assert run.job_id is not None
        assert run.job_status == "queued"

    enforcer = ProjectQuotaEnforcer(
        QuotaService(
            database.seed.factory,
            QuotaConfig(),
            source_ref_hasher=AuditHmacKeyring.from_environment(),
        )
    )
    for request_id in ("migration-terminal", "migration-terminal-retry"):
        async with database.seed.factory() as session, session.begin():
            await enforcer.release_concurrent_run(
                session,
                database.seed.owner_a_scope,
                run_id=database.history_run_id,
                request_id=request_id,
            )
            await session.execute(
                text(
                    """UPDATE runs SET status='success'
                       WHERE project_id=:project_id AND owner_user_id=:owner
                         AND run_id=:run_id"""
                ),
                {
                    "project_id": database.seed.owner_a.project_id,
                    "owner": str(database.seed.owner_a.user_id),
                    "run_id": database.history_run_id,
                },
            )
    async with database.engine.connect() as connection:
        assert (
            await connection.scalar(
                text(
                    """SELECT reserved FROM project_usage_counters
                   WHERE project_id=:project_id AND dimension='concurrent_runs'
                     AND bucket='lifetime'"""
                ),
                {"project_id": database.seed.owner_a.project_id},
            )
            == 0
        )
        assert (
            await connection.scalar(
                text(
                    """SELECT count(*) FROM project_usage_ledger
                   WHERE project_id=:project_id
                     AND dimension='concurrent_runs' AND source_kind='release'"""
                ),
                {"project_id": database.seed.owner_a.project_id},
            )
            == 1
        )


async def test_aggregate_only_quota_fixture_is_rejected(
    m6_migration_database: M6MigrationDatabase,
) -> None:
    database = m6_migration_database.database
    await database.upgrade("0014_project_reliability_expand")
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO project_usage_counters
                   (project_id,dimension,bucket,used,reserved,version,updated_at)
                   VALUES (:project_id,'members','lifetime',0,3,1,now())"""
            ),
            {"project_id": database.seed.owner_a.project_id},
        )
        await connection.execute(
            text(
                """INSERT INTO project_usage_ledger
                   (id,project_id,dimension,delta,bucket,source_kind,
                    source_ref_key_id,source_ref_hmac,idempotency_key,occurred_at)
                   VALUES (gen_random_uuid(),:project_id,'members',3,'lifetime',
                           'reconcile_adjustment','test-key',:digest,:digest,now())"""
            ),
            {"project_id": database.seed.owner_a.project_id, "digest": "a" * 64},
        )

    with pytest.raises(ReliabilityMigrationError):
        await run_reliability_migration(
            database.url,
            backup_proof=m6_migration_database.proof,
            execute=True,
            maintenance_acknowledged=True,
        )
    assert await database.current_revision() == "0014_project_reliability_expand"
    async with database.engine.connect() as connection:
        assert not await connection.scalar(
            text(
                """SELECT EXISTS (
                       SELECT 1 FROM reliability_cutover_state
                       WHERE stage='cutover_complete')"""
            )
        )
