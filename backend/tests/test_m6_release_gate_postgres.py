from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from postgres_utils import replace_database
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.m4_private_threads import seed_m4_thread_database

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditMetadataRejected,
    AuditOutcome,
    AuditTarget,
    AuditTargetKind,
)
from app.audit.service import AuditService, _bind_recovery_audit_process
from app.audit.sinks import TrustedOperationAuditSink
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.quotas.models import QuotaExceeded, QuotaSourceRef
from app.quotas.service import QuotaService
from app.recovery import BackupConfig, create_backup
from app.recovery.journal import TombstoneJournal, TombstoneSequenceGap
from app.recovery.purge import RetentionCandidate, RetentionPurger
from app.recovery.restore import (
    RestoreAuthenticationFailed,
    RestoreConfig,
    Restorer,
)
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.private_work.model import PrivateFileRow
from deerflow.persistence.recovery.model import RecoveryJournalStateRow, RestoreProofRow

NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)
BACKUP_KEY = b"b" * 32
JOURNAL_KEY = b"j" * 32


def _keyring() -> AuditHmacKeyring:
    return AuditHmacKeyring(
        active_key_id="release-audit-v1",
        _keys={"release-audit-v1": b"a" * 32},
    )


def _source_ref(payload: bytes) -> QuotaSourceRef:
    return QuotaSourceRef(
        key_id="release-quota-v1",
        hmac_hex=hmac.new(b"q" * 32, payload, hashlib.sha256).hexdigest(),
    )


def _recovery_audit(factory) -> TrustedOperationAuditSink:
    service = AuditService(factory, _keyring())
    return TrustedOperationAuditSink(
        service,
        process_context=_bind_recovery_audit_process(service),
    )


async def _child_state(
    process: asyncio.subprocess.Process,
    expected: str,
    *,
    timeout: float = 10,
) -> dict[str, object]:
    assert process.stdout is not None
    deadline = asyncio.get_running_loop().time() + timeout
    lines: list[str] = []
    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
        if not line:
            stderr = b""
            if process.stderr is not None:
                stderr = await process.stderr.read()
            raise AssertionError(f"scheduler child exited before {expected}: {stderr.decode(errors='replace')}")
        decoded = line.decode().strip()
        lines.append(decoded)
        payload = json.loads(decoded)
        if payload.get("state") == expected:
            return payload
    raise AssertionError(f"scheduler child did not reach {expected}: {lines}")


async def _stop_child(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=5)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_scheduler_lock_takeover_uses_a_different_process_and_session(
    migrated_postgres_database_url: str,
) -> None:
    child_path = Path(__file__).parent / "support" / "m6_scheduler_ownership_child.py"
    command = (sys.executable, str(child_path), str(migrated_postgres_database_url))
    environment = os.environ.copy()
    backend_root = str(Path(__file__).parent.parent)
    environment["PYTHONPATH"] = os.pathsep.join(value for value in (backend_root, environment.get("PYTHONPATH", "")) if value)
    first = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    second: asyncio.subprocess.Process | None = None
    try:
        first_owned = await _child_state(first, "owned")
        second = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        await _child_state(second, "contended")

        first.kill()
        await asyncio.wait_for(first.wait(), timeout=5)
        second_owned = await _child_state(second, "owned")

        assert first_owned["backend_pid"] != second_owned["backend_pid"]
        assert first.returncode == -signal.SIGKILL
    finally:
        await _stop_child(first)
        if second is not None:
            await _stop_child(second)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_quota_race_and_audit_redaction_are_fail_closed(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quota = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    secret = f"release-secret-{uuid.uuid4()}"
    audit = AuditService(seed.factory, _keyring())
    try:
        held = await quota.reserve_new_session(
            seed.owner_a,
            "concurrent_runs",
            1,
            "release:held",
        )
        contenders = await asyncio.gather(
            *(
                quota.reserve_new_session(
                    seed.owner_a,
                    "concurrent_runs",
                    1,
                    f"release:race:{index}",
                )
                for index in range(5)
            ),
            return_exceptions=True,
        )
        accepted = [item for item in contenders if not isinstance(item, Exception)]
        rejected = [item for item in contenders if isinstance(item, Exception)]
        assert len(accepted) == 2
        assert len(rejected) == 3
        assert all(isinstance(item, QuotaExceeded) for item in rejected)
        assert sum(item.threshold_crossed for item in (held, *accepted)) == 1

        actor = AuditActor.user(seed.owner_a.user_id)
        target = AuditTarget(
            kind=AuditTargetKind.RUN,
            authority_id=uuid.uuid4(),
            project_id=seed.owner_a.project_id,
        )
        async with seed.factory() as session, session.begin():
            with pytest.raises(AuditMetadataRejected) as captured:
                await audit.append(
                    session,
                    actor,
                    AuditAction.RUN_ADMITTED,
                    target,
                    AuditOutcome.SUCCESS,
                    {
                        "job_type": "private_run",
                        "non_interactive": False,
                        "token": secret,
                    },
                )
            assert secret not in str(captured.value)
            assert secret not in repr(captured.value)
            await audit.append(
                session,
                actor,
                AuditAction.RUN_ADMITTED,
                target,
                AuditOutcome.SUCCESS,
                {"job_type": "private_run", "non_interactive": False},
            )

        async with seed.factory() as session:
            rows = (
                await session.execute(
                    select(AuditLogRow).where(
                        AuditLogRow.project_id == seed.owner_a.project_id,
                    )
                )
            ).scalars()
            encoded = json.dumps(
                [row.metadata_json for row in rows],
                sort_keys=True,
            )
        assert secret not in encoded
    finally:
        await seed.engine.dispose()


async def _seed_purge_candidate(seed) -> uuid.UUID:
    file_id = uuid.uuid4()
    thread_id = f"release-restore-{uuid.uuid4()}"
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        session.add(
            PrivateFileRow(
                id=file_id,
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                thread_id=thread_id,
                kind="upload",
                logical_path="release-restore.txt",
                media_type="text/plain",
                size=6,
                sha256="0" * 64,
                status="deleted",
                deleted_at=NOW - timedelta(days=31),
            )
        )
        await session.flush()
        await session.execute(
            text(
                """INSERT INTO file_chunks (file_id,chunk_index,content,size,sha256)
                   VALUES (:file_id,0,:content,6,:sha256)"""
            ),
            {
                "file_id": file_id,
                "content": b"secret",
                "sha256": "2bb80d537b1da3e38bd30361aa855686bde0ba0a5e7e0627f31e1d3da249cc42",
            },
        )
    return file_id


async def _database_exists(admin_url: str, database: str) -> bool:
    engine = create_async_engine(
        replace_database(admin_url, "postgres"),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with engine.connect() as connection:
            return bool(
                await connection.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=:name)"),
                    {"name": database},
                )
            )
    finally:
        await engine.dispose()


async def _drop_restore_database(admin_url: str, target_url: str) -> None:
    database = make_url(target_url).database or ""
    assert database.startswith("deerflow_restore_")
    engine = create_async_engine(
        replace_database(admin_url, "postgres"),
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name AND pid<>pg_backend_pid()"),
                {"name": database},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_encrypted_archive_restore_replays_journal_and_rejects_tamper_and_gap(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_JWT_SECRET", "task19-release-distinct-auth-secret")
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    psycopg_url = str(migrated_postgres_database_url).replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )
    async with AsyncPostgresSaver.from_conn_string(psycopg_url) as saver:
        await saver.setup()
    async with AsyncPostgresStore.from_conn_string(psycopg_url) as store:
        await store.setup()

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    file_id = await _seed_purge_candidate(seed)
    archive = tmp_path / "release.dfba"
    manifest = await create_backup(
        BackupConfig(
            database_url=migrated_postgres_database_url,
            output=archive,
            key=BACKUP_KEY,
        )
    )
    journal = TombstoneJournal(
        tmp_path / "journal" / "tombstones.jsonl",
        JOURNAL_KEY,
    )
    await RetentionPurger(
        seed.factory,
        journal=journal,
        keyring=_keyring(),
        audit=_recovery_audit(seed.factory),
    ).purge(
        RetentionCandidate.file(
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            file_id=file_id,
            deleted_at=NOW - timedelta(days=31),
            idempotency_key=f"release-restore:{file_id}",
            request_id="task19-release-restore-purge",
        ),
        now=NOW,
    )

    target_urls = [
        replace_database(
            migrated_postgres_database_url,
            f"deerflow_restore_{os.getpid()}_{uuid.uuid4().hex}",
        )
        for _ in range(3)
    ]
    try:
        tampered_archive = tmp_path / "tampered.dfba"
        shutil.copytree(archive, tampered_archive)
        chunk = tampered_archive / "chunks" / "00000000.bin"
        payload = bytearray(chunk.read_bytes())
        payload[0] ^= 1
        chunk.write_bytes(payload)
        with pytest.raises(RestoreAuthenticationFailed):
            await Restorer(
                RestoreConfig(
                    archive=tampered_archive,
                    target_database_url=target_urls[0],
                    current_database_url=migrated_postgres_database_url,
                    journal=journal,
                    backup_key=BACKUP_KEY,
                    keyring=_keyring(),
                )
            ).restore()
        assert not await _database_exists(
            migrated_postgres_database_url,
            make_url(target_urls[0]).database or "",
        )

        gap_path = tmp_path / "gap" / "tombstones.jsonl"
        gap_path.parent.mkdir()
        shutil.copy2(journal.path, gap_path)
        lines = gap_path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["sequence"] = 2
        lines[1] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        gap_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(TombstoneSequenceGap):
            await Restorer(
                RestoreConfig(
                    archive=archive,
                    target_database_url=target_urls[1],
                    current_database_url=migrated_postgres_database_url,
                    journal=TombstoneJournal(gap_path, JOURNAL_KEY),
                    backup_key=BACKUP_KEY,
                    keyring=_keyring(),
                )
            ).restore()
        assert not await _database_exists(
            migrated_postgres_database_url,
            make_url(target_urls[1]).database or "",
        )

        restorer = Restorer(
            RestoreConfig(
                archive=archive,
                target_database_url=target_urls[2],
                current_database_url=migrated_postgres_database_url,
                journal=journal,
                backup_key=BACKUP_KEY,
                keyring=_keyring(),
            )
        )
        result = await restorer.restore()
        assert restorer.owns_verified_target(result)
        assert result.archive_id == manifest.archive_id
        assert result.tombstones_replayed == 1
        assert result.probes_complete is True
        assert result.status == "verified"

        target_engine = create_async_engine(target_urls[2])
        try:
            async with async_sessionmaker(target_engine)() as session:
                assert await session.scalar(select(PrivateFileRow.id).where(PrivateFileRow.id == file_id)) is None
                proof = await session.get(RestoreProofRow, result.proof_id)
                state = await session.get(RecoveryJournalStateRow, 1)
                assert proof is not None and proof.probes_complete is True
                assert proof.replayed_through_sequence == 1
                assert state is not None and state.high_watermark == 1
        finally:
            await target_engine.dispose()
    finally:
        await seed.engine.dispose()
        for target_url in target_urls:
            database = make_url(target_url).database or ""
            if await _database_exists(migrated_postgres_database_url, database):
                await _drop_restore_database(
                    migrated_postgres_database_url,
                    target_url,
                )


def test_cross_platform_release_runner_requires_url_and_fails_a_real_child_skip(tmp_path: Path) -> None:
    backend_tests = Path(__file__).parent
    runner = backend_tests / "support" / "release_gate_plugin.py"
    child = tmp_path / "child"
    child.mkdir()
    (child / "conftest.py").write_text(
        f"import sys\nsys.path.insert(0, {str(backend_tests)!r})\nfrom support.release_gate_plugin import pytest_sessionfinish\n",
        encoding="utf-8",
    )
    (child / "test_skip.py").write_text(
        "import pytest\n\ndef test_release_skip():\n    pytest.skip('release evidence unavailable')\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("POSTGRES_TEST_URL", None)
    missing_url = subprocess.run(
        [sys.executable, str(runner), "test_skip.py", "-q"],
        cwd=child,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert missing_url.returncode != 0
    assert "POSTGRES_TEST_URL is required for the PostgreSQL release gate" in (missing_url.stdout + missing_url.stderr)

    environment["POSTGRES_TEST_URL"] = "postgresql://release.invalid/postgres"
    environment["DEER_FLOW_RELEASE_GATE_LABEL"] = "M1-M7"
    result = subprocess.run(
        [sys.executable, str(runner), "test_skip.py", "-q"],
        cwd=child,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "M1-M7 release stats: collected=1 passed=0 failed=0 skipped=1" in output


def test_removed_database_migration_clis_do_not_return() -> None:
    backend = Path(__file__).parent.parent
    root_makefile = backend.parent.joinpath("Makefile").read_text(encoding="utf-8")
    backend_makefile = backend.joinpath("Makefile").read_text(encoding="utf-8")
    for stem in (
        "migrate_sqlite_to_postgres",
        "migrate_assets",
        "migrate_private_work",
        "migrate_automations",
        "migrate_reliability",
    ):
        assert not backend.joinpath("scripts", f"{stem}.py").exists()
    for target in (
        "migrate-sqlite:",
        "migrate-assets:",
        "migrate-private-work:",
        "migrate-automations:",
        "migrate-reliability:",
    ):
        assert target not in root_makefile
        assert target not in backend_makefile
