from __future__ import annotations

import asyncio
import importlib
import stat
from pathlib import Path

import pytest

from app.recovery.archive import (
    CHUNK_SIZE,
    BackupArchiveReader,
    BackupArchiveWriter,
    BackupAuthenticationFailed,
    BackupCommandFailed,
    BackupConfig,
    BackupKeyInvalid,
    BackupKeyMissing,
    create_backup,
    load_backup_key,
    pg_dump_argv,
)


@pytest.fixture
def backup_key() -> bytes:
    return bytes(range(32))


def make_archive(tmp_path: Path, backup_key: bytes, payload: bytes):
    output = tmp_path / "archive.dfba"
    with BackupArchiveWriter.atomic(output, backup_key, chunk_bytes=CHUNK_SIZE, source_installation_id="source-a") as writer:
        for offset in range(0, len(payload), CHUNK_SIZE):
            writer.write_chunk(payload[offset : offset + CHUNK_SIZE])
        manifest = writer.finalize(database_high_watermark=12, tombstone_journal_sequence=4)
    return output, manifest


def test_tampered_chunk_fails_before_plaintext_release(tmp_path: Path, backup_key: bytes) -> None:
    archive, _ = make_archive(tmp_path, backup_key, b"a" * (CHUNK_SIZE + 1))
    chunk = archive / "chunks" / "00000001.bin"
    ciphertext = bytearray(chunk.read_bytes())
    ciphertext[0] ^= 1
    chunk.write_bytes(ciphertext)

    with pytest.raises(BackupAuthenticationFailed):
        list(BackupArchiveReader(backup_key).verified_chunks(archive))


def test_each_chunk_uses_unique_nonce(tmp_path: Path, backup_key: bytes) -> None:
    _, manifest = make_archive(tmp_path, backup_key, b"x" * CHUNK_SIZE * 3)
    assert len({chunk.nonce for chunk in manifest.chunks}) == 3


def test_manifest_tampering_and_wrong_key_fail_closed(tmp_path: Path, backup_key: bytes) -> None:
    archive, _ = make_archive(tmp_path, backup_key, b"database bytes")
    manifest_path = archive / "manifest.json"
    tampered = bytearray(manifest_path.read_bytes())
    tampered[-2] ^= 1
    manifest_path.write_bytes(tampered)
    with pytest.raises(BackupAuthenticationFailed):
        list(BackupArchiveReader(backup_key).verified_chunks(archive))

    archive, _ = make_archive(tmp_path / "wrong-key", backup_key, b"database bytes")
    with pytest.raises(BackupAuthenticationFailed):
        list(BackupArchiveReader(b"z" * 32).verified_chunks(archive))


def test_backup_key_must_be_distinct_32_byte_secret(tmp_path: Path) -> None:
    with pytest.raises(BackupKeyInvalid):
        BackupArchiveWriter.atomic(tmp_path / "archive", b"database-password", source_installation_id="source-a")
    with pytest.raises(BackupKeyInvalid):
        BackupArchiveReader(b"credential-keyring")


def test_missing_backup_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEER_FLOW_BACKUP_KEY", raising=False)
    with pytest.raises(BackupKeyMissing):
        load_backup_key()


def test_archive_permissions_are_operator_only(tmp_path: Path, backup_key: bytes) -> None:
    archive, _ = make_archive(tmp_path, backup_key, b"database bytes")
    assert stat.S_IMODE(archive.stat().st_mode) == 0o700
    assert stat.S_IMODE((archive / "manifest.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((archive / "chunks" / "00000000.bin").stat().st_mode) == 0o600


def test_pg_dump_argv_is_fixed_and_never_shell_interpolated() -> None:
    database_url = "postgresql://user:password@db/test; rm -rf /"
    argv = pg_dump_argv(database_url)
    assert argv == ("pg_dump", "--format=custom", "--no-owner", "--no-acl", database_url)
    assert all(not isinstance(argument, str) or "sh" not in argument for argument in argv[:-1])


class _FakeStdout:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    async def read(self, _size: int) -> bytes:
        return next(self._chunks, b"")


class _FakeStderr:
    def __init__(self, value: bytes) -> None:
        self._value = value

    async def read(self) -> bytes:
        return self._value


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
        source_installation_id="source-a",
        database_high_watermark=12,
        tombstone_journal_sequence=4,
    )

    with pytest.raises(BackupCommandFailed) as exc_info:
        await create_backup(config)
    assert str(exc_info.value) == "BACKUP_COMMAND_FAILED"
    assert not config.output.exists()
    assert "password" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_create_backup_publishes_only_authenticated_archive(tmp_path: Path, backup_key: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_subprocess(*_argv: str, **_kwargs: object) -> _FakeProcess:
        return _FakeProcess(0, [b"one", b"two"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    config = BackupConfig(
        database_url="postgresql://db/test",
        output=tmp_path / "ok.dfba",
        key=backup_key,
        source_installation_id="source-a",
        database_high_watermark=12,
        tombstone_journal_sequence=4,
    )
    manifest = await create_backup(config)
    assert manifest.chunk_count == 2
    assert b"".join(BackupArchiveReader(backup_key).verified_chunks(config.output)) == b"onetwo"
    assert not list(tmp_path.glob(".ok.dfba.*"))


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
                source_installation_id="source-a",
                database_high_watermark=12,
                tombstone_journal_sequence=4,
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
        return type("Manifest", (), {"archive_id": "archive-a", "schema_revision": 1, "chunk_count": 2, "chunks": (), "as_dict": lambda self: {}})()

    monkeypatch.setattr(module, "create_backup", fake_create_backup)
    assert await module.async_main(["--output", str(tmp_path), "--source-installation-id", "source-a"]) == 0
    output = capsys.readouterr().out
    assert "archive-a" in output
    assert "password" not in output
    assert "postgresql" not in output
    assert str(tmp_path) not in output
