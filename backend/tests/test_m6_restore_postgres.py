from __future__ import annotations

import base64
import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from postgres_utils import replace_database
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.m4_private_threads import seed_m4_thread_database

import app.recovery.restore as restore_module
from app.audit.service import AuditService, _bind_recovery_audit_process
from app.audit.sinks import TrustedOperationAuditSink
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.recovery import BackupArchiveWriter, BackupConfig, create_backup
from app.recovery.journal import TombstoneJournal, TombstoneSequenceGap
from app.recovery.purge import RetentionCandidate, RetentionPurger
from app.recovery.restore import (
    RecoveryProbeFailed,
    RestoreAuthenticationFailed,
    RestoreConfig,
    Restorer,
    RestoreTargetRejected,
    drill_restore,
    replay_tombstones,
)
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.persistence.private_work.model import PrivateFileRow
from deerflow.persistence.recovery.model import DeletionTombstoneRow, RestoreProofRow

NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)
EXPIRED = NOW - timedelta(days=31)
BACKUP_KEY = b"b" * 32
JOURNAL_KEY = b"j" * 32
_RESTORE_NAME = re.compile(r"deerflow_restore_[0-9]+_[0-9a-f]{32}\Z")


def _keyring() -> AuditHmacKeyring:
    return AuditHmacKeyring(active_key_id="audit-v1", _keys={"audit-v1": b"a" * 32})


def _recovery_audit(factory) -> TrustedOperationAuditSink:
    service = AuditService(factory, _keyring())
    return TrustedOperationAuditSink(service, process_context=_bind_recovery_audit_process(service))


def _restore_url(source_url: str) -> str:
    return replace_database(source_url, f"deerflow_restore_{os.getpid()}_{uuid.uuid4().hex}")


async def _database_exists(admin_url: str, database: str) -> bool:
    engine = create_async_engine(replace_database(admin_url, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            return bool(await connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=:name)"), {"name": database}))
    finally:
        await engine.dispose()


async def _drop_restore_database(admin_url: str, target_url: str) -> None:
    from sqlalchemy.engine import make_url

    database = make_url(target_url).database or ""
    assert _RESTORE_NAME.fullmatch(database)
    engine = create_async_engine(replace_database(admin_url, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name AND pid<>pg_backend_pid()"),
                {"name": database},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
    finally:
        await engine.dispose()


async def _seed_deleted_file(seed) -> uuid.UUID:
    file_id = uuid.uuid4()
    thread_id = f"restore-{uuid.uuid4()}"
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
                logical_path="restore-secret.txt",
                media_type="text/plain",
                size=6,
                sha256="0" * 64,
                status="deleted",
                deleted_at=EXPIRED,
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


async def _archive_then_purge(source_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_JWT_SECRET", "task17-distinct-auth-secret")
    postgres_bin = "/opt/homebrew/Cellar/postgresql@14/14.19/bin"
    monkeypatch.setenv("PATH", f"{postgres_bin}:{os.environ.get('PATH', '')}")
    # A valid M1-M6 source includes the LangGraph-owned checkpoint and Store
    # schemas in the same database; ORM bootstrap intentionally does not own
    # these tables.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    psycopg_url = str(source_url).replace("postgresql+asyncpg://", "postgresql://", 1)
    async with AsyncPostgresSaver.from_conn_string(psycopg_url) as saver:
        await saver.setup()
    async with AsyncPostgresStore.from_conn_string(psycopg_url) as store:
        await store.setup()
    seed = await seed_m4_thread_database(source_url)
    file_id = await _seed_deleted_file(seed)
    archive = tmp_path / "archive.dfba"
    manifest = await create_backup(BackupConfig(database_url=source_url, output=archive, key=BACKUP_KEY))
    journal = TombstoneJournal(tmp_path / "journal" / "tombstones.jsonl", JOURNAL_KEY)
    purger = RetentionPurger(
        seed.factory,
        journal=journal,
        keyring=_keyring(),
        audit=_recovery_audit(seed.factory),
    )
    await purger.purge(
        RetentionCandidate.file(
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            file_id=file_id,
            deleted_at=EXPIRED,
            idempotency_key=f"restore-file:{file_id}",
            request_id="task17-restore-source-purge",
        ),
        now=NOW,
    )
    return seed, file_id, archive, manifest, journal


def _unit_archive(tmp_path: Path, name: str = "unit.dfba") -> Path:
    archive = tmp_path / name
    with BackupArchiveWriter.atomic(
        archive,
        BACKUP_KEY,
        source_installation_id="1" * 64,
        schema_revision="0015_project_reliability_finalize",
        pg_dump_version="pg_dump (PostgreSQL) 14.19",
        table_count=41,
    ) as writer:
        writer.write_chunk(b"PGDMPunit")
        writer.finalize(database_high_watermark=0, tombstone_journal_sequence=0)
    return archive


@pytest.mark.postgres
@pytest.mark.anyio
async def test_live_archive_restore_replays_tombstone_runs_probes_and_writes_proof(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, file_id, archive, manifest, journal = await _archive_then_purge(
        migrated_postgres_database_url,
        tmp_path,
        monkeypatch,
    )
    target_url = _restore_url(migrated_postgres_database_url)
    original_database_url = os.environ.get("DATABASE_URL")
    try:
        result = await Restorer(
            RestoreConfig(
                archive=archive,
                target_database_url=target_url,
                current_database_url=migrated_postgres_database_url,
                journal=journal,
                backup_key=BACKUP_KEY,
                keyring=_keyring(),
            )
        ).restore()

        assert result.archive_id == manifest.archive_id
        assert result.schema_revision == manifest.schema_revision
        assert result.tombstones_replayed == 1
        assert result.probes_complete is True
        assert result.status == "verified"
        assert os.environ.get("DATABASE_URL") == original_database_url
        target_engine = create_async_engine(target_url)
        try:
            target_factory = async_sessionmaker(target_engine, expire_on_commit=False)
            async with target_factory() as session:
                assert await session.scalar(select(PrivateFileRow.id).where(PrivateFileRow.id == file_id)) is None
                tombstone = await session.get(DeletionTombstoneRow, 1)
                assert tombstone is not None and tombstone.purge_status == "purged"
                proof = await session.get(RestoreProofRow, result.proof_id)
                assert proof is not None and proof.probes_complete is True
                assert proof.replayed_through_sequence == 1
                assert str(file_id) not in repr(proof.__dict__)
        finally:
            await target_engine.dispose()

        # Replaying the same authenticated suffix is an idempotent no-op.
        assert await replay_tombstones(target_url, journal, archive_high_watermark=0) == 0
    finally:
        await seed.engine.dispose()
        await _drop_restore_database(migrated_postgres_database_url, target_url)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_restore_rejects_current_existing_nonempty_and_non_restore_targets(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    archive = _unit_archive(tmp_path)
    journal = TombstoneJournal(tmp_path / "journal" / "tombstones.jsonl", JOURNAL_KEY)
    journal.snapshot()
    base = dict(
        archive=archive,
        current_database_url=migrated_postgres_database_url,
        journal=journal,
        backup_key=BACKUP_KEY,
        keyring=_keyring(),
    )
    with pytest.raises(RestoreTargetRejected):
        await Restorer(RestoreConfig(target_database_url=migrated_postgres_database_url, **base)).restore()
    with pytest.raises(RestoreTargetRejected):
        await Restorer(RestoreConfig(target_database_url=replace_database(migrated_postgres_database_url, "ordinary_name"), **base)).restore()

    target_url = _restore_url(migrated_postgres_database_url)
    from sqlalchemy.engine import make_url

    database = make_url(target_url).database or ""
    admin = create_async_engine(replace_database(migrated_postgres_database_url, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database}"'))
        target = create_async_engine(target_url)
        try:
            async with target.begin() as connection:
                await connection.execute(text("CREATE TABLE must_not_overwrite (id integer primary key)"))
        finally:
            await target.dispose()
        with pytest.raises(RestoreTargetRejected):
            await Restorer(RestoreConfig(target_database_url=target_url, **base)).restore()
    finally:
        await admin.dispose()
        await _drop_restore_database(migrated_postgres_database_url, target_url)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_restore_wrong_key_tamper_and_journal_gap_fail_before_target_creation(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    archive = _unit_archive(tmp_path)
    journal_path = tmp_path / "journal" / "tombstones.jsonl"
    journal = TombstoneJournal(journal_path, JOURNAL_KEY)
    journal.append_and_fsync(
        restore_module.TombstoneRecord(
            resource_kind="file",
            project_id="11111111-1111-1111-1111-111111111111",
            owner_user_id="22222222-2222-2222-2222-222222222222",
            file_id="33333333-3333-3333-3333-333333333333",
            project_ids=(),
            idempotency_key="gap-1",
        ),
        committed_sequence=0,
    )
    target_url = _restore_url(migrated_postgres_database_url)
    from sqlalchemy.engine import make_url

    target_name = make_url(target_url).database or ""
    base = dict(
        archive=archive,
        target_database_url=target_url,
        current_database_url=migrated_postgres_database_url,
        journal=journal,
        keyring=_keyring(),
    )
    with pytest.raises(RestoreAuthenticationFailed):
        await Restorer(RestoreConfig(backup_key=b"x" * 32, **base)).restore()
    assert not await _database_exists(migrated_postgres_database_url, target_name)

    chunk_path = archive / "chunks" / "00000000.bin"
    tampered = bytearray(chunk_path.read_bytes())
    tampered[0] ^= 1
    chunk_path.write_bytes(tampered)
    with pytest.raises(RestoreAuthenticationFailed):
        await Restorer(RestoreConfig(backup_key=BACKUP_KEY, **base)).restore()
    assert not await _database_exists(migrated_postgres_database_url, target_name)

    base["archive"] = _unit_archive(tmp_path, "fresh-unit.dfba")

    lines = journal_path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["sequence"] = 2
    lines[1] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(TombstoneSequenceGap):
        await Restorer(RestoreConfig(backup_key=BACKUP_KEY, **base)).restore()
    assert not await _database_exists(migrated_postgres_database_url, target_name)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_probe_failure_is_fail_closed_and_does_not_leave_target_or_proof(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, _file_id, archive, _manifest, journal = await _archive_then_purge(
        migrated_postgres_database_url,
        tmp_path,
        monkeypatch,
    )
    target_url = _restore_url(migrated_postgres_database_url)
    from sqlalchemy.engine import make_url

    target_name = make_url(target_url).database or ""

    async def fail_probe(*_args, **_kwargs):
        raise RecoveryProbeFailed()

    monkeypatch.setattr(restore_module, "_run_recovery_probes", fail_probe)
    try:
        with pytest.raises(RecoveryProbeFailed):
            await Restorer(
                RestoreConfig(
                    archive=archive,
                    target_database_url=target_url,
                    current_database_url=migrated_postgres_database_url,
                    journal=journal,
                    backup_key=BACKUP_KEY,
                    keyring=_keyring(),
                )
            ).restore()
        assert not await _database_exists(migrated_postgres_database_url, target_name)
    finally:
        await seed.engine.dispose()
        if await _database_exists(migrated_postgres_database_url, target_name):
            await _drop_restore_database(migrated_postgres_database_url, target_url)


@pytest.mark.anyio
async def test_drill_cleans_up_only_the_random_restore_database_it_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _unit_archive(tmp_path)
    journal = TombstoneJournal(tmp_path / "journal" / "tombstones.jsonl", JOURNAL_KEY)
    journal.snapshot()
    created: list[str] = []
    dropped: list[str] = []

    async def fake_restore(self):
        created.append(self.config.target_database_url)
        raise RecoveryProbeFailed()

    async def fake_drop(current_url: str, target_url: str) -> None:
        assert current_url == "postgresql://operator@localhost/postgres"
        dropped.append(target_url)

    monkeypatch.setattr(Restorer, "restore", fake_restore)
    monkeypatch.setattr(restore_module, "_drop_created_database", fake_drop)
    with pytest.raises(RecoveryProbeFailed):
        await drill_restore(
            current_database_url="postgresql://operator@localhost/postgres",
            archive=archive,
            journal=journal,
            backup_key=BACKUP_KEY,
            keyring=_keyring(),
        )

    assert dropped == created
    assert len(created) == 1
    assert _RESTORE_NAME.fullmatch(restore_module.database_name(created[0]))


def test_restore_result_and_cli_contract_are_public_safe() -> None:
    from scripts.restore_postgres import public_restore_result

    payload = public_restore_result(
        restore_module.RestoreResult(
            proof_id=uuid.uuid4(),
            archive_id=str(uuid.uuid4()),
            schema_revision="0015_project_reliability_finalize",
            table_count=41,
            tombstones_replayed=2,
            replayed_through_sequence=9,
            probes_complete=True,
            status="verified",
            checksum="a" * 64,
        )
    )
    encoded = json.dumps(payload, sort_keys=True)
    assert set(payload) == {"archive_id", "schema_revision", "table_count", "status", "checksum", "proof"}
    assert payload["checksum"] == "a" * 16
    assert "postgresql://" not in encoded
    assert "journal" not in encoded
    assert "replayed_through" not in encoded
    assert "private" not in encoded


@pytest.mark.anyio
async def test_failed_restore_cleanup_connects_to_the_target_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected_urls: list[str] = []

    class _Connection:
        async def execute(self, *_args, **_kwargs) -> None:
            return None

    class _ConnectionContext:
        async def __aenter__(self) -> _Connection:
            return _Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Engine:
        def connect(self) -> _ConnectionContext:
            return _ConnectionContext()

        async def dispose(self) -> None:
            return None

    def fake_engine(url: str, **_kwargs: object) -> _Engine:
        connected_urls.append(url)
        return _Engine()

    monkeypatch.setattr(restore_module, "create_async_engine", fake_engine)
    await restore_module._drop_created_database(
        "postgresql://operator@source.internal/deerflow_source",
        "postgresql://operator@restore.internal/deerflow_restore_1_0123456789abcdef0123456789abcdef",
    )

    from sqlalchemy.engine import make_url

    assert len(connected_urls) == 1
    assert make_url(connected_urls[0]).host == "restore.internal"


@pytest.mark.anyio
async def test_new_database_is_removed_when_empty_database_verification_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dropped: list[tuple[str, str]] = []
    engine_count = 0

    class _Connection:
        def __init__(self, *, target: bool) -> None:
            self.target = target

        async def scalar(self, *_args, **_kwargs) -> int | bool:
            return 1 if self.target else False

        async def execute(self, *_args, **_kwargs) -> None:
            return None

    class _ConnectionContext:
        def __init__(self, *, target: bool) -> None:
            self.target = target

        async def __aenter__(self) -> _Connection:
            return _Connection(target=self.target)

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Engine:
        def __init__(self, *, target: bool) -> None:
            self.target = target

        def connect(self) -> _ConnectionContext:
            return _ConnectionContext(target=self.target)

        async def dispose(self) -> None:
            return None

    def fake_engine(_url: str, **_kwargs: object) -> _Engine:
        nonlocal engine_count
        engine_count += 1
        return _Engine(target=engine_count == 2)

    async def fake_drop(target_url: str, database: str) -> None:
        dropped.append((target_url, database))

    monkeypatch.setattr(restore_module, "create_async_engine", fake_engine)
    monkeypatch.setattr(
        restore_module,
        "_drop_database_on_target",
        fake_drop,
        raising=False,
    )
    target_url = "postgresql://operator@restore.internal/deerflow_restore_1_0123456789abcdef0123456789abcdef"
    with pytest.raises(RestoreTargetRejected):
        await restore_module._create_empty_database(
            target_url,
            "deerflow_restore_1_0123456789abcdef0123456789abcdef",
        )

    assert dropped == [
        (
            target_url,
            "deerflow_restore_1_0123456789abcdef0123456789abcdef",
        )
    ]


def test_backup_and_journal_keys_cannot_be_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded = base64.b64encode(BACKUP_KEY).decode("ascii")
    monkeypatch.setenv("AUTH_JWT_SECRET", "distinct-auth-secret")
    monkeypatch.setenv("DEER_FLOW_BACKUP_KEY", encoded)
    monkeypatch.setenv("DEER_FLOW_RECOVERY_JOURNAL_KEY", encoded)
    from app.recovery.journal import load_journal_key

    with pytest.raises(Exception, match="JOURNAL"):
        load_journal_key()
