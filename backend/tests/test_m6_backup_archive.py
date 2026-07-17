from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import os
import stat
import sys
import threading
import types
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import app.recovery.archive as archive_module
from app.recovery.archive import (
    CHUNK_SIZE,
    BackupArchiveReader,
    BackupArchiveWriter,
    BackupAuthenticationFailed,
    BackupCommandFailed,
    BackupConfig,
    BackupKeyInvalid,
    BackupKeyMissing,
    BackupSnapshot,
    create_backup,
    load_backup_key,
    pg_dump_argv,
)

_SOURCE_ID = hashlib.sha256(b"test-source").hexdigest()
_ORIGINAL_EXPORTED_SNAPSHOT = archive_module._exported_snapshot


@pytest.fixture
def backup_key() -> bytes:
    return bytes(range(32))


@pytest.fixture(autouse=True)
def backup_runtime_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_JWT_SECRET", "unit-test-auth-secret-that-is-distinct")

    @asynccontextmanager
    async def fake_snapshot(_database_url: str):
        yield BackupSnapshot(
            snapshot_id="00000003-0000001B-1",
            schema_revision="0015_project_reliability_finalize",
            source_installation_id=_SOURCE_ID,
            database_high_watermark=12,
            tombstone_journal_sequence=4,
            table_count=41,
        )

    async def fake_version() -> str:
        return "pg_dump (PostgreSQL) 16.4"

    async def fake_audit(_database_url: str, _manifest: object) -> None:
        return None

    monkeypatch.setattr(archive_module, "_exported_snapshot", fake_snapshot)
    monkeypatch.setattr(archive_module, "_read_pg_dump_version", fake_version)
    monkeypatch.setattr(archive_module, "_record_backup_audit", fake_audit)


def make_archive(tmp_path: Path, backup_key: bytes, payload: bytes):
    tmp_path.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "archive.dfba"
    with BackupArchiveWriter.atomic(output, backup_key, chunk_bytes=CHUNK_SIZE, source_installation_id=_SOURCE_ID) as writer:
        for offset in range(0, len(payload), CHUNK_SIZE):
            writer.write_chunk(payload[offset : offset + CHUNK_SIZE])
        manifest = writer.finalize(database_high_watermark=12, tombstone_journal_sequence=4)
    return output, manifest


def test_tampered_chunk_fails_before_plaintext_release(tmp_path: Path, backup_key: bytes) -> None:
    archive, _ = make_archive(tmp_path, backup_key, b"PGDMP" + b"a" * CHUNK_SIZE)
    chunk = archive / "chunks" / "00000001.bin"
    ciphertext = bytearray(chunk.read_bytes())
    ciphertext[0] ^= 1
    chunk.write_bytes(ciphertext)

    with pytest.raises(BackupAuthenticationFailed):
        list(BackupArchiveReader(backup_key).verified_chunks(archive))


def test_each_chunk_uses_unique_nonce(tmp_path: Path, backup_key: bytes) -> None:
    _, manifest = make_archive(tmp_path, backup_key, b"PGDMP" + b"x" * (CHUNK_SIZE * 3 - 5))
    assert len({chunk.nonce for chunk in manifest.chunks}) == 3


def test_manifest_tampering_and_wrong_key_fail_closed(tmp_path: Path, backup_key: bytes) -> None:
    archive, _ = make_archive(tmp_path, backup_key, b"PGDMPdatabase bytes")
    manifest_path = archive / "manifest.json"
    tampered = bytearray(manifest_path.read_bytes())
    tampered[-2] ^= 1
    manifest_path.write_bytes(tampered)
    with pytest.raises(BackupAuthenticationFailed):
        list(BackupArchiveReader(backup_key).verified_chunks(archive))

    archive, _ = make_archive(tmp_path / "wrong-key", backup_key, b"PGDMPdatabase bytes")
    with pytest.raises(BackupAuthenticationFailed):
        list(BackupArchiveReader(b"z" * 32).verified_chunks(archive))


def test_backup_key_must_be_distinct_32_byte_secret(tmp_path: Path) -> None:
    with pytest.raises(BackupKeyInvalid):
        BackupArchiveWriter.atomic(tmp_path / "archive", b"database-password", source_installation_id=_SOURCE_ID)
    with pytest.raises(BackupKeyInvalid):
        BackupArchiveReader(b"credential-keyring")


def test_backup_key_reuse_of_known_deployment_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    reused = bytes(range(32))
    encoded = base64.b64encode(reused).decode("ascii")
    monkeypatch.setenv("DEER_FLOW_AUDIT_ACTIVE_KEY_ID", "audit-v1")
    monkeypatch.setenv("DEER_FLOW_AUDIT_KEYRING_JSON", json.dumps({"audit-v1": encoded}))

    with pytest.raises(BackupKeyInvalid):
        load_backup_key(encoded)


def test_backup_key_reuse_of_persisted_auth_secret_is_rejected_without_rotating_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reused = bytes(range(32))
    encoded = base64.b64encode(reused).decode("ascii")
    home = tmp_path / "deer-flow-home"
    home.mkdir()
    secret_file = home / ".jwt_secret"
    secret_file.write_text(encoded, encoding="utf-8")
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    monkeypatch.setenv("DEER_FLOW_HOME", str(home))

    with pytest.raises(BackupKeyInvalid):
        load_backup_key(encoded)
    assert secret_file.read_text(encoding="utf-8") == encoded


def test_backup_key_validation_never_creates_missing_auth_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    encoded = base64.b64encode(bytes(range(32))).decode("ascii")
    home = tmp_path / "deer-flow-home"
    home.mkdir()
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    monkeypatch.setenv("DEER_FLOW_HOME", str(home))

    with pytest.raises(BackupKeyInvalid):
        load_backup_key(encoded)
    assert not (home / ".jwt_secret").exists()


def test_backup_key_validation_fails_closed_on_unsafe_auth_secret_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    encoded = base64.b64encode(bytes(range(32))).decode("ascii")
    home = tmp_path / "deer-flow-home"
    home.mkdir()
    target = tmp_path / "elsewhere-secret"
    target.write_text(encoded, encoding="utf-8")
    (home / ".jwt_secret").symlink_to(target)
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    monkeypatch.setenv("DEER_FLOW_HOME", str(home))

    with pytest.raises(BackupKeyInvalid):
        load_backup_key(encoded)


def test_explicit_auth_secret_takes_precedence_over_persisted_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_key = bytes(range(32))
    encoded = base64.b64encode(backup_key).decode("ascii")
    home = tmp_path / "deer-flow-home"
    home.mkdir()
    (home / ".jwt_secret").write_text(encoded, encoding="utf-8")
    monkeypatch.setenv("DEER_FLOW_HOME", str(home))
    monkeypatch.setenv("AUTH_JWT_SECRET", "explicit-distinct-auth-secret")

    assert load_backup_key(encoded) == backup_key


def test_missing_backup_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEER_FLOW_BACKUP_KEY", raising=False)
    with pytest.raises(BackupKeyMissing):
        load_backup_key()


def test_archive_permissions_are_operator_only(tmp_path: Path, backup_key: bytes) -> None:
    archive, _ = make_archive(tmp_path, backup_key, b"PGDMPdatabase bytes")
    assert stat.S_IMODE(archive.stat().st_mode) == 0o700
    assert stat.S_IMODE((archive / "manifest.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((archive / "chunks" / "00000000.bin").stat().st_mode) == 0o600


def test_pg_dump_argv_is_fixed_and_never_shell_interpolated() -> None:
    database_url = "postgresql://user:password@db/test; rm -rf /"
    argv = pg_dump_argv(database_url)
    assert argv == ("pg_dump", "--format=custom", "--no-owner", "--no-acl")
    assert all("sh" not in argument for argument in argv)


def test_pg_dump_argv_excludes_database_url_and_password() -> None:
    database_url = "postgresql://user:password@db.example/test"

    argv = pg_dump_argv(database_url)

    assert database_url not in argv
    assert all("password" not in argument for argument in argv)


def test_empty_and_non_custom_pg_dump_archives_are_rejected(tmp_path: Path, backup_key: bytes) -> None:
    empty_output = tmp_path / "empty.dfba"
    with pytest.raises(ValueError):
        with BackupArchiveWriter.atomic(empty_output, backup_key, source_installation_id=_SOURCE_ID) as writer:
            writer.finalize(database_high_watermark=0, tombstone_journal_sequence=0)
    assert not empty_output.exists()

    invalid_output = tmp_path / "invalid.dfba"
    with pytest.raises(ValueError):
        with BackupArchiveWriter.atomic(invalid_output, backup_key, source_installation_id=_SOURCE_ID) as writer:
            writer.write_chunk(b"not-a-pgdump")
            writer.finalize(database_high_watermark=0, tombstone_journal_sequence=0)
    assert not invalid_output.exists()


def test_manifest_records_nonempty_plaintext_and_ciphertext_totals(tmp_path: Path, backup_key: bytes) -> None:
    _, manifest = make_archive(tmp_path, backup_key, b"PGDMP" + b"x" * 17)

    assert manifest.total_plaintext_bytes == 22
    assert manifest.total_ciphertext_bytes > manifest.total_plaintext_bytes


def test_archive_size_contract_is_explicit_and_validated(backup_key: bytes, tmp_path: Path) -> None:
    import app.recovery as recovery_facade

    assert archive_module.MAX_MANIFEST_BYTES == 16 * 1024 * 1024
    assert archive_module.MAX_ARCHIVE_CHUNKS == 65_536
    assert archive_module.MAX_ARCHIVE_PLAINTEXT_BYTES == CHUNK_SIZE * archive_module.MAX_ARCHIVE_CHUNKS
    assert recovery_facade.MAX_ARCHIVE_PLAINTEXT_BYTES == archive_module.MAX_ARCHIVE_PLAINTEXT_BYTES
    with pytest.raises(ValueError, match="chunk_bytes"):
        BackupConfig(database_url="postgresql://db/test", output=tmp_path / "too-large.dfba", key=backup_key, chunk_bytes=CHUNK_SIZE + 1)


def test_writer_rejects_manifest_the_reader_cannot_accept(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "oversized-manifest.dfba"
    monkeypatch.setattr(archive_module, "MAX_MANIFEST_BYTES", 128, raising=False)
    with pytest.raises(ValueError, match="manifest"):
        with BackupArchiveWriter.atomic(output, backup_key, source_installation_id=_SOURCE_ID) as writer:
            writer.write_chunk(b"PGDMPpayload")
            writer.finalize(database_high_watermark=0, tombstone_journal_sequence=0)
    assert not output.exists()


def test_reader_uses_the_same_manifest_limit_as_writer(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    archive, _ = make_archive(tmp_path, backup_key, b"PGDMPpayload")
    manifest_size = (archive / "manifest.json").stat().st_size
    monkeypatch.setattr(archive_module, "MAX_MANIFEST_BYTES", manifest_size - 1, raising=False)
    with pytest.raises(BackupAuthenticationFailed):
        list(BackupArchiveReader(backup_key).verified_chunks(archive))


def test_parent_fsync_failure_removes_published_archive(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.recovery.archive as archive_module

    output = tmp_path / "fsync-failed.dfba"
    monkeypatch.setattr(archive_module, "_fsync_directory", lambda _path: (_ for _ in ()).throw(OSError("fsync failed")))

    with pytest.raises(OSError):
        with BackupArchiveWriter.atomic(output, backup_key, source_installation_id=_SOURCE_ID) as writer:
            writer.write_chunk(b"PGDMPpayload")
            writer.finalize(database_high_watermark=0, tombstone_journal_sequence=0)
    assert not output.exists()


def test_publication_never_clobbers_output_created_after_staging(tmp_path: Path, backup_key: bytes) -> None:
    output = tmp_path / "raced.dfba"
    writer = BackupArchiveWriter.atomic(output, backup_key, source_installation_id=_SOURCE_ID)
    writer.__enter__()
    try:
        writer.write_chunk(b"PGDMPpayload")
        output.mkdir()
        with pytest.raises(FileExistsError):
            writer.finalize(database_high_watermark=0, tombstone_journal_sequence=0)
        assert output.is_dir()
        assert not (output / "manifest.json").exists()
    finally:
        writer.abort()


def test_external_archive_parent_symlink_is_rejected(tmp_path: Path, backup_key: bytes) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(OSError):
        with BackupArchiveWriter.atomic(linked / "archive.dfba", backup_key, source_installation_id=_SOURCE_ID):
            pass


def test_per_archive_salt_prevents_key_nonce_reuse(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.recovery.archive as archive_module

    monkeypatch.setattr(archive_module.secrets, "token_bytes", lambda size: b"\x00" * size)
    outputs = (tmp_path / "first.dfba", tmp_path / "second.dfba")
    archive_ids = ("00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002")
    for output, archive_id in zip(outputs, archive_ids, strict=True):
        with BackupArchiveWriter.atomic(
            output,
            backup_key,
            source_installation_id=_SOURCE_ID,
            archive_id=archive_id,
        ) as writer:
            writer.write_chunk(b"PGDMPpayload")
            writer.finalize(database_high_watermark=0, tombstone_journal_sequence=0)

    first = (outputs[0] / "chunks" / "00000000.bin").read_bytes()
    second = (outputs[1] / "chunks" / "00000000.bin").read_bytes()
    assert first[:-16] != second[:-16]


def test_reader_does_not_load_ciphertext_chunks_with_read_bytes(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    archive, _ = make_archive(tmp_path, backup_key, b"PGDMP" + b"x" * (CHUNK_SIZE * 2))
    original = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path.suffix == ".bin":
            raise AssertionError("ciphertext chunks must be streamed")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    assert b"".join(BackupArchiveReader(backup_key).verified_chunks(archive)).startswith(b"PGDMP")


class _FakeStdout:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    async def read(self, _size: int) -> bytes:
        return next(self._chunks, b"")


class _FakeStderr:
    def __init__(self, value: bytes) -> None:
        self._value = value

    async def read(self, _size: int = -1) -> bytes:
        value, self._value = self._value, b""
        return value


class _FakeProcess:
    def __init__(self, returncode: int, chunks: list[bytes], stderr: bytes = b"password=leaked") -> None:
        self.stdout = _FakeStdout(chunks)
        self._returncode = returncode
        self._stderr = stderr
        self.stderr = _FakeStderr(stderr)

    async def wait(self) -> int:
        return self._returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"postgresql://user:password@db/test", self._stderr

    def terminate(self) -> None:
        self._returncode = -15


class _TerminableProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__(0, [b"never written"])
        self.returncode: int | None = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


@pytest.mark.asyncio
async def test_pg_dump_failure_does_not_publish_archive_or_expose_command_output(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_subprocess(*argv: str, **kwargs: object) -> _FakeProcess:
        assert argv[0] == "pg_dump"
        assert "shell" not in kwargs
        return _FakeProcess(1, [b"partial"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    config = BackupConfig(
        database_url="postgresql://user:password@db/test",
        output=tmp_path / "failed.dfba",
        key=backup_key,
    )

    with pytest.raises(BackupCommandFailed) as exc_info:
        await create_backup(config)
    assert str(exc_info.value) == "BACKUP_COMMAND_FAILED"
    assert not config.output.exists()
    assert "password" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_dump_uses_exported_snapshot_for_revision_identity_and_contiguous_cursor(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"transaction_open": False, "closed": False}

    class Transaction:
        async def start(self) -> None:
            state["transaction_open"] = True

        async def rollback(self) -> None:
            state["transaction_open"] = False

    class Connection:
        def transaction(self, **kwargs: object) -> Transaction:
            assert kwargs == {"isolation": "repeatable_read", "readonly": True}
            return Transaction()

        async def fetchval(self, query: str) -> str:
            assert "pg_export_snapshot" in query
            return "00000003-0000001B-1"

        async def fetchrow(self, _query: str) -> dict[str, object]:
            return {
                "schema_revision": "0015_project_reliability_finalize",
                "system_identifier": "7312345678901234567",
                "database_oid": 16384,
                "database_high_watermark": 17,
                "tombstone_count": 3,
                "tombstone_min": 1,
                "tombstone_max": 3,
                "table_count": 41,
            }

        async def close(self) -> None:
            state["closed"] = True

    connection = Connection()
    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=lambda *_args, **_kwargs: connection))

    async def connect(*_args: object, **_kwargs: object) -> Connection:
        return connection

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    monkeypatch.setattr(archive_module, "_exported_snapshot", _ORIGINAL_EXPORTED_SNAPSHOT)

    async def fake_version() -> str:
        return "pg_dump (PostgreSQL) 16.4"

    monkeypatch.setattr(archive_module, "_read_pg_dump_version", fake_version, raising=False)

    async def fake_audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(archive_module, "_record_backup_audit", fake_audit, raising=False)

    async def fake_subprocess(*argv: str, **_kwargs: object) -> _FakeProcess:
        assert state["transaction_open"]
        assert not state["closed"]
        assert "--snapshot=00000003-0000001B-1" in argv
        assert all("password" not in argument for argument in argv)
        return _FakeProcess(0, [b"PGDMPpayload"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    manifest = await create_backup(
        BackupConfig(
            database_url="postgresql://user:password@db/test",
            output=tmp_path / "snapshot.dfba",
            key=backup_key,
        )
    )

    expected_identity = hashlib.sha256(b"deerflow-postgres-source-v1\x007312345678901234567\x0016384").hexdigest()
    assert manifest.schema_revision == "0015_project_reliability_finalize"
    assert manifest.source_installation_id == expected_identity
    assert manifest.tombstone_journal_sequence == 3
    assert manifest.pg_dump_version == "pg_dump (PostgreSQL) 16.4"
    assert not state["transaction_open"]
    assert state["closed"]


@pytest.mark.asyncio
async def test_tombstone_snapshot_gap_fails_before_pg_dump(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"open": False}

    class Transaction:
        async def start(self) -> None:
            state["open"] = True

        async def rollback(self) -> None:
            state["open"] = False

    class Connection:
        def transaction(self, **_kwargs: object) -> Transaction:
            return Transaction()

        async def fetchval(self, _query: str) -> str:
            return "00000003-0000001B-1"

        async def fetchrow(self, _query: str) -> dict[str, object]:
            return {
                "schema_revision": "0015_project_reliability_finalize",
                "system_identifier": "7312345678901234567",
                "database_oid": 16384,
                "database_high_watermark": 17,
                "tombstone_count": 2,
                "tombstone_min": 1,
                "tombstone_max": 3,
                "table_count": 41,
            }

        async def close(self) -> None:
            return None

    async def connect(*_args: object, **_kwargs: object) -> Connection:
        return Connection()

    spawned = False

    async def fake_subprocess(*_args: str, **_kwargs: object) -> _FakeProcess:
        nonlocal spawned
        spawned = True
        return _FakeProcess(0, [b"PGDMPpayload"])

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    monkeypatch.setattr(archive_module, "_exported_snapshot", _ORIGINAL_EXPORTED_SNAPSHOT)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    with pytest.raises(BackupCommandFailed):
        await create_backup(
            BackupConfig(
                database_url="postgresql://db/test",
                output=tmp_path / "gap.dfba",
                key=backup_key,
            )
        )
    assert not spawned


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stage", ["start", "export", "metadata"])
async def test_snapshot_acquisition_cancellation_rolls_back_and_closes(cancel_stage: str, monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    rolled_back = False
    closed = False

    class Transaction:
        async def start(self) -> None:
            if cancel_stage == "start":
                entered.set()
                await asyncio.Event().wait()

        async def rollback(self) -> None:
            nonlocal rolled_back
            rolled_back = True

    class Connection:
        def transaction(self, **_kwargs: object) -> Transaction:
            return Transaction()

        async def fetchval(self, _query: str) -> str:
            if cancel_stage == "export":
                entered.set()
                await asyncio.Event().wait()
            return "00000003-0000001B-1"

        async def fetchrow(self, _query: str) -> dict[str, object]:
            if cancel_stage == "metadata":
                entered.set()
                await asyncio.Event().wait()
            return {
                "schema_revision": "0015_project_reliability_finalize",
                "system_identifier": "7312345678901234567",
                "database_oid": 16384,
                "database_high_watermark": 17,
                "tombstone_count": 0,
                "tombstone_min": 0,
                "tombstone_max": 0,
                "table_count": 41,
            }

        async def close(self) -> None:
            nonlocal closed
            closed = True

    async def connect(*_args: object, **_kwargs: object) -> Connection:
        return Connection()

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))

    async def acquire() -> None:
        async with _ORIGINAL_EXPORTED_SNAPSHOT("postgresql://db/test"):
            raise AssertionError("snapshot acquisition unexpectedly yielded")

    task = asyncio.create_task(acquire())
    await asyncio.wait_for(entered.wait(), timeout=0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rolled_back
    assert closed


@pytest.mark.asyncio
async def test_snapshot_cleanup_is_shielded_through_repeated_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    yielded = asyncio.Event()
    rollback_entered = asyncio.Event()
    rollback_release = asyncio.Event()
    close_entered = asyncio.Event()
    close_release = asyncio.Event()
    rolled_back = False
    closed = False

    class Transaction:
        async def start(self) -> None:
            return None

        async def rollback(self) -> None:
            nonlocal rolled_back
            rollback_entered.set()
            await rollback_release.wait()
            rolled_back = True

    class Connection:
        def transaction(self, **_kwargs: object) -> Transaction:
            return Transaction()

        async def fetchval(self, _query: str) -> str:
            return "00000003-0000001B-1"

        async def fetchrow(self, _query: str) -> dict[str, object]:
            return {
                "schema_revision": "0015_project_reliability_finalize",
                "system_identifier": "7312345678901234567",
                "database_oid": 16384,
                "database_high_watermark": 17,
                "tombstone_count": 0,
                "tombstone_min": 0,
                "tombstone_max": 0,
                "table_count": 41,
            }

        async def close(self) -> None:
            nonlocal closed
            close_entered.set()
            await close_release.wait()
            closed = True

    async def connect(*_args: object, **_kwargs: object) -> Connection:
        return Connection()

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))

    async def hold_snapshot() -> None:
        async with _ORIGINAL_EXPORTED_SNAPSHOT("postgresql://db/test"):
            yielded.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold_snapshot())
    await asyncio.wait_for(yielded.wait(), timeout=0.2)
    task.cancel()
    await asyncio.wait_for(rollback_entered.wait(), timeout=0.2)
    task.cancel()
    rollback_release.set()
    await asyncio.wait_for(close_entered.wait(), timeout=0.2)
    task.cancel()
    close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rolled_back
    assert closed


@pytest.mark.asyncio
async def test_create_backup_publishes_only_authenticated_archive(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    passfile: Path | None = None

    async def fake_subprocess(*argv: str, **kwargs: object) -> _FakeProcess:
        nonlocal passfile
        assert all("password" not in argument for argument in argv)
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert "PGPASSWORD" not in env
        passfile = Path(env["PGPASSFILE"])
        assert stat.S_IMODE(passfile.stat().st_mode) == 0o600
        assert "password" in passfile.read_text()
        return _FakeProcess(0, [b"PGDMPone", b"two"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    config = BackupConfig(
        database_url="postgresql://user:password@db/test",
        output=tmp_path / "ok.dfba",
        key=backup_key,
    )
    manifest = await create_backup(config)
    assert manifest.chunk_count == 1
    assert b"".join(BackupArchiveReader(backup_key).verified_chunks(config.output)) == b"PGDMPonetwo"
    assert not list(tmp_path.glob(".ok.dfba.*"))
    assert passfile is not None and not passfile.exists()


@pytest.mark.asyncio
async def test_passfile_creation_cancellation_removes_owned_file(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    created = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    passfile: Path | None = None
    original_create = archive_module._create_libpq_invocation

    def blocking_create(*args: object, **kwargs: object):
        nonlocal passfile
        invocation = original_create(*args, **kwargs)
        passfile = invocation.passfile
        created.set()
        release.wait(timeout=2)
        finished.set()
        return invocation

    monkeypatch.setattr(archive_module, "_create_libpq_invocation", blocking_create)
    task = asyncio.create_task(
        create_backup(
            BackupConfig(
                database_url="postgresql://user:password@db/test",
                output=tmp_path / "cancel-passfile.dfba",
                key=backup_key,
            )
        )
    )
    assert await asyncio.to_thread(created.wait, 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 1)
    assert passfile is not None and not passfile.exists()


@pytest.mark.asyncio
async def test_passfile_create_and_remove_fsync_the_pinned_directory(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    directory_fsyncs = 0
    original_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        nonlocal directory_fsyncs
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == parent_identity:
            directory_fsyncs += 1
        original_fsync(descriptor)

    async def fake_subprocess(*_argv: str, **_kwargs: object) -> _FakeProcess:
        return _FakeProcess(0, [b"PGDMPpayload"])

    monkeypatch.setattr(archive_module.os, "fsync", recording_fsync)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    await create_backup(
        BackupConfig(
            database_url="postgresql://user:password@db/test",
            output=tmp_path / "fsynced-passfile.dfba",
            key=backup_key,
        )
    )
    assert directory_fsyncs >= 3  # passfile create, passfile unlink, archive publication


@pytest.mark.asyncio
async def test_passfile_cleanup_never_unlinks_replaced_file(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    replacement: Path | None = None

    async def fake_subprocess(*_argv: str, **kwargs: object) -> _FakeProcess:
        nonlocal replacement
        env = kwargs["env"]
        assert isinstance(env, dict)
        replacement = Path(env["PGPASSFILE"])
        replacement.unlink()
        replacement.write_text("replacement", encoding="utf-8")
        return _FakeProcess(0, [b"PGDMPpayload"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    with pytest.raises(BackupCommandFailed):
        await create_backup(
            BackupConfig(
                database_url="postgresql://user:password@db/test",
                output=tmp_path / "replaced-passfile.dfba",
                key=backup_key,
            )
        )
    assert replacement is not None
    assert replacement.read_text(encoding="utf-8") == "replacement"


def test_external_directory_walk_pins_every_ancestor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "operator" / "archives"
    parent.mkdir(parents=True)
    opened: list[tuple[object, int, int | None]] = []
    original_open = os.open

    def recording_open(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        opened.append((path, flags, dir_fd))
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(archive_module.os, "open", recording_open)
    pinned = archive_module._open_external_directory(parent)
    try:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        assert opened
        assert all(flags & no_follow and flags & directory for _path, flags, _dir_fd in opened)
        assert all(dir_fd is not None for path, _flags, dir_fd in opened if os.fspath(path) != parent.anchor)

        moved = tmp_path / "moved-operator"
        parent.parent.rename(moved)
        parent.mkdir(parents=True)
        descriptor = os.open("proof", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=pinned.descriptor)
        os.close(descriptor)
        assert (moved / "archives" / "proof").exists()
        assert not (parent / "proof").exists()
    finally:
        pinned.close()


def test_pgpass_components_reject_control_characters(tmp_path: Path) -> None:
    with pytest.raises(BackupCommandFailed):
        archive_module._create_libpq_invocation("postgresql://user:bad%0Asecret@db/test", tmp_path)
    assert not list(tmp_path.glob(".pgpass.*"))


@pytest.mark.asyncio
async def test_backup_service_rejects_repository_output_before_process_spawn(backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned = False

    async def fake_subprocess(*_argv: str, **_kwargs: object) -> _FakeProcess:
        nonlocal spawned
        spawned = True
        return _FakeProcess(0, [b"PGDMPpayload"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    with pytest.raises(ValueError, match="BACKUP_OUTPUT_MUST_BE_EXTERNAL"):
        await create_backup(
            BackupConfig(
                database_url="postgresql://db/test",
                output=Path(__file__).resolve().parents[2] / "must-not-exist.dfba",
                key=backup_key,
            )
        )
    assert not spawned


@pytest.mark.asyncio
async def test_cancellation_during_publication_removes_final_archive(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.recovery.archive as archive_module

    async def fake_subprocess(*_argv: str, **_kwargs: object) -> _FakeProcess:
        return _FakeProcess(0, [b"PGDMPpayload"])

    async def fake_audit(*_args: object, **_kwargs: object) -> None:
        return None

    entered = threading.Event()
    release = threading.Event()
    original_fsync = archive_module._fsync_directory

    def blocking_fsync(path: Path) -> None:
        entered.set()
        release.wait(timeout=2)
        original_fsync(path)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(archive_module, "_record_backup_audit", fake_audit, raising=False)
    monkeypatch.setattr(archive_module, "_fsync_directory", blocking_fsync)
    config = BackupConfig(
        database_url="postgresql://db/test",
        output=tmp_path / "cancelled.dfba",
        key=backup_key,
    )
    task = asyncio.create_task(create_backup(config))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert not config.output.exists()


@pytest.mark.asyncio
async def test_cancellation_during_post_commit_descriptor_close_preserves_archive(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_subprocess(*_argv: str, **_kwargs: object) -> _FakeProcess:
        return _FakeProcess(0, [b"PGDMPpayload"])

    entered = threading.Event()
    release = threading.Event()
    original_close = BackupArchiveWriter.close

    def blocking_close(writer: BackupArchiveWriter) -> None:
        entered.set()
        release.wait(timeout=2)
        original_close(writer)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(BackupArchiveWriter, "close", blocking_close)
    config = BackupConfig(
        database_url="postgresql://db/test",
        output=tmp_path / "close-cancelled.dfba",
        key=backup_key,
    )
    task = asyncio.create_task(create_backup(config))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert b"".join(BackupArchiveReader(backup_key).verified_chunks(config.output)) == b"PGDMPpayload"


@pytest.mark.asyncio
async def test_cancellation_during_post_commit_audit_dispose_preserves_archive(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    committed = asyncio.Event()
    dispose_entered = asyncio.Event()
    dispose_release = asyncio.Event()

    class Engine:
        async def dispose(self) -> None:
            dispose_entered.set()
            await dispose_release.wait()

    async def fake_subprocess(*_argv: str, **_kwargs: object) -> _FakeProcess:
        return _FakeProcess(0, [b"PGDMPpayload"])

    async def fake_audit(*_args: object, **_kwargs: object) -> None:
        committed.set()
        await archive_module._dispose_audit_engine(Engine(), committed=True)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(archive_module, "_record_backup_audit", fake_audit)
    config = BackupConfig(
        database_url="postgresql://db/test",
        output=tmp_path / "audit-dispose-cancelled.dfba",
        key=backup_key,
    )
    task = asyncio.create_task(create_backup(config))
    await asyncio.wait_for(committed.wait(), timeout=0.2)
    await asyncio.wait_for(dispose_entered.wait(), timeout=0.2)
    task.cancel()
    dispose_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert b"".join(BackupArchiveReader(backup_key).verified_chunks(config.output)) == b"PGDMPpayload"


@pytest.mark.asyncio
async def test_post_commit_audit_dispose_failure_preserves_archive(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    class Engine:
        async def dispose(self) -> None:
            raise OSError("dispose failed")

    async def fake_subprocess(*_argv: str, **_kwargs: object) -> _FakeProcess:
        return _FakeProcess(0, [b"PGDMPpayload"])

    async def fake_audit(*_args: object, **_kwargs: object) -> None:
        await archive_module._dispose_audit_engine(Engine(), committed=True)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(archive_module, "_record_backup_audit", fake_audit)
    config = BackupConfig(
        database_url="postgresql://db/test",
        output=tmp_path / "audit-dispose-failed.dfba",
        key=backup_key,
    )
    await create_backup(config)
    assert b"".join(BackupArchiveReader(backup_key).verified_chunks(config.output)) == b"PGDMPpayload"


@pytest.mark.asyncio
async def test_audit_failure_is_command_failure_and_removes_archive(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.recovery.archive as archive_module

    async def fake_subprocess(*_argv: str, **_kwargs: object) -> _FakeProcess:
        return _FakeProcess(0, [b"PGDMPpayload"])

    called = False

    async def failing_audit(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(archive_module, "_record_backup_audit", failing_audit, raising=False)
    config = BackupConfig(
        database_url="postgresql://db/test",
        output=tmp_path / "audit-failed.dfba",
        key=backup_key,
    )

    with pytest.raises(BackupCommandFailed):
        await create_backup(config)
    assert called
    assert not config.output.exists()


@pytest.mark.asyncio
async def test_hung_pg_dump_is_terminated_then_killed_and_pipe_tasks_are_awaited(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.recovery.archive as archive_module

    stderr_finished = asyncio.Event()
    killed = asyncio.Event()

    class BrokenStdout:
        async def read(self, _size: int) -> bytes:
            raise OSError("pipe failed")

    class WaitingStderr:
        async def read(self, _size: int = -1) -> bytes:
            try:
                await asyncio.Event().wait()
            finally:
                stderr_finished.set()
            return b""

    class HungProcess:
        stdout = BrokenStdout()
        stderr = WaitingStderr()
        returncode = None
        terminated = False
        killed = False

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            killed.set()

        async def wait(self) -> int:
            if self.returncode is None:
                await killed.wait()
            return -9

    process = HungProcess()

    async def fake_subprocess(*_argv: str, **_kwargs: object) -> HungProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(archive_module, "_PROCESS_TERM_TIMEOUT_SECONDS", 0.01, raising=False)
    with pytest.raises(BackupCommandFailed):
        async with asyncio.timeout(0.2):
            await create_backup(
                BackupConfig(
                    database_url="postgresql://db/test",
                    output=tmp_path / "hung.dfba",
                    key=backup_key,
                )
            )
    assert process.terminated and process.killed
    assert stderr_finished.is_set()


@pytest.mark.asyncio
async def test_failed_archive_setup_terminates_started_pg_dump(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    process = _TerminableProcess()

    async def fake_subprocess(*_argv: str, **_kwargs: object) -> _TerminableProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    output = tmp_path / "already-exists.dfba"
    output.write_bytes(b"not an archive")
    with pytest.raises(FileExistsError):
        await create_backup(
            BackupConfig(
                database_url="postgresql://db/test",
                output=output,
                key=backup_key,
            )
        )
    assert process.terminated


@pytest.mark.asyncio
async def test_backup_cli_output_is_redacted_and_uses_only_environment_secrets(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    module = importlib.import_module("scripts.backup_postgres")
    monkeypatch.setenv("DEER_FLOW_BACKUP_KEY", __import__("base64").b64encode(backup_key).decode("ascii"))
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@db/test")

    async def fake_create_backup(config: BackupConfig):
        assert config.database_url == "postgresql://user:password@db/test"
        return type("Manifest", (), {"archive_id": "archive-a", "schema_revision": "0015_project_reliability_finalize", "chunk_count": 2, "chunks": (), "as_dict": lambda self: {}})()

    monkeypatch.setattr(module, "create_backup", fake_create_backup)
    assert await module.async_main(["--output", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "archive-a" in output
    assert "password" not in output
    assert "postgresql" not in output
    assert str(tmp_path) not in output
