"""Backup filesystem encryption and publication stay off the asyncio event loop."""

from __future__ import annotations

import asyncio
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
        return b"encrypted backup input"


class _Stderr:
    async def read(self) -> bytes:
        return b""


class _Process:
    stdout = _Stdout()
    stderr = _Stderr()

    async def wait(self) -> int:
        return 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


async def test_backup_subprocess_file_and_crypto_work_does_not_block_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.recovery.archive import BackupConfig, create_backup

    async def fake_subprocess(*_argv: str, **_kwargs: object) -> _Process:
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    await create_backup(
        BackupConfig(
            database_url="postgresql://db/test",
            output=tmp_path / "archive.dfba",
            key=bytes(range(32)),
            source_installation_id="source-a",
            database_high_watermark=1,
            tombstone_journal_sequence=0,
        )
    )
