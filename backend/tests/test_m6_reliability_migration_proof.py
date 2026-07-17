from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

import scripts.migrate_reliability as migration
from app.recovery.pre_cutover_backup import publish_external_proof


@pytest.mark.asyncio
async def test_attestation_cancellation_settles_producer_and_removes_owned_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def delayed_publish(path: Path, encoded: bytes):
        started.set()
        assert release.wait(timeout=5)
        return publish_external_proof(path, encoded)

    monkeypatch.setattr(migration, "publish_external_proof", delayed_publish)
    proof = tmp_path / "attestation.json"
    task = asyncio.create_task(migration._publish_backup_proof(proof, {"proof": "value"}))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not proof.exists()


@pytest.mark.asyncio
async def test_attestation_no_clobber_preserves_existing_proof(tmp_path: Path) -> None:
    proof = tmp_path / "attestation.json"
    proof.write_bytes(b"existing")

    with pytest.raises(migration.ReliabilityMigrationError):
        await migration._publish_backup_proof(proof, {"proof": "replacement"})

    assert proof.read_bytes() == b"existing"
