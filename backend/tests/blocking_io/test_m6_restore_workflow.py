"""Restore archive, journal, passfile, and cleanup work stays off the event loop."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio
SOURCE_ID = hashlib.sha256(b"restore-blocking-source").hexdigest()


def _build_archive(root: Path, key: bytes) -> Path:
    from app.recovery import BackupArchiveWriter

    root.mkdir(parents=True)
    archive = root / "archive.dfba"
    with BackupArchiveWriter.atomic(
        archive,
        key,
        source_installation_id=SOURCE_ID,
        schema_revision="0015_project_reliability_finalize",
        table_count=41,
    ) as writer:
        writer.write_chunk(b"PGDMP restore blocking test")
        writer.finalize(database_high_watermark=0, tombstone_journal_sequence=0)
    return archive


async def test_restore_archive_and_journal_file_io_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.recovery.restore as restore_module
    from app.recovery.journal import TombstoneJournal
    from app.recovery.restore import RestoreConfig, Restorer
    from app.reliability.owner_refs import AuditHmacKeyring

    key = b"b" * 32
    archive = await asyncio.to_thread(_build_archive, tmp_path / "archive-root", key)
    journal = TombstoneJournal(
        tmp_path / "journal-root" / "tombstones.jsonl",
        b"j" * 32,
        source_installation_id=SOURCE_ID,
    )
    await asyncio.to_thread(journal.snapshot)

    async def database_missing(_url: str, _database: str) -> bool:
        return False

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    async def no_replay(*_args: object, **_kwargs: object) -> int:
        return 0

    @asynccontextmanager
    async def source_authority(*_args: object, **_kwargs: object):
        yield await asyncio.to_thread(journal.snapshot, require_existing=True)

    monkeypatch.setattr(restore_module, "_database_exists", database_missing)
    monkeypatch.setattr(restore_module, "_source_recovery_authority", source_authority)
    monkeypatch.setattr(restore_module, "_record_source_restore_started", no_op)
    monkeypatch.setattr(restore_module, "_create_empty_database", no_op)
    monkeypatch.setattr(restore_module, "_run_pg_restore", no_op)
    monkeypatch.setattr(restore_module, "replay_tombstones", no_replay)
    monkeypatch.setattr(restore_module, "_run_recovery_probes", no_op)
    monkeypatch.setattr(restore_module, "_write_proof_and_completion", no_op)

    result = await Restorer(
        RestoreConfig(
            archive=archive,
            target_database_url=(f"postgresql://operator@127.0.0.1/deerflow_restore_{os.getpid()}_0123456789abcdef0123456789abcdef"),
            current_database_url="postgresql://operator@127.0.0.1/deerflow_source",
            journal=journal,
            backup_key=key,
            keyring=AuditHmacKeyring(
                active_key_id="audit-v1",
                _keys={"audit-v1": b"a" * 32},
            ),
        )
    ).restore()

    assert result.status == "verified"


class _CancellableProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.started = asyncio.Event()
        self._finished = asyncio.Event()
        self.terminated = False

    async def wait(self) -> int:
        self.started.set()
        await self._finished.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._finished.set()

    def kill(self) -> None:
        self.returncode = -9
        self._finished.set()


class _SlowTerminationProcess(_CancellableProcess):
    def __init__(self) -> None:
        super().__init__()
        self.termination_started = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        self.termination_started.set()

    def finish_termination(self) -> None:
        self.returncode = -15
        self._finished.set()


async def test_cancelled_pg_restore_settles_child_and_removes_passfile_off_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.recovery.restore as restore_module
    from app.recovery.cleanup import (
        _cleanup_owned_workspace,
        _create_owned_workspace,
    )

    process = _CancellableProcess()
    workspace = await asyncio.to_thread(
        _create_owned_workspace,
        prefix="deerflow-blocking-restore-",
    )

    async def fake_subprocess(*_argv: str, **_kwargs: object) -> _CancellableProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    task = asyncio.create_task(
        restore_module._run_pg_restore(
            "postgresql://operator:secret@127.0.0.1/deerflow_restore_1_0123456789abcdef0123456789abcdef",
            tmp_path / "authenticated.dump",
            workspace,
        )
    )
    await process.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminated
    assert workspace.files == {}
    await asyncio.to_thread(
        _cleanup_owned_workspace,
        workspace.path,
        workspace.identity,
        {},
    )


async def test_repeated_cancellation_still_settles_pg_restore_termination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_module = await asyncio.to_thread(
        importlib.import_module,
        "app.recovery.restore_process",
    )
    from app.recovery.cleanup import (
        _cleanup_owned_workspace,
        _create_owned_workspace,
    )

    process = _SlowTerminationProcess()
    workspace = await asyncio.to_thread(
        _create_owned_workspace,
        prefix="deerflow-blocking-restore-repeat-",
    )

    async def fake_subprocess(*_argv: str, **_kwargs: object) -> _SlowTerminationProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    task = asyncio.create_task(
        process_module.run_pg_restore(
            "postgresql://operator@127.0.0.1/deerflow_restore_1_0123456789abcdef0123456789abcdef",
            tmp_path / "authenticated.dump",
            workspace,
        )
    )
    await process.started.wait()
    task.cancel()
    await process.termination_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    process.finish_termination()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated
    await asyncio.to_thread(
        _cleanup_owned_workspace,
        workspace.path,
        workspace.identity,
        {},
    )
