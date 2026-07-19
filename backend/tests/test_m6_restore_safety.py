from __future__ import annotations

import asyncio
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import app.recovery.authority as authority_module
import app.recovery.cleanup as cleanup_module
import app.recovery.restore as restore_module
import app.recovery.restore_process as restore_process_module
from app.recovery import ARCHIVE_SCHEMA_VERSION
from app.recovery.archive import M7_CANONICAL_SCHEMA_DIGEST
from app.recovery.cleanup import (
    OwnedFile,
    OwnedWorkspace,
    SensitiveCleanupFailed,
    _cleanup_owned_workspace,
    _create_owned_file,
    _create_owned_workspace,
    _write_owned_file,
)
from app.recovery.journal import TombstoneJournal
from app.recovery.restore import (
    RestoreCommandFailed,
    RestoreConfig,
    Restorer,
)
from app.reliability.owner_refs import AuditHmacKeyring

BACKUP_KEY = b"b" * 32
JOURNAL_KEY = b"j" * 32
SOURCE_ID = "1" * 64
TARGET_URL = "postgresql://operator@localhost/deerflow_restore_1_0123456789abcdef0123456789abcdef"
SOURCE_URL = "postgresql://operator@localhost/deerflow_source"


@pytest.fixture(autouse=True)
def exact_m7_database(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exact(_database_url: str) -> None:
        return None

    monkeypatch.setattr(restore_module, "_require_exact_m7_database", exact)


def _keyring() -> AuditHmacKeyring:
    return AuditHmacKeyring(
        active_key_id="audit-v1",
        _keys={"audit-v1": b"a" * 32},
    )


def _authenticated(dump: OwnedFile) -> restore_module._AuthenticatedArchive:
    return restore_module._AuthenticatedArchive(
        archive_id="00000000-0000-0000-0000-000000000001",
        archive_schema_version=ARCHIVE_SCHEMA_VERSION,
        schema_revision="0001_project_saas_baseline",
        schema_digest=M7_CANONICAL_SCHEMA_DIGEST,
        source_installation_id=SOURCE_ID,
        tombstone_journal_sequence=0,
        table_count=1,
        archive_digest="a" * 64,
        dump_path=dump.path,
        dump_identity=dump.identity,
    )


class _ScalarResult:
    def __init__(self, value: bool = True) -> None:
        self.value = value

    def scalar_one(self) -> bool:
        return self.value


class _ConnectionContext:
    def __init__(self, connection: object) -> None:
        self.connection = connection

    async def __aenter__(self) -> object:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Engine:
    def __init__(self, connection: object) -> None:
        self.connection = connection

    def connect(self) -> _ConnectionContext:
        return _ConnectionContext(self.connection)

    async def dispose(self) -> None:
        return None


@pytest.mark.anyio
async def test_source_authority_explicitly_unlocks_when_anchor_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Connection:
        async def execute(self, statement: object, *_args: object, **_kwargs: object):
            sql = str(statement)
            if "pg_advisory_unlock" in sql:
                calls.append("unlock")
            elif "pg_advisory_lock" in sql:
                calls.append("lock")
            return _ScalarResult()

    async def fail_anchor(*_args: object, **_kwargs: object):
        calls.append("verify")
        raise RuntimeError("anchor failed")

    monkeypatch.setattr(
        authority_module,
        "create_async_engine",
        lambda *_args, **_kwargs: _Engine(_Connection()),
    )
    monkeypatch.setattr(
        authority_module,
        "_verify_source_recovery_anchor",
        fail_anchor,
    )
    journal = TombstoneJournal(
        tmp_path / "journal.jsonl",
        JOURNAL_KEY,
        source_installation_id=SOURCE_ID,
    )

    with pytest.raises(RuntimeError, match="anchor failed"):
        async with authority_module.source_recovery_authority(
            SOURCE_URL,
            journal=journal,
            expected_source_installation_id=SOURCE_ID,
            archive_tombstone_sequence=0,
        ):
            raise AssertionError("unreachable")

    assert calls == ["lock", "verify", "unlock"]


@pytest.mark.anyio
async def test_source_authority_unlock_cancellation_is_rethrown_after_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlock_started = asyncio.Event()
    allow_unlock = asyncio.Event()
    unlock_calls = 0

    class _Connection:
        async def execute(self, statement: object, *_args: object, **_kwargs: object):
            nonlocal unlock_calls
            sql = str(statement)
            if "pg_advisory_unlock" in sql:
                unlock_calls += 1
                unlock_started.set()
                await allow_unlock.wait()
            return _ScalarResult()

    async def verified_anchor(*_args: object, **_kwargs: object):
        return object()

    monkeypatch.setattr(
        authority_module,
        "create_async_engine",
        lambda *_args, **_kwargs: _Engine(_Connection()),
    )
    monkeypatch.setattr(
        authority_module,
        "_verify_source_recovery_anchor",
        verified_anchor,
    )
    journal = TombstoneJournal(
        tmp_path / "journal.jsonl",
        JOURNAL_KEY,
        source_installation_id=SOURCE_ID,
    )

    async def restore_boundary() -> str:
        async with authority_module.source_recovery_authority(
            SOURCE_URL,
            journal=journal,
            expected_source_installation_id=SOURCE_ID,
            archive_tombstone_sequence=0,
        ):
            pass
        return "verified"

    task = asyncio.create_task(restore_boundary())
    await asyncio.wait_for(unlock_started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_unlock.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert unlock_calls == 1


def test_workspace_cleanup_refuses_unknown_files_without_deleting_them(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    known = workspace / "known.dump"
    attacker = workspace / "attacker.file"
    known.write_bytes(b"secret")
    attacker.write_bytes(b"do-not-own")
    workspace_info = workspace.stat(follow_symlinks=False)

    with pytest.raises(SensitiveCleanupFailed):
        _cleanup_owned_workspace(
            workspace,
            (workspace_info.st_dev, workspace_info.st_ino),
        )

    assert known.read_bytes() == b"secret"
    assert attacker.read_bytes() == b"do-not-own"


def test_workspace_cleanup_retry_accepts_owned_file_already_unlinked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _create_owned_workspace(prefix="deerflow-cleanup-retry-")
    owned = _create_owned_file(
        workspace.path,
        prefix="dump-",
        suffix=".bin",
    )
    workspace.register(owned)
    expected = dict(workspace.files)
    real_fsync = cleanup_module.os.fsync
    failures = 2

    def fail_directory_fsync_twice(descriptor: int) -> None:
        nonlocal failures
        if failures:
            failures -= 1
            raise OSError("transient directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(cleanup_module.os, "fsync", fail_directory_fsync_twice)
    with pytest.raises(SensitiveCleanupFailed):
        _cleanup_owned_workspace(
            workspace.path,
            workspace.identity,
            expected,
        )

    assert not owned.path.exists()
    assert workspace.path.exists()
    _cleanup_owned_workspace(
        workspace.path,
        workspace.identity,
        expected,
    )
    assert not workspace.path.exists()


def test_workspace_cleanup_retry_deletes_only_remaining_owned_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _create_owned_workspace(prefix="deerflow-cleanup-retry-many-")
    dump = _create_owned_file(
        workspace.path,
        prefix="00-dump-",
        suffix=".bin",
    )
    passfile = _create_owned_file(
        workspace.path,
        prefix="01-passfile-",
    )
    workspace.register(dump)
    workspace.register(passfile)
    expected = dict(workspace.files)
    fail_passfile = True
    real_listdir = cleanup_module.os.listdir
    real_unlink = cleanup_module.os.unlink

    def sorted_listdir(descriptor: int) -> list[str]:
        return sorted(real_listdir(descriptor))

    def fail_second_unlink(
        name: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if fail_passfile and name == passfile.path.name:
            raise OSError("transient passfile unlink failure")
        real_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(cleanup_module.os, "listdir", sorted_listdir)
    monkeypatch.setattr(cleanup_module.os, "unlink", fail_second_unlink)
    with pytest.raises(SensitiveCleanupFailed):
        _cleanup_owned_workspace(
            workspace.path,
            workspace.identity,
            expected,
        )

    assert not dump.path.exists()
    assert passfile.path.exists()
    fail_passfile = False
    _cleanup_owned_workspace(
        workspace.path,
        workspace.identity,
        expected,
    )
    assert not workspace.path.exists()


@pytest.mark.anyio
async def test_sensitive_blocking_producer_settles_before_cancellation_returns() -> None:
    from app.recovery.cleanup import _settle_blocking_result

    started = threading.Event()
    release = threading.Event()

    def producer() -> str:
        started.set()
        assert release.wait(timeout=2)
        return "owned"

    task = asyncio.create_task(_settle_blocking_result(producer))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()

    result, cancelled = await task
    assert result == "owned"
    assert cancelled is True


@pytest.mark.anyio
async def test_workspace_creation_cancellation_cleans_captured_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    captured: list[Path] = []
    real_create = restore_module._create_owned_workspace
    archive = tmp_path / "archive"
    archive.mkdir()
    journal = TombstoneJournal(
        tmp_path / "journal.jsonl",
        JOURNAL_KEY,
        source_installation_id=SOURCE_ID,
    )

    def delayed_create(*, prefix: str):
        workspace = real_create(prefix=prefix)
        captured.append(workspace.path)
        started.set()
        assert release.wait(timeout=2)
        return workspace

    async def database_missing(*_args: object) -> bool:
        return False

    monkeypatch.setattr(
        restore_module,
        "_create_owned_workspace",
        delayed_create,
    )
    monkeypatch.setattr(restore_module, "_database_exists", database_missing)
    task = asyncio.create_task(
        Restorer(
            RestoreConfig(
                archive=archive,
                target_database_url=TARGET_URL,
                current_database_url=SOURCE_URL,
                journal=journal,
                backup_key=BACKUP_KEY,
                keyring=_keyring(),
            )
        ).restore()
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(captured) == 1
    assert not captured[0].exists()


@pytest.mark.anyio
async def test_archive_authentication_cancellation_settles_before_exact_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    captured: list[OwnedFile] = []
    archive = tmp_path / "archive"
    archive.mkdir()
    journal = TombstoneJournal(
        tmp_path / "journal.jsonl",
        JOURNAL_KEY,
        source_installation_id=SOURCE_ID,
    )

    def delayed_authenticate(
        _archive: Path,
        _key: bytes,
        dump: OwnedFile,
    ) -> restore_module._AuthenticatedArchive:
        _write_owned_file(dump, b"authenticated plaintext")
        captured.append(dump)
        started.set()
        assert release.wait(timeout=2)
        return _authenticated(dump)

    async def database_missing(*_args: object) -> bool:
        return False

    monkeypatch.setattr(
        restore_module,
        "_authenticate_archive",
        delayed_authenticate,
    )
    monkeypatch.setattr(restore_module, "_database_exists", database_missing)
    task = asyncio.create_task(
        Restorer(
            RestoreConfig(
                archive=archive,
                target_database_url=TARGET_URL,
                current_database_url=SOURCE_URL,
                journal=journal,
                backup_key=BACKUP_KEY,
                keyring=_keyring(),
            )
        ).restore()
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(captured) == 1
    assert not captured[0].path.exists()
    assert not captured[0].path.parent.exists()


@pytest.mark.anyio
async def test_passfile_creation_cancellation_settles_before_exact_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await asyncio.to_thread(
        _create_owned_workspace,
        prefix="deerflow-passfile-test-",
    )
    started = threading.Event()
    release = threading.Event()
    captured: list[OwnedFile] = []
    real_create = restore_process_module._create_owned_file

    def delayed_create(*args: object, **kwargs: object) -> OwnedFile:
        owned = real_create(*args, **kwargs)
        captured.append(owned)
        started.set()
        assert release.wait(timeout=2)
        return owned

    monkeypatch.setattr(
        restore_process_module,
        "_create_owned_file",
        delayed_create,
    )
    task = asyncio.create_task(
        restore_process_module.run_pg_restore(
            "postgresql://operator:secret@127.0.0.1/deerflow_restore_1_0123456789abcdef0123456789abcdef",
            workspace.path / "authenticated.dump",
            workspace,
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(captured) == 1
    assert not captured[0].path.exists()
    assert workspace.files == {}
    await asyncio.to_thread(
        _cleanup_owned_workspace,
        workspace.path,
        workspace.identity,
        {},
    )


@pytest.mark.anyio
async def test_restore_body_failure_keeps_authority_until_target_and_workspace_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_lock = asyncio.Lock()
    drop_started = asyncio.Event()
    allow_drop = asyncio.Event()
    observations: list[tuple[str, bool]] = []
    archive = tmp_path / "archive"
    archive.mkdir()
    journal = TombstoneJournal(
        tmp_path / "journal.jsonl",
        JOURNAL_KEY,
        source_installation_id=SOURCE_ID,
    )
    journal.snapshot()

    async def database_missing(*_args: object) -> bool:
        return False

    @asynccontextmanager
    async def source_authority(*_args: object, **_kwargs: object):
        async with authority_lock:
            yield journal.snapshot(require_existing=True)

    async def create(*_args: object) -> None:
        return None

    async def fail_restore(*_args: object, **_kwargs: object) -> None:
        raise RestoreCommandFailed()

    async def drop(*_args: object) -> None:
        observations.append(("drop", authority_lock.locked()))
        drop_started.set()
        await allow_drop.wait()

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    authenticated = restore_module._AuthenticatedArchive(
        archive_id="00000000-0000-0000-0000-000000000001",
        archive_schema_version=ARCHIVE_SCHEMA_VERSION,
        schema_revision="0001_project_saas_baseline",
        schema_digest=M7_CANONICAL_SCHEMA_DIGEST,
        source_installation_id=SOURCE_ID,
        tombstone_journal_sequence=0,
        table_count=1,
        archive_digest="a" * 64,
        dump_path=tmp_path / "placeholder.dump",
        dump_identity=(1, 1),
    )

    monkeypatch.setattr(restore_module, "_database_exists", database_missing)
    monkeypatch.setattr(restore_module, "_source_recovery_authority", source_authority)
    monkeypatch.setattr(restore_module, "_authenticate_archive", lambda *_args: authenticated)
    monkeypatch.setattr(restore_module, "_record_source_restore_started", no_op)
    monkeypatch.setattr(restore_module, "_create_empty_database", create)
    monkeypatch.setattr(restore_module, "_run_pg_restore", fail_restore)
    monkeypatch.setattr(restore_module, "_drop_created_database", drop)

    restore_task = asyncio.create_task(
        Restorer(
            RestoreConfig(
                archive=archive,
                target_database_url=TARGET_URL,
                current_database_url=SOURCE_URL,
                journal=journal,
                backup_key=BACKUP_KEY,
                keyring=_keyring(),
            )
        ).restore()
    )
    await asyncio.wait_for(drop_started.wait(), timeout=2)

    async def concurrent_purge() -> None:
        async with authority_lock:
            observations.append(("purge", True))

    purge_task = asyncio.create_task(concurrent_purge())
    await asyncio.sleep(0)
    assert not purge_task.done()
    allow_drop.set()

    with pytest.raises(RestoreCommandFailed):
        await restore_task
    await purge_task
    assert observations == [("drop", True), ("purge", True)]


@pytest.mark.anyio
async def test_unlock_cancellation_after_proof_drops_owned_target_and_never_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    journal = TombstoneJournal(
        tmp_path / "journal.jsonl",
        JOURNAL_KEY,
        source_installation_id=SOURCE_ID,
    )
    snapshot = journal.snapshot()
    proof_calls = 0
    dropped: list[str] = []

    async def database_missing(*_args: object) -> bool:
        return False

    def authenticate(
        _archive: Path,
        _key: bytes,
        dump: OwnedFile,
    ) -> restore_module._AuthenticatedArchive:
        return _authenticated(dump)

    @asynccontextmanager
    async def cancelled_unlock(*_args: object, **_kwargs: object):
        yield snapshot
        raise asyncio.CancelledError

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    async def no_replay(*_args: object, **_kwargs: object) -> int:
        return 0

    async def proof(*_args: object, **_kwargs: object) -> None:
        nonlocal proof_calls
        proof_calls += 1

    async def drop(_current: str, target: str) -> None:
        dropped.append(target)

    monkeypatch.setattr(restore_module, "_database_exists", database_missing)
    monkeypatch.setattr(restore_module, "_authenticate_archive", authenticate)
    monkeypatch.setattr(restore_module, "_source_recovery_authority", cancelled_unlock)
    monkeypatch.setattr(restore_module, "_record_source_restore_started", no_op)
    monkeypatch.setattr(restore_module, "_create_empty_database", no_op)
    monkeypatch.setattr(restore_module, "_run_pg_restore", no_op)
    monkeypatch.setattr(restore_module, "replay_tombstones", no_replay)
    monkeypatch.setattr(restore_module, "_run_recovery_probes", no_op)
    monkeypatch.setattr(restore_module, "_write_proof_and_completion", proof)
    monkeypatch.setattr(restore_module, "_drop_created_database", drop)
    restorer = Restorer(
        RestoreConfig(
            archive=archive,
            target_database_url=TARGET_URL,
            current_database_url=SOURCE_URL,
            journal=journal,
            backup_key=BACKUP_KEY,
            keyring=_keyring(),
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await restorer.restore()

    assert proof_calls == 1
    assert dropped == [TARGET_URL]
    assert restorer._verified_result is None


@pytest.mark.anyio
async def test_unknown_workspace_file_blocks_proof_and_is_never_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    journal = TombstoneJournal(
        tmp_path / "journal.jsonl",
        JOURNAL_KEY,
        source_installation_id=SOURCE_ID,
    )
    snapshot = journal.snapshot()
    attacker_paths: list[Path] = []
    proof_calls = 0
    dropped: list[str] = []

    async def database_missing(*_args: object) -> bool:
        return False

    def authenticate(
        _archive: Path,
        _key: bytes,
        dump: OwnedFile,
    ) -> restore_module._AuthenticatedArchive:
        return _authenticated(dump)

    @asynccontextmanager
    async def source_authority(*_args: object, **_kwargs: object):
        yield snapshot

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    async def inject_unknown(
        _target: str,
        _dump: Path,
        workspace: OwnedWorkspace,
    ) -> None:
        attacker = workspace.path / "attacker.file"
        await asyncio.to_thread(attacker.write_bytes, b"not invocation owned")
        attacker_paths.append(attacker)

    async def proof(*_args: object, **_kwargs: object) -> None:
        nonlocal proof_calls
        proof_calls += 1

    async def drop(_current: str, target: str) -> None:
        dropped.append(target)

    monkeypatch.setattr(restore_module, "_database_exists", database_missing)
    monkeypatch.setattr(restore_module, "_authenticate_archive", authenticate)
    monkeypatch.setattr(restore_module, "_source_recovery_authority", source_authority)
    monkeypatch.setattr(restore_module, "_record_source_restore_started", no_op)
    monkeypatch.setattr(restore_module, "_create_empty_database", no_op)
    monkeypatch.setattr(restore_module, "_run_pg_restore", inject_unknown)
    monkeypatch.setattr(restore_module, "_write_proof_and_completion", proof)
    monkeypatch.setattr(restore_module, "_drop_created_database", drop)

    with pytest.raises(RestoreCommandFailed):
        await Restorer(
            RestoreConfig(
                archive=archive,
                target_database_url=TARGET_URL,
                current_database_url=SOURCE_URL,
                journal=journal,
                backup_key=BACKUP_KEY,
                keyring=_keyring(),
            )
        ).restore()

    assert proof_calls == 0
    assert dropped == [TARGET_URL]
    assert len(attacker_paths) == 1
    assert attacker_paths[0].read_bytes() == b"not invocation owned"
    await asyncio.to_thread(
        shutil.rmtree,
        attacker_paths[0].parent,
    )


@pytest.mark.anyio
async def test_drill_cancellation_waits_for_owned_target_drop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    journal = TombstoneJournal(
        tmp_path / "journal.jsonl",
        JOURNAL_KEY,
        source_installation_id=SOURCE_ID,
    )
    drop_started = asyncio.Event()
    allow_drop = asyncio.Event()

    async def verified_restore(self: Restorer) -> restore_module.RestoreResult:
        result = restore_module.RestoreResult(
            proof_id=uuid.uuid4(),
            archive_id=str(uuid.uuid4()),
            archive_schema_version=ARCHIVE_SCHEMA_VERSION,
            schema_revision="0001_project_saas_baseline",
            schema_digest=M7_CANONICAL_SCHEMA_DIGEST,
            table_count=1,
            tombstones_replayed=0,
            replayed_through_sequence=0,
            probes_complete=True,
            status="verified",
            checksum="a" * 64,
            _handoff_token=self._handoff_token,
        )
        self._verified_result = result
        return result

    async def no_audit(*_args: object, **_kwargs: object) -> None:
        return None

    async def slow_drop(*_args: object, **_kwargs: object) -> None:
        drop_started.set()
        await allow_drop.wait()

    monkeypatch.setattr(Restorer, "restore", verified_restore)
    monkeypatch.setattr(restore_module, "_record_drill_completion", no_audit)
    monkeypatch.setattr(restore_module, "_drop_created_database", slow_drop)
    task = asyncio.create_task(
        restore_module.drill_restore(
            current_database_url=SOURCE_URL,
            archive=archive,
            journal=journal,
            backup_key=BACKUP_KEY,
            keyring=_keyring(),
        )
    )
    await asyncio.wait_for(drop_started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_drop.set()

    with pytest.raises(asyncio.CancelledError):
        await task
