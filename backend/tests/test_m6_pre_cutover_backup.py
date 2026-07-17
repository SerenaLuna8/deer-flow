from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import uuid
from pathlib import Path

import pytest

import app.recovery.pre_cutover_backup as commit_module
from app.recovery.pre_cutover_backup import (
    PreCutoverBackupCommitError,
    commit_pre_cutover_backup,
    publish_pre_cutover_backup_commit,
    verify_pre_cutover_backup_commit,
)


@pytest.fixture
def commit_inputs(tmp_path: Path) -> tuple[int, str, dict[str, object], bytes, bytes]:
    name = "pre-m6.dfba.commit.json"
    manifest: dict[str, object] = {
        "archive_id": str(uuid.uuid4()),
        "schema_revision": "0013_project_automation_finalize",
        "source_installation_id": hashlib.sha256(b"source").hexdigest(),
        "database_high_watermark": 9,
        "tombstone_journal_sequence": 0,
        "table_count": 33,
    }
    parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    return parent_fd, name, manifest, b'{"authenticated":"manifest"}', hashlib.sha256(b"backup-key").digest()


def _publish(inputs: tuple[int, str, dict[str, object], bytes, bytes]):
    parent_fd, name, manifest, envelope, key = inputs
    return publish_pre_cutover_backup_commit(
        parent_fd=parent_fd,
        name=name,
        manifest=manifest,
        manifest_envelope=envelope,
        key=key,
    )


def test_commit_round_trip_is_hmac_authenticated_and_bounded(
    tmp_path: Path,
    commit_inputs: tuple[int, str, dict[str, object], bytes, bytes],
) -> None:
    parent_fd, name, manifest, envelope, key = commit_inputs
    try:
        handle = _publish(commit_inputs)
        handle.commit()
        digest = verify_pre_cutover_backup_commit(
            proof=tmp_path / name,
            manifest=manifest,
            manifest_envelope=envelope,
            key=key,
        )
        assert len(digest) == 64
        assert stat_mode(tmp_path / name) == 0o600
    finally:
        os.close(parent_fd)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_commit_write_failure_removes_partial_file(
    tmp_path: Path,
    commit_inputs: tuple[int, str, dict[str, object], bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd, name, *_ = commit_inputs
    original_fdopen = os.fdopen

    class FailingHandle:
        def __init__(self, descriptor: int) -> None:
            self.descriptor = descriptor

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            os.close(self.descriptor)

        def write(self, _value: bytes) -> None:
            raise OSError("write failed")

        def flush(self) -> None:
            return None

        def fileno(self) -> int:
            return self.descriptor

    monkeypatch.setattr(commit_module.os, "fdopen", lambda descriptor, _mode: FailingHandle(descriptor))
    try:
        with pytest.raises(PreCutoverBackupCommitError):
            _publish(commit_inputs)
        assert not (tmp_path / name).exists()
        assert not tuple(tmp_path.glob(".*.tmp"))
    finally:
        monkeypatch.setattr(commit_module.os, "fdopen", original_fdopen)
        os.close(parent_fd)


def test_commit_directory_fsync_failure_removes_published_identity(
    tmp_path: Path,
    commit_inputs: tuple[int, str, dict[str, object], bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd, name, *_ = commit_inputs
    monkeypatch.setattr(commit_module, "_fsync_directory", lambda _descriptor: (_ for _ in ()).throw(OSError("fsync failed")))
    try:
        with pytest.raises(PreCutoverBackupCommitError):
            _publish(commit_inputs)
        assert not (tmp_path / name).exists()
    finally:
        os.close(parent_fd)


def test_commit_unlink_failure_is_not_swallowed(
    tmp_path: Path,
    commit_inputs: tuple[int, str, dict[str, object], bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd, name, *_ = commit_inputs
    original_unlink = os.unlink
    monkeypatch.setattr(commit_module, "_fsync_directory", lambda _descriptor: (_ for _ in ()).throw(OSError("fsync failed")))
    monkeypatch.setattr(commit_module.os, "unlink", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unlink failed")))
    try:
        with pytest.raises(PreCutoverBackupCommitError):
            _publish(commit_inputs)
        assert (tmp_path / name).exists()
    finally:
        original_unlink(tmp_path / name)
        os.close(parent_fd)


def test_identity_safe_remove_does_not_delete_replacement(
    tmp_path: Path,
    commit_inputs: tuple[int, str, dict[str, object], bytes, bytes],
) -> None:
    parent_fd, name, *_ = commit_inputs
    proof = tmp_path / name
    try:
        handle = _publish(commit_inputs)
        proof.unlink()
        proof.write_bytes(b"replacement")
        with pytest.raises(OSError):
            handle.remove()
        assert proof.read_bytes() == b"replacement"
    finally:
        os.close(parent_fd)


def test_commit_never_replaces_existing_proof(
    tmp_path: Path,
    commit_inputs: tuple[int, str, dict[str, object], bytes, bytes],
) -> None:
    parent_fd, name, *_ = commit_inputs
    proof = tmp_path / name
    proof.write_bytes(b"existing")
    try:
        with pytest.raises(PreCutoverBackupCommitError):
            _publish(commit_inputs)
        assert proof.read_bytes() == b"existing"
        assert not tuple(tmp_path.glob(".*.tmp"))
    finally:
        os.close(parent_fd)


def test_tampered_commit_is_rejected(
    tmp_path: Path,
    commit_inputs: tuple[int, str, dict[str, object], bytes, bytes],
) -> None:
    parent_fd, name, manifest, envelope, key = commit_inputs
    proof = tmp_path / name
    try:
        handle = _publish(commit_inputs)
        handle.commit()
        raw = bytearray(proof.read_bytes())
        raw[-2] ^= 1
        proof.write_bytes(raw)
        with pytest.raises(PreCutoverBackupCommitError):
            verify_pre_cutover_backup_commit(
                proof=proof,
                manifest=manifest,
                manifest_envelope=envelope,
                key=key,
            )
    finally:
        os.close(parent_fd)


@pytest.mark.asyncio
async def test_pre_cutover_commit_cancellation_settles_and_removes_receipt(
    tmp_path: Path,
    commit_inputs: tuple[int, str, dict[str, object], bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd, name, manifest, envelope, key = commit_inputs
    started = threading.Event()
    release = threading.Event()
    original = publish_pre_cutover_backup_commit

    def delayed_publish(**kwargs: object):
        started.set()
        assert release.wait(timeout=5)
        return original(**kwargs)

    monkeypatch.setattr(commit_module, "publish_pre_cutover_backup_commit", delayed_publish)
    task = asyncio.create_task(
        commit_pre_cutover_backup(
            parent_fd=parent_fd,
            name=name,
            manifest=manifest,
            manifest_envelope=envelope,
            key=key,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (tmp_path / name).exists()
    finally:
        os.close(parent_fd)
