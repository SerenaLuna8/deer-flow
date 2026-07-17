"""Externally committed backup evidence for the exact pre-M6 schema boundary."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import hmac
import json
import os
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PRE_M6_SCHEMA_REVISION = "0013_project_automation_finalize"
COMMIT_FORMAT = "deerflow.m6.pre-cutover-backup-commit.v1"
_COMMIT_DOMAIN = b"deerflow.m6.pre-cutover-backup-commit.v1\x00"
_COMMIT_INFO = b"deerflow-recovery-archive-v1:pre-m6-cutover-backup-commit-v1:"
_MAX_COMMIT_BYTES = 16 * 1024
_REPOSITORY_ROOT = Path(__file__).parents[3]


class PreCutoverBackupCommitError(RuntimeError):
    """The external pre-cutover backup commit could not be trusted."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _commit_key(key: bytes, archive_id: str) -> bytes:
    if type(key) is not bytes or len(key) != 32:
        raise PreCutoverBackupCommitError
    try:
        archive = uuid.UUID(archive_id)
    except (AttributeError, TypeError, ValueError):
        raise PreCutoverBackupCommitError from None
    if str(archive) != archive_id:
        raise PreCutoverBackupCommitError
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_COMMIT_INFO + archive.bytes,
    ).derive(key)


def _fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def _remove_identity_at(parent_fd: int, name: str, identity: tuple[int, int]) -> None:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (info.st_dev, info.st_ino) != identity:
        raise OSError(errno.ESTALE, "backup commit proof identity changed")
    os.unlink(name, dir_fd=parent_fd)
    _fsync_directory(parent_fd)
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise OSError(errno.EIO, "backup commit proof removal was not durable")


def _rename_noreplace(parent_fd: int, source: str, target: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(parent_fd, source_bytes, parent_fd, target_bytes, 1)
    elif hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(parent_fd, source_bytes, parent_fd, target_bytes, 0x00000004)
    else:
        raise OSError(errno.ENOTSUP, "no no-clobber rename primitive")
    if result != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(errno.EEXIST, "BACKUP_COMMIT_EXISTS", target)
        raise OSError(error, os.strerror(error), target)


@dataclass
class PublishedPreCutoverBackupCommit:
    """Own the published receipt until the archive/receipt commit point settles."""

    parent_fd: int
    name: str
    identity: tuple[int, int]

    def commit(self) -> None:
        if self.parent_fd < 0:
            raise PreCutoverBackupCommitError
        os.close(self.parent_fd)
        self.parent_fd = -1

    def remove(self) -> None:
        if self.parent_fd < 0:
            raise PreCutoverBackupCommitError
        try:
            _remove_identity_at(self.parent_fd, self.name, self.identity)
        finally:
            os.close(self.parent_fd)
            self.parent_fd = -1


def _body(manifest: Mapping[str, object], manifest_envelope: bytes) -> dict[str, object]:
    if (
        manifest.get("schema_revision") != PRE_M6_SCHEMA_REVISION
        or manifest.get("tombstone_journal_sequence") != 0
        or type(manifest.get("database_high_watermark")) is not int
        or int(manifest["database_high_watermark"]) < 0
        or type(manifest.get("table_count")) is not int
        or int(manifest["table_count"]) < 1
    ):
        raise PreCutoverBackupCommitError
    return {
        "format": COMMIT_FORMAT,
        "archive_id": manifest.get("archive_id"),
        "archive_manifest_sha256": hashlib.sha256(manifest_envelope).hexdigest(),
        "schema_revision": manifest.get("schema_revision"),
        "source_installation_id": manifest.get("source_installation_id"),
        "database_high_watermark": manifest.get("database_high_watermark"),
        "tombstone_journal_sequence": manifest.get("tombstone_journal_sequence"),
        "table_count": manifest.get("table_count"),
    }


def publish_pre_cutover_backup_commit(
    *,
    parent_fd: int,
    name: str,
    manifest: Mapping[str, object],
    manifest_envelope: bytes,
    key: bytes,
) -> PublishedPreCutoverBackupCommit:
    """Publish one no-clobber, fd-owned commit receipt beside its archive."""

    body = _body(manifest, manifest_envelope)
    archive_id = str(body["archive_id"])
    body["signature"] = hmac.new(
        _commit_key(key, archive_id),
        _COMMIT_DOMAIN + _canonical_json(body),
        hashlib.sha256,
    ).hexdigest()
    return _publish_bytes(parent_fd, name, _canonical_json(body))


async def commit_pre_cutover_backup(
    *,
    parent_fd: int,
    name: str,
    manifest: Mapping[str, object],
    manifest_envelope: bytes,
    key: bytes,
) -> None:
    """Settle publication/removal before returning success or cancellation."""

    task = asyncio.create_task(
        asyncio.to_thread(
            publish_pre_cutover_backup_commit,
            parent_fd=parent_fd,
            name=name,
            manifest=manifest,
            manifest_envelope=manifest_envelope,
            key=key,
        )
    )
    result, cancelled = await _settle_thread_task(task)
    if not isinstance(result, PublishedPreCutoverBackupCommit):
        raise PreCutoverBackupCommitError
    if cancelled:
        cleanup = asyncio.create_task(asyncio.to_thread(result.remove))
        try:
            await _settle_thread_task(cleanup)
        except BaseException:
            raise PreCutoverBackupCommitError from None
        raise asyncio.CancelledError
    result.commit()


async def _settle_thread_task(task: asyncio.Task[object]) -> tuple[object, bool]:
    cancelled = False
    current = asyncio.current_task()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if current is not None:
                current.uncancel()
    try:
        result = task.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    return result, cancelled


def _publish_bytes(parent_fd: int, name: str, encoded: bytes) -> PublishedPreCutoverBackupCommit:
    if Path(name).name != name or name in {"", ".", ".."} or not encoded:
        raise PreCutoverBackupCommitError
    owned_parent = os.dup(parent_fd)
    staging_name = f".{name}.{uuid.uuid4()}.tmp"
    current_name = staging_name
    identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            staging_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=owned_parent,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError(errno.EINVAL, "backup commit proof is not regular")
            identity = (info.st_dev, info.st_ino)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        staged = os.stat(staging_name, dir_fd=owned_parent, follow_symlinks=False)
        if identity != (staged.st_dev, staged.st_ino):
            raise OSError(errno.ESTALE, "backup commit proof identity changed")
        _rename_noreplace(owned_parent, staging_name, name)
        current_name = name
        published = os.stat(name, dir_fd=owned_parent, follow_symlinks=False)
        if identity != (published.st_dev, published.st_ino):
            raise OSError(errno.ESTALE, "backup commit proof identity changed")
        _fsync_directory(owned_parent)
        return PublishedPreCutoverBackupCommit(owned_parent, name, identity)
    except BaseException as write_error:
        cleanup_error: BaseException | None = None
        if identity is not None:
            try:
                _remove_identity_at(owned_parent, current_name, identity)
            except FileNotFoundError:
                pass
            except BaseException as error:
                cleanup_error = error
        os.close(owned_parent)
        raise PreCutoverBackupCommitError from (cleanup_error or write_error)


def publish_external_proof(path: Path, encoded: bytes) -> PublishedPreCutoverBackupCommit:
    """Publish an external proof with the same fd-owned durable commit contract."""

    path = path.expanduser().absolute()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved_parent = path.parent.resolve(strict=True)
    repository = _REPOSITORY_ROOT.resolve(strict=True)
    if resolved_parent == repository or repository in resolved_parent.parents:
        raise PreCutoverBackupCommitError
    parent_info = os.lstat(path.parent)
    if not stat.S_ISDIR(parent_info.st_mode):
        raise PreCutoverBackupCommitError
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(parent_fd)
        if (opened.st_dev, opened.st_ino) != (parent_info.st_dev, parent_info.st_ino):
            raise PreCutoverBackupCommitError
        os.fchmod(parent_fd, 0o700)
        return _publish_bytes(parent_fd, path.name, encoded)
    finally:
        os.close(parent_fd)


def verify_pre_cutover_backup_commit(
    *,
    proof: Path,
    manifest: Mapping[str, object],
    manifest_envelope: bytes,
    key: bytes,
) -> str:
    """Authenticate a bounded receipt against the already-authenticated archive."""

    descriptor: int | None = None
    try:
        descriptor = os.open(proof, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_COMMIT_BYTES:
            raise PreCutoverBackupCommitError
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(_MAX_COMMIT_BYTES + 1)
            after = os.fstat(handle.fileno())
        if len(raw) > _MAX_COMMIT_BYTES or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise PreCutoverBackupCommitError
        value = json.loads(raw)
        expected = _body(manifest, manifest_envelope)
        if not isinstance(value, dict) or set(value) != {*expected, "signature"}:
            raise PreCutoverBackupCommitError
        signature = value.pop("signature")
        if value != expected or not isinstance(signature, str):
            raise PreCutoverBackupCommitError
        expected_signature = hmac.new(
            _commit_key(key, str(expected["archive_id"])),
            _COMMIT_DOMAIN + _canonical_json(value),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise PreCutoverBackupCommitError
        return hashlib.sha256(raw).hexdigest()
    except PreCutoverBackupCommitError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise PreCutoverBackupCommitError from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
