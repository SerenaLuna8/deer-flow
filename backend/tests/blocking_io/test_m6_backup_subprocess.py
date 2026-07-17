"""Backup filesystem encryption and publication stay off the asyncio event loop."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


class _Stdout:
    def __init__(self) -> None:
        self._sent = False

    async def read(self, _size: int) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return b"PGDMP encrypted backup input"


class _Stderr:
    async def read(self, _size: int = -1) -> bytes:
        return b""


class _Process:
    stdout = _Stdout()
    stderr = _Stderr()

    async def wait(self) -> int:
        return 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


async def test_backup_subprocess_file_and_crypto_work_does_not_block_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.recovery.archive as archive_module
    from app.recovery.archive import BackupConfig, BackupSnapshot, create_backup

    @asynccontextmanager
    async def fake_snapshot(_database_url: str):
        yield BackupSnapshot(
            snapshot_id="00000003-0000001B-1",
            schema_revision="0015_project_reliability_finalize",
            source_installation_id=hashlib.sha256(b"blocking-test").hexdigest(),
            database_high_watermark=1,
            tombstone_journal_sequence=0,
            table_count=41,
        )

    async def fake_version() -> str:
        return "pg_dump (PostgreSQL) 16.4"

    async def fake_audit(_database_url: str, _manifest: object) -> None:
        return None

    async def fake_subprocess(*_argv: str, **_kwargs: object) -> _Process:
        return _Process()

    monkeypatch.setenv("AUTH_JWT_SECRET", "blocking-test-auth-secret-distinct-from-backup")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(archive_module, "_exported_snapshot", fake_snapshot)
    monkeypatch.setattr(archive_module, "_read_pg_dump_version", fake_version)
    monkeypatch.setattr(archive_module, "_record_backup_audit", fake_audit)
    await create_backup(
        BackupConfig(
            database_url="postgresql://db/test",
            output=tmp_path / "archive.dfba",
            key=bytes(range(32)),
        )
    )
