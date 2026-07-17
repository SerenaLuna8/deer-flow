"""Authenticated, snapshot-consistent archives for trusted PostgreSQL backups.

The archive contains only encrypted ``pg_dump`` custom-format bytes. Restore,
tombstone replay, retention purge, and recovery drills remain Task 17 work.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ctypes
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.engine import make_url

CHUNK_SIZE = 1_048_576
ARCHIVE_FORMAT_VERSION = 1
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_CHUNKS = 65_536
MAX_ARCHIVE_PLAINTEXT_BYTES = CHUNK_SIZE * MAX_ARCHIVE_CHUNKS
_PGDMP_MAGIC = b"PGDMP"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_KEY_BYTES = 32
_SALT_BYTES = 32
_READ_BLOCK_BYTES = 64 * 1024
_PROCESS_TERM_TIMEOUT_SECONDS = 5.0
_VERSION_OUTPUT_LIMIT = 512
_AUTH_SECRET_FILE_LIMIT = 16 * 1024
_ARCHIVE_INFO = b"deerflow-recovery-archive-v1"
_CHUNK_INFO = b"deerflow-recovery-chunk-v1"
_MANIFEST_INFO = b"deerflow-recovery-manifest-v1"
_SOURCE_ID_DOMAIN = b"deerflow-postgres-source-v1\x00"
_SCHEMA_REVISION = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_SOURCE_ID = re.compile(r"[0-9a-f]{64}")
_PG_DUMP_VERSION = re.compile(r"pg_dump \(PostgreSQL\) [ -~]{1,96}")
_REPOSITORY_ROOT = Path(__file__).parents[3]
_LIBPQ_QUERY_ENV = {
    "application_name": "PGAPPNAME",
    "channel_binding": "PGCHANNELBINDING",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "gssencmode": "PGGSSENCMODE",
    "sslcert": "PGSSLCERT",
    "sslcrl": "PGSSLCRL",
    "sslkey": "PGSSLKEY",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
}


class BackupAuthenticationFailed(RuntimeError):
    """Archive authenticity or integrity could not be verified."""

    def __init__(self) -> None:
        super().__init__("BACKUP_AUTHENTICATION_FAILED")


class BackupKeyMissing(RuntimeError):
    """The independent operator backup key is absent."""

    def __init__(self) -> None:
        super().__init__("BACKUP_KEY_REQUIRED")


class BackupKeyInvalid(RuntimeError):
    """The independent operator backup key is malformed or reused."""

    def __init__(self) -> None:
        super().__init__("BACKUP_KEY_INVALID")


class BackupCommandFailed(RuntimeError):
    """The backup failed without retaining child output or connection details."""

    def __init__(self) -> None:
        super().__init__("BACKUP_COMMAND_FAILED")


@dataclass(frozen=True)
class BackupChunk:
    index: int
    nonce: str
    plaintext_bytes: int
    ciphertext_sha256: str
    ciphertext_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "nonce": self.nonce,
            "plaintext_bytes": self.plaintext_bytes,
            "ciphertext_sha256": self.ciphertext_sha256,
            "ciphertext_bytes": self.ciphertext_bytes,
        }


@dataclass(frozen=True)
class BackupManifest:
    archive_id: str
    archive_format_version: int
    archive_salt: str
    schema_revision: str
    source_installation_id: str
    chunk_bytes: int
    chunks: tuple[BackupChunk, ...]
    total_plaintext_bytes: int
    total_ciphertext_bytes: int
    database_high_watermark: int
    tombstone_journal_sequence: int
    table_count: int
    pg_dump_version: str
    tool: str = "pg_dump --format=custom --no-owner --no-acl"

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def as_dict(self) -> dict[str, object]:
        return {
            "archive_id": self.archive_id,
            "archive_format_version": self.archive_format_version,
            "archive_salt": self.archive_salt,
            "schema_revision": self.schema_revision,
            "source_installation_id": self.source_installation_id,
            "chunk_bytes": self.chunk_bytes,
            "chunks": [chunk.as_dict() for chunk in self.chunks],
            "total_plaintext_bytes": self.total_plaintext_bytes,
            "total_ciphertext_bytes": self.total_ciphertext_bytes,
            "database_high_watermark": self.database_high_watermark,
            "tombstone_journal_sequence": self.tombstone_journal_sequence,
            "table_count": self.table_count,
            "pg_dump_version": self.pg_dump_version,
            "tool": self.tool,
        }


@dataclass(frozen=True)
class BackupConfig:
    database_url: str
    output: Path
    key: bytes
    chunk_bytes: int = CHUNK_SIZE
    archive_id: str | None = None

    def __post_init__(self) -> None:
        _validate_key(self.key)
        if not isinstance(self.database_url, str) or not self.database_url:
            raise ValueError("database_url is required")
        if type(self.chunk_bytes) is not int or not len(_PGDMP_MAGIC) <= self.chunk_bytes <= CHUNK_SIZE:
            raise ValueError("chunk_bytes must fit the pg_dump header and archive bound")
        if self.archive_id is not None:
            _canonical_uuid(self.archive_id)


@dataclass(frozen=True)
class BackupSnapshot:
    snapshot_id: str
    schema_revision: str
    source_installation_id: str
    database_high_watermark: int
    tombstone_journal_sequence: int
    table_count: int


@dataclass
class _PinnedDirectory:
    path: Path
    descriptor: int

    def duplicate(self) -> _PinnedDirectory:
        if self.descriptor < 0:
            raise OSError(errno.EBADF, "pinned directory is closed")
        return _PinnedDirectory(self.path, os.dup(self.descriptor))

    def close(self) -> None:
        if self.descriptor >= 0:
            try:
                os.close(self.descriptor)
            finally:
                self.descriptor = -1


@dataclass
class _LibpqInvocation:
    env: Mapping[str, str]
    passfile: Path | None
    directory: _PinnedDirectory
    passfile_name: str | None = None
    passfile_identity: tuple[int, int] | None = None
    passfile_descriptor: int | None = None

    @property
    def pass_fds(self) -> tuple[int, ...]:
        if self.passfile_descriptor is None:
            return ()
        if self.passfile_descriptor < 0:
            raise OSError(errno.EBADF, "passfile descriptor is closed")
        return (self.passfile_descriptor,)


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
        raise BackupKeyInvalid


def _decoded_secret_candidates(value: str) -> tuple[bytes, ...]:
    candidates: list[bytes] = []
    try:
        candidates.append(value.encode("utf-8"))
    except UnicodeError:
        pass
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        pass
    else:
        candidates.append(decoded)
    return tuple(candidates)


def _active_auth_secret_candidates() -> tuple[bytes, ...]:
    auth_secret = os.environ.get("AUTH_JWT_SECRET")
    if auth_secret:
        return _decoded_secret_candidates(auth_secret)

    pinned: _PinnedDirectory | None = None
    try:
        from deerflow.config.paths import get_paths

        pinned, _ = _walk_directory(get_paths().base_dir)
        descriptor = os.open(".jwt_secret", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=pinned.descriptor)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _AUTH_SECRET_FILE_LIMIT:
                raise BackupKeyInvalid
            encoded = _read_fd_all(handle, limit=_AUTH_SECRET_FILE_LIMIT).decode("utf-8").strip()
        if not encoded:
            raise BackupKeyInvalid
        return _decoded_secret_candidates(encoded)
    except BackupKeyInvalid:
        raise
    except Exception:
        raise BackupKeyInvalid from None
    finally:
        if pinned is not None:
            pinned.close()


def _known_deployment_secrets(database_url: str | None) -> Iterator[bytes]:
    yield from _active_auth_secret_candidates()
    for name in (
        "DEER_FLOW_AUDIT_KEYRING_JSON",
        "DEER_FLOW_CREDENTIAL_KEYRING_JSON",
    ):
        raw = os.environ.get(name)
        if not raw:
            continue
        try:
            values = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(values, dict):
            for encoded in values.values():
                if isinstance(encoded, str):
                    yield from _decoded_secret_candidates(encoded)
    if database_url:
        try:
            password = make_url(database_url).password
        except Exception:
            password = None
        if password:
            yield from _decoded_secret_candidates(password)


def known_deployment_secrets(database_url: str | None = None) -> tuple[bytes, ...]:
    """Return deployment secret candidates for recovery-key separation checks."""

    return tuple(_known_deployment_secrets(database_url))


def _validate_key_separation(key: bytes, database_url: str | None) -> None:
    _validate_key(key)
    if any(hmac.compare_digest(key, candidate) for candidate in known_deployment_secrets(database_url) if len(candidate) == _KEY_BYTES):
        raise BackupKeyInvalid


def load_backup_key(value: str | None = None, *, database_url: str | None = None) -> bytes:
    """Load and separation-check the operator backup key."""

    encoded = value if value is not None else os.environ.get("DEER_FLOW_BACKUP_KEY")
    if not encoded:
        raise BackupKeyMissing
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise BackupKeyInvalid from None
    if base64.b64encode(key).decode("ascii") != encoded:
        raise BackupKeyInvalid
    _validate_key_separation(key, database_url)
    return key


def _derive_key(master_key: bytes, purpose: bytes, *, salt: bytes | None = None, archive_id: str | None = None) -> bytes:
    _validate_key(master_key)
    archive = _canonical_uuid(archive_id).bytes if archive_id is not None else b""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=salt,
        info=_ARCHIVE_INFO + b":" + purpose + b":" + archive,
    ).derive(master_key)


def _canonical_uuid(value: str | uuid.UUID) -> uuid.UUID:
    try:
        parsed = value if type(value) is uuid.UUID else uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("archive_id must be a UUID") from None
    if str(parsed) != str(value):
        raise ValueError("archive_id must be canonical")
    return parsed


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _aad(archive_id: str, schema_revision: str, source_installation_id: str, index: int) -> bytes:
    return _canonical_json(
        {
            "archive_id": archive_id,
            "chunk_index": index,
            "schema_revision": schema_revision,
            "source_installation_id": source_installation_id,
        }
    )


def _archive_salt(value: str) -> bytes:
    try:
        salt = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise BackupAuthenticationFailed from None
    if len(salt) != _SALT_BYTES or base64.b64encode(salt).decode("ascii") != value:
        raise BackupAuthenticationFailed
    return salt


def _manifest_signature(key: bytes, manifest: dict[str, object]) -> str:
    try:
        archive_id = str(manifest["archive_id"])
        salt = _archive_salt(str(manifest["archive_salt"]))
    except (KeyError, BackupAuthenticationFailed):
        raise BackupAuthenticationFailed from None
    manifest_key = _derive_key(key, _MANIFEST_INFO, salt=salt, archive_id=archive_id)
    return hmac.new(manifest_key, _canonical_json(manifest), hashlib.sha256).hexdigest()


def _chunk_name(index: int) -> str:
    return f"{index:08d}.bin"


def _fsync_directory(path_or_descriptor: Path | int) -> None:
    if type(path_or_descriptor) is int:
        os.fsync(path_or_descriptor)
        return
    descriptor = os.open(path_or_descriptor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _absolute_directory_parts(path: Path) -> tuple[Path, tuple[str, ...]]:
    expanded = path.expanduser()
    absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if not absolute.anchor:
        raise OSError(errno.EINVAL, "archive path must be absolute")
    parts = absolute.parts[1:]
    if any(part in {"", ".", ".."} for part in parts):
        raise OSError(errno.EINVAL, "archive path contains an unsafe component")
    return Path(absolute.anchor).joinpath(*parts), parts


def _walk_directory(path: Path) -> tuple[_PinnedDirectory, tuple[tuple[int, int], ...]]:
    absolute, parts = _absolute_directory_parts(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute.anchor, flags)
    identities: list[tuple[int, int]] = []
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(errno.ENOTDIR, "archive ancestor is not a directory")
        identities.append((info.st_dev, info.st_ino))
        for part in parts:
            child = os.open(part, flags, dir_fd=descriptor)
            try:
                info = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode):
                    raise OSError(errno.ENOTDIR, "archive ancestor is not a directory")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
            identities.append((info.st_dev, info.st_ino))
        return _PinnedDirectory(absolute, descriptor), tuple(identities)
    except BaseException:
        os.close(descriptor)
        raise


def _open_external_directory(path: Path) -> _PinnedDirectory:
    parent, ancestor_identities = _walk_directory(path)
    repository: _PinnedDirectory | None = None
    try:
        repository, _ = _walk_directory(_REPOSITORY_ROOT)
        repository_info = os.fstat(repository.descriptor)
        if (repository_info.st_dev, repository_info.st_ino) in ancestor_identities:
            raise ValueError("BACKUP_OUTPUT_MUST_BE_EXTERNAL")
        os.fchmod(parent.descriptor, 0o700)
        return parent
    except BaseException:
        parent.close()
        raise
    finally:
        if repository is not None:
            repository.close()


def _rename_noreplace(parent_fd: int, source: str, target: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    result: int
    if hasattr(libc, "renameat2"):
        renameat2 = libc.renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(parent_fd, source_bytes, parent_fd, target_bytes, 1)
    elif hasattr(libc, "renameatx_np"):
        renameatx_np = libc.renameatx_np
        renameatx_np.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameatx_np.restype = ctypes.c_int
        result = renameatx_np(parent_fd, source_bytes, parent_fd, target_bytes, 0x00000004)
    else:
        raise OSError(errno.ENOTSUP, "no no-clobber rename primitive")
    if result != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(errno.EEXIST, "BACKUP_OUTPUT_EXISTS", target)
        raise OSError(error, os.strerror(error), target)


def _remove_tree_at(parent_fd: int, name: str) -> None:
    try:
        shutil.rmtree(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return


class BackupArchiveWriter:
    """Synchronous fd-relative writer; async callers use ``to_thread``."""

    def __init__(
        self,
        output: Path,
        key: bytes,
        *,
        chunk_bytes: int = CHUNK_SIZE,
        source_installation_id: str,
        schema_revision: str = "test-schema",
        pg_dump_version: str = "pg_dump (PostgreSQL) test",
        table_count: int = 1,
        archive_id: str | None = None,
        _parent_directory: _PinnedDirectory | None = None,
    ) -> None:
        _validate_key(key)
        if type(chunk_bytes) is not int or not len(_PGDMP_MAGIC) <= chunk_bytes <= CHUNK_SIZE:
            raise ValueError("invalid backup chunk size")
        if _SOURCE_ID.fullmatch(source_installation_id) is None:
            raise ValueError("invalid source installation identity")
        if _SCHEMA_REVISION.fullmatch(schema_revision) is None:
            raise ValueError("invalid schema revision")
        if _PG_DUMP_VERSION.fullmatch(pg_dump_version) is None:
            raise ValueError("invalid pg_dump version")
        if type(table_count) is not int or table_count < 1:
            raise ValueError("invalid table count")
        self.output = Path(output)
        self._key = key
        self._chunk_bytes = chunk_bytes
        self._source_installation_id = source_installation_id
        self._schema_revision = schema_revision
        self._pg_dump_version = pg_dump_version
        self._table_count = table_count
        self._archive_id = str(uuid.uuid4()) if archive_id is None else str(_canonical_uuid(archive_id))
        self._salt = secrets.token_bytes(_SALT_BYTES)
        self._staging_name = f".{self.output.name}.{uuid.uuid4().hex}.part"
        self._chunks: list[BackupChunk] = []
        self._magic = bytearray()
        self._parent_fd: int | None = None
        self._staging_fd: int | None = None
        self._chunks_fd: int | None = None
        self._published = False
        self._published_identity: tuple[int, int] | None = None
        self._finalized = False
        self._provided_parent = _parent_directory

    @classmethod
    def atomic(
        cls,
        output: Path,
        key: bytes,
        *,
        chunk_bytes: int = CHUNK_SIZE,
        source_installation_id: str,
        schema_revision: str = "test-schema",
        pg_dump_version: str = "pg_dump (PostgreSQL) test",
        table_count: int = 1,
        archive_id: str | None = None,
        _parent_directory: _PinnedDirectory | None = None,
    ) -> BackupArchiveWriter:
        return cls(
            output,
            key,
            chunk_bytes=chunk_bytes,
            source_installation_id=source_installation_id,
            schema_revision=schema_revision,
            pg_dump_version=pg_dump_version,
            table_count=table_count,
            archive_id=archive_id,
            _parent_directory=_parent_directory,
        )

    def __enter__(self) -> BackupArchiveWriter:
        if self.output.name in {"", ".", ".."} or Path(self.output.name).name != self.output.name:
            raise ValueError("invalid backup output name")
        parent = self._provided_parent or _open_external_directory(self.output.parent)
        self._provided_parent = None
        self.output = parent.path / self.output.name
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        self._parent_fd = parent.descriptor
        parent.descriptor = -1
        try:
            os.fchmod(self._parent_fd, 0o700)
            try:
                os.stat(self.output.name, dir_fd=self._parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(errno.EEXIST, "BACKUP_OUTPUT_EXISTS", self.output.name)
            os.mkdir(self._staging_name, 0o700, dir_fd=self._parent_fd)
            self._staging_fd = os.open(self._staging_name, flags, dir_fd=self._parent_fd)
            os.mkdir("chunks", 0o700, dir_fd=self._staging_fd)
            self._chunks_fd = os.open("chunks", flags, dir_fd=self._staging_fd)
        except BaseException:
            self.abort()
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if not self._finalized:
                self.abort()
        finally:
            self.close()

    def close(self) -> None:
        for attribute in ("_chunks_fd", "_staging_fd", "_parent_fd"):
            descriptor = getattr(self, attribute)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, attribute, None)

    def abort(self) -> None:
        parent_fd = self._parent_fd
        if parent_fd is None:
            if self._published:
                raise OSError(errno.EBADF, "published archive parent is closed")
            return
        else:
            close_parent = False
        for attribute in ("_chunks_fd", "_staging_fd"):
            descriptor = getattr(self, attribute)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, attribute, None)
        try:
            if self._published:
                try:
                    info = os.stat(self.output.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if self._published_identity is None or (info.st_dev, info.st_ino) != self._published_identity:
                        raise OSError(errno.ESTALE, "published archive identity changed")
                    _remove_tree_at(parent_fd, self.output.name)
                self._published = False
                self._published_identity = None
            else:
                _remove_tree_at(parent_fd, self._staging_name)
            _fsync_directory(parent_fd)
        finally:
            if close_parent:
                os.close(parent_fd)

    def write_chunk(self, plaintext: bytes) -> None:
        if self._finalized or self._chunks_fd is None or not plaintext or len(plaintext) > self._chunk_bytes:
            raise ValueError("invalid backup chunk")
        index = len(self._chunks)
        if index >= MAX_ARCHIVE_CHUNKS:
            raise ValueError("backup archive chunk bound exceeded")
        nonce = index.to_bytes(_NONCE_BYTES, "big")
        chunk_key = _derive_key(self._key, _CHUNK_INFO, salt=self._salt, archive_id=self._archive_id)
        ciphertext = AESGCM(chunk_key).encrypt(
            nonce,
            plaintext,
            _aad(self._archive_id, self._schema_revision, self._source_installation_id, index),
        )
        descriptor = os.open(
            _chunk_name(index),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=self._chunks_fd,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(ciphertext)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        if len(self._magic) < len(_PGDMP_MAGIC):
            needed = len(_PGDMP_MAGIC) - len(self._magic)
            self._magic.extend(plaintext[:needed])
        self._chunks.append(
            BackupChunk(
                index=index,
                nonce=base64.b64encode(nonce).decode("ascii"),
                plaintext_bytes=len(plaintext),
                ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest(),
                ciphertext_bytes=len(ciphertext),
            )
        )

    def finalize(self, *, database_high_watermark: int, tombstone_journal_sequence: int) -> BackupManifest:
        if self._finalized or self._staging_fd is None or self._chunks_fd is None:
            raise ValueError("invalid backup finalization")
        if type(database_high_watermark) is not int or database_high_watermark < 0 or type(tombstone_journal_sequence) is not int or tombstone_journal_sequence < 0:
            raise ValueError("invalid backup high-watermark")
        if not self._chunks or bytes(self._magic) != _PGDMP_MAGIC:
            raise ValueError("pg_dump custom-format input required")
        total_plaintext = sum(chunk.plaintext_bytes for chunk in self._chunks)
        total_ciphertext = sum(chunk.ciphertext_bytes for chunk in self._chunks)
        manifest = BackupManifest(
            archive_id=self._archive_id,
            archive_format_version=ARCHIVE_FORMAT_VERSION,
            archive_salt=base64.b64encode(self._salt).decode("ascii"),
            schema_revision=self._schema_revision,
            source_installation_id=self._source_installation_id,
            chunk_bytes=self._chunk_bytes,
            chunks=tuple(self._chunks),
            total_plaintext_bytes=total_plaintext,
            total_ciphertext_bytes=total_ciphertext,
            database_high_watermark=database_high_watermark,
            tombstone_journal_sequence=tombstone_journal_sequence,
            table_count=self._table_count,
            pg_dump_version=self._pg_dump_version,
        )
        body = manifest.as_dict()
        envelope = {"manifest": body, "signature": _manifest_signature(self._key, body)}
        encoded_envelope = _canonical_json(envelope)
        if len(encoded_envelope) > MAX_MANIFEST_BYTES:
            raise ValueError("backup manifest exceeds authenticated reader bound")
        try:
            descriptor = os.open(
                "manifest.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._staging_fd,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded_envelope)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            os.fsync(self._chunks_fd)
            os.fsync(self._staging_fd)
            if self._parent_fd is None:
                raise OSError(errno.EBADF, "archive parent is closed")
            _rename_noreplace(self._parent_fd, self._staging_name, self.output.name)
            self._published = True
            published = os.stat(self.output.name, dir_fd=self._parent_fd, follow_symlinks=False)
            self._published_identity = (published.st_dev, published.st_ino)
            _fsync_directory(self._parent_fd)
            self._finalized = True
            return manifest
        except BaseException:
            try:
                self.abort()
            except BaseException:
                pass
            raise


def _read_fd_all(handle: BinaryIO, *, limit: int | None = None) -> bytes:
    result = bytearray()
    while True:
        block = handle.read(_READ_BLOCK_BYTES if limit is None else min(_READ_BLOCK_BYTES, limit + 1 - len(result)))
        if not block:
            return bytes(result)
        result.extend(block)
        if limit is not None and len(result) > limit:
            raise BackupAuthenticationFailed


class BackupArchiveReader:
    """Bounded reader which releases plaintext only after complete verification."""

    def __init__(self, key: bytes) -> None:
        _validate_key(key)
        self._key = key

    def verified_chunks(self, archive: Path) -> Iterator[bytes]:
        archive_fd: int | None = None
        chunks_fd: int | None = None
        spool: BinaryIO | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            archive_fd = os.open(Path(archive), flags)
            manifest = self._load_manifest(archive_fd)
            chunks_fd = os.open("chunks", flags, dir_fd=archive_fd)
            expected_names = {_chunk_name(chunk.index) for chunk in manifest.chunks}
            actual_names: set[str] = set()
            with os.scandir(chunks_fd) as entries:
                for entry in entries:
                    actual_names.add(entry.name)
                    if len(actual_names) > len(expected_names):
                        raise BackupAuthenticationFailed
            if actual_names != expected_names:
                raise BackupAuthenticationFailed
            identities = self._verify_ciphertext_pass(chunks_fd, manifest)
            spool = tempfile.TemporaryFile(mode="w+b")
            os.fchmod(spool.fileno(), 0o600)
            self._decrypt_to_spool(chunks_fd, manifest, identities, spool)
            spool.seek(0)
            if spool.read(len(_PGDMP_MAGIC)) != _PGDMP_MAGIC:
                raise BackupAuthenticationFailed
            spool.seek(0)
        except BackupAuthenticationFailed:
            if spool is not None:
                spool.close()
            if chunks_fd is not None:
                os.close(chunks_fd)
            if archive_fd is not None:
                os.close(archive_fd)
            raise
        except Exception:
            if spool is not None:
                spool.close()
            if chunks_fd is not None:
                os.close(chunks_fd)
            if archive_fd is not None:
                os.close(archive_fd)
            raise BackupAuthenticationFailed from None
        try:
            for chunk in manifest.chunks:
                plaintext = spool.read(chunk.plaintext_bytes)
                if len(plaintext) != chunk.plaintext_bytes:
                    raise BackupAuthenticationFailed
                yield plaintext
            if spool.read(1):
                raise BackupAuthenticationFailed
        finally:
            spool.close()
            os.close(chunks_fd)
            os.close(archive_fd)

    def _load_manifest(self, archive_fd: int) -> BackupManifest:
        descriptor = os.open("manifest.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=archive_fd)
        with os.fdopen(descriptor, "rb") as handle:
            envelope = json.loads(_read_fd_all(handle, limit=MAX_MANIFEST_BYTES))
        if not isinstance(envelope, dict) or set(envelope) != {"manifest", "signature"}:
            raise BackupAuthenticationFailed
        body, signature = envelope["manifest"], envelope["signature"]
        if not isinstance(body, dict) or not isinstance(signature, str) or not hmac.compare_digest(signature, _manifest_signature(self._key, body)):
            raise BackupAuthenticationFailed
        if set(body) != {
            "archive_id",
            "archive_format_version",
            "archive_salt",
            "schema_revision",
            "source_installation_id",
            "chunk_bytes",
            "chunks",
            "total_plaintext_bytes",
            "total_ciphertext_bytes",
            "database_high_watermark",
            "tombstone_journal_sequence",
            "table_count",
            "pg_dump_version",
            "tool",
        }:
            raise BackupAuthenticationFailed
        if body.get("archive_format_version") != ARCHIVE_FORMAT_VERSION:
            raise BackupAuthenticationFailed
        archive_id = str(_canonical_uuid(str(body["archive_id"])))
        schema_revision = str(body["schema_revision"])
        source_id = str(body["source_installation_id"])
        pg_dump_version = str(body["pg_dump_version"])
        if _SCHEMA_REVISION.fullmatch(schema_revision) is None or _SOURCE_ID.fullmatch(source_id) is None or _PG_DUMP_VERSION.fullmatch(pg_dump_version) is None:
            raise BackupAuthenticationFailed
        archive_salt = str(body["archive_salt"])
        _archive_salt(archive_salt)
        chunks_data = body.get("chunks")
        if not isinstance(chunks_data, list) or not chunks_data or len(chunks_data) > MAX_ARCHIVE_CHUNKS:
            raise BackupAuthenticationFailed
        chunks = tuple(self._parse_chunk(entry) for entry in chunks_data)
        if [chunk.index for chunk in chunks] != list(range(len(chunks))):
            raise BackupAuthenticationFailed
        manifest = BackupManifest(
            archive_id=archive_id,
            archive_format_version=ARCHIVE_FORMAT_VERSION,
            archive_salt=archive_salt,
            schema_revision=schema_revision,
            source_installation_id=source_id,
            chunk_bytes=_positive_int(body["chunk_bytes"]),
            chunks=chunks,
            total_plaintext_bytes=_positive_int(body["total_plaintext_bytes"]),
            total_ciphertext_bytes=_positive_int(body["total_ciphertext_bytes"]),
            database_high_watermark=_nonnegative_int(body["database_high_watermark"]),
            tombstone_journal_sequence=_nonnegative_int(body["tombstone_journal_sequence"]),
            table_count=_positive_int(body["table_count"]),
            pg_dump_version=pg_dump_version,
            tool=str(body["tool"]),
        )
        if manifest.tool != "pg_dump --format=custom --no-owner --no-acl" or manifest.chunk_bytes < len(_PGDMP_MAGIC) or manifest.chunk_bytes > CHUNK_SIZE or manifest.total_plaintext_bytes > MAX_ARCHIVE_PLAINTEXT_BYTES:
            raise BackupAuthenticationFailed
        if sum(chunk.plaintext_bytes for chunk in chunks) != manifest.total_plaintext_bytes or sum(chunk.ciphertext_bytes for chunk in chunks) != manifest.total_ciphertext_bytes:
            raise BackupAuthenticationFailed
        return manifest

    @staticmethod
    def _parse_chunk(entry: object) -> BackupChunk:
        if not isinstance(entry, dict) or set(entry) != {"index", "nonce", "plaintext_bytes", "ciphertext_sha256", "ciphertext_bytes"}:
            raise BackupAuthenticationFailed
        index = _nonnegative_int(entry["index"])
        nonce = str(entry["nonce"])
        try:
            nonce_bytes = base64.b64decode(nonce, validate=True)
        except (binascii.Error, ValueError):
            raise BackupAuthenticationFailed from None
        if nonce_bytes != index.to_bytes(_NONCE_BYTES, "big"):
            raise BackupAuthenticationFailed
        digest = str(entry["ciphertext_sha256"])
        plaintext_bytes = _positive_int(entry["plaintext_bytes"])
        ciphertext_bytes = _positive_int(entry["ciphertext_bytes"])
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None or ciphertext_bytes != plaintext_bytes + _TAG_BYTES:
            raise BackupAuthenticationFailed
        return BackupChunk(index, nonce, plaintext_bytes, digest, ciphertext_bytes)

    @staticmethod
    def _verify_ciphertext_pass(chunks_fd: int, manifest: BackupManifest) -> tuple[tuple[int, int, int], ...]:
        identities: list[tuple[int, int, int]] = []
        for chunk in manifest.chunks:
            descriptor = os.open(_chunk_name(chunk.index), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=chunks_fd)
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != chunk.ciphertext_bytes:
                    raise BackupAuthenticationFailed
                digest = hashlib.sha256()
                while block := handle.read(_READ_BLOCK_BYTES):
                    digest.update(block)
            if not hmac.compare_digest(digest.hexdigest(), chunk.ciphertext_sha256):
                raise BackupAuthenticationFailed
            identities.append((info.st_dev, info.st_ino, info.st_size))
        return tuple(identities)

    def _decrypt_to_spool(
        self,
        chunks_fd: int,
        manifest: BackupManifest,
        identities: tuple[tuple[int, int, int], ...],
        spool: BinaryIO,
    ) -> None:
        salt = _archive_salt(manifest.archive_salt)
        chunk_key = _derive_key(self._key, _CHUNK_INFO, salt=salt, archive_id=manifest.archive_id)
        for chunk, identity in zip(manifest.chunks, identities, strict=True):
            descriptor = os.open(_chunk_name(chunk.index), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=chunks_fd)
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (info.st_dev, info.st_ino, info.st_size) != identity:
                    raise BackupAuthenticationFailed
                ciphertext = _read_fd_all(handle, limit=manifest.chunk_bytes + _TAG_BYTES)
            try:
                plaintext = AESGCM(chunk_key).decrypt(
                    chunk.index.to_bytes(_NONCE_BYTES, "big"),
                    ciphertext,
                    _aad(manifest.archive_id, manifest.schema_revision, manifest.source_installation_id, chunk.index),
                )
            except InvalidTag:
                raise BackupAuthenticationFailed from None
            if len(plaintext) != chunk.plaintext_bytes or len(plaintext) > manifest.chunk_bytes:
                raise BackupAuthenticationFailed
            spool.write(plaintext)
        spool.flush()
        if spool.tell() != manifest.total_plaintext_bytes:
            raise BackupAuthenticationFailed


def _positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        raise BackupAuthenticationFailed
    return value


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise BackupAuthenticationFailed
    return value


def pg_dump_argv(database_url: str, *, snapshot_id: str | None = None) -> tuple[str, ...]:
    """Return fixed, password-free argv; libpq connection data lives in env/passfile."""

    if not isinstance(database_url, str) or not database_url:
        raise ValueError("database_url is required")
    argv = ["pg_dump", "--format=custom", "--no-owner", "--no-acl"]
    if snapshot_id is not None:
        if not isinstance(snapshot_id, str) or not snapshot_id or any(character.isspace() for character in snapshot_id):
            raise ValueError("invalid exported snapshot")
        argv.append(f"--snapshot={snapshot_id}")
    return tuple(argv)


def _base_subprocess_env() -> dict[str, str]:
    return {name: value for name in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if (value := os.environ.get(name)) is not None}


def _escape_pgpass(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise BackupCommandFailed
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _write_fd_all(descriptor: int, value: bytes) -> None:
    remaining = memoryview(value)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "short passfile write")
        remaining = remaining[written:]


def _create_libpq_invocation(database_url: str, directory: Path) -> _LibpqInvocation:
    try:
        parsed = make_url(database_url)
    except Exception:
        raise BackupCommandFailed from None
    if parsed.get_backend_name() != "postgresql" or not parsed.database:
        raise BackupCommandFailed
    env = _base_subprocess_env()
    host = parsed.host or ""
    port = str(parsed.port or 5432)
    username = parsed.username or ""
    database = parsed.database
    env.update({"PGHOST": host, "PGPORT": port, "PGUSER": username, "PGDATABASE": database, "PGAPPNAME": "deerflow-backup"})
    for key, value in parsed.query.items():
        target = _LIBPQ_QUERY_ENV.get(key)
        if target is None or not isinstance(value, str):
            raise BackupCommandFailed
        env[target] = value
    pinned = _open_external_directory(directory)
    invocation = _LibpqInvocation(env=env, passfile=None, directory=pinned)
    if parsed.password is not None:
        if os.name != "posix" or not os.path.isdir("/dev/fd"):
            _finish_libpq_invocation_release(invocation)
            raise BackupCommandFailed
        passfile_name = f".pgpass.{uuid.uuid4().hex}"
        try:
            passfile_descriptor = os.open(
                passfile_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=pinned.descriptor,
            )
        except BaseException:
            _finish_libpq_invocation_release(invocation)
            raise
        invocation.passfile = pinned.path / passfile_name
        invocation.passfile_name = passfile_name
        invocation.passfile_descriptor = passfile_descriptor
        try:
            info = os.fstat(passfile_descriptor)
            invocation.passfile_identity = (info.st_dev, info.st_ino)
            line = ":".join(_escape_pgpass(value) for value in (host or "*", port, database, username or "*", parsed.password)) + "\n"
            _write_fd_all(passfile_descriptor, line.encode("utf-8"))
            os.fsync(passfile_descriptor)
            os.lseek(passfile_descriptor, 0, os.SEEK_SET)
            _fsync_directory(pinned.descriptor)
        except BaseException:
            _release_libpq_invocation_retrying_transient_errors(invocation)
            raise
        env["PGPASSFILE"] = f"/dev/fd/{passfile_descriptor}"
    return invocation


def _finish_libpq_invocation_release(invocation: _LibpqInvocation) -> None:
    descriptor = invocation.passfile_descriptor
    if descriptor is not None:
        try:
            os.close(descriptor)
        finally:
            invocation.passfile_descriptor = None
    invocation.passfile_name = None
    invocation.passfile_identity = None
    invocation.directory.close()


def _release_libpq_invocation(invocation: _LibpqInvocation) -> None:
    passfile_name = invocation.passfile_name
    if passfile_name is None:
        _finish_libpq_invocation_release(invocation)
        return
    directory_descriptor = invocation.directory.descriptor
    if directory_descriptor < 0:
        raise BackupCommandFailed
    passfile_identity = invocation.passfile_identity
    if passfile_identity is None:
        passfile_descriptor = invocation.passfile_descriptor
        if passfile_descriptor is None:
            raise BackupCommandFailed
        owned_info = os.fstat(passfile_descriptor)
        if not stat.S_ISREG(owned_info.st_mode):
            raise BackupCommandFailed
        passfile_identity = (owned_info.st_dev, owned_info.st_ino)
        invocation.passfile_identity = passfile_identity
    try:
        info = os.stat(passfile_name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        _fsync_directory(directory_descriptor)
        _finish_libpq_invocation_release(invocation)
        return
    if not stat.S_ISREG(info.st_mode) or passfile_identity != (info.st_dev, info.st_ino):
        _finish_libpq_invocation_release(invocation)
        raise BackupCommandFailed
    os.unlink(passfile_name, dir_fd=directory_descriptor)
    _fsync_directory(directory_descriptor)
    _finish_libpq_invocation_release(invocation)


def _release_libpq_invocation_retrying_transient_errors(invocation: _LibpqInvocation) -> None:
    for attempt in range(2):
        try:
            _release_libpq_invocation(invocation)
            return
        except OSError:
            if attempt == 1:
                raise


def _asyncpg_url(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@asynccontextmanager
async def _exported_snapshot(database_url: str) -> AsyncIterator[BackupSnapshot]:
    connection = None
    transaction = None
    try:
        try:
            import asyncpg

            connection = await asyncpg.connect(_asyncpg_url(database_url))
            transaction = connection.transaction(isolation="repeatable_read", readonly=True)
            await transaction.start()
            snapshot_id = await connection.fetchval("SELECT pg_export_snapshot()")
            row = await connection.fetchrow(
                """SELECT
                       (SELECT version_num FROM alembic_version LIMIT 1) AS schema_revision,
                       (SELECT system_identifier::text FROM pg_control_system()) AS system_identifier,
                       (SELECT oid::bigint FROM pg_database WHERE datname = current_database()) AS database_oid,
                       COALESCE((SELECT MAX(high_watermark) FROM thread_event_sequences), 0)::bigint AS database_high_watermark,
                       (SELECT COUNT(*)::bigint FROM deletion_tombstones) AS tombstone_count,
                       COALESCE((SELECT MIN(journal_sequence) FROM deletion_tombstones), 0)::bigint AS tombstone_min,
                       COALESCE((SELECT MAX(journal_sequence) FROM deletion_tombstones), 0)::bigint AS tombstone_max,
                       (SELECT COUNT(*)::bigint
                          FROM pg_class
                         WHERE relnamespace = current_schema()::regnamespace
                           AND relkind IN ('r', 'p')) AS table_count"""
            )
            schema_revision = str(row["schema_revision"])
            system_identifier = str(row["system_identifier"])
            database_oid = int(row["database_oid"])
            database_high_watermark = int(row["database_high_watermark"])
            tombstone_count = int(row["tombstone_count"])
            tombstone_min = int(row["tombstone_min"])
            tombstone_max = int(row["tombstone_max"])
            table_count = int(row["table_count"])
            if (
                not snapshot_id
                or _SCHEMA_REVISION.fullmatch(schema_revision) is None
                or not system_identifier.isdigit()
                or database_oid < 1
                or database_high_watermark < 0
                or table_count < 1
                or tombstone_count < 0
                or (tombstone_count == 0 and (tombstone_min != 0 or tombstone_max != 0))
                or (tombstone_count > 0 and (tombstone_min != 1 or tombstone_count != tombstone_max))
            ):
                raise BackupCommandFailed
            source_payload = _SOURCE_ID_DOMAIN + system_identifier.encode("ascii") + b"\x00" + str(database_oid).encode("ascii")
            snapshot = BackupSnapshot(
                snapshot_id=str(snapshot_id),
                schema_revision=schema_revision,
                source_installation_id=hashlib.sha256(source_payload).hexdigest(),
                database_high_watermark=database_high_watermark,
                tombstone_journal_sequence=tombstone_max,
                table_count=table_count,
            )
        except asyncio.CancelledError:
            raise
        except BackupCommandFailed:
            raise
        except Exception:
            raise BackupCommandFailed from None
        yield snapshot
    finally:
        cleanup_task = asyncio.create_task(_release_exported_snapshot(transaction, connection))
        await _await_shielded(cleanup_task)


async def _release_exported_snapshot(transaction: object | None, connection: object | None) -> None:
    cleanup_failed = False
    if transaction is not None:
        try:
            await transaction.rollback()
        except BaseException:
            cleanup_failed = True
    if connection is not None:
        try:
            await connection.close()
        except BaseException:
            cleanup_failed = True
    if cleanup_failed:
        raise BackupCommandFailed from None


async def _read_pg_dump_version() -> str:
    process = await asyncio.create_subprocess_exec(
        "pg_dump",
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_base_subprocess_env(),
    )
    if process.stdout is None or process.stderr is None:
        await _terminate_process(process)
        raise BackupCommandFailed
    stdout_task = asyncio.create_task(_capture_bounded(process.stdout, _VERSION_OUTPUT_LIMIT))
    stderr_task = asyncio.create_task(_drain_stderr(process.stderr))
    try:
        returncode = await process.wait()
        stdout, overflow = await stdout_task
        await stderr_task
        try:
            version = stdout.decode("ascii").strip()
        except UnicodeDecodeError:
            raise BackupCommandFailed from None
        if returncode != 0 or overflow or _PG_DUMP_VERSION.fullmatch(version) is None:
            raise BackupCommandFailed
        return version
    except BaseException:
        await _terminate_process(process)
        await _cancel_and_await(stdout_task, stderr_task)
        raise


async def _capture_bounded(stream: object, limit: int) -> tuple[bytes, bool]:
    read = getattr(stream, "read")
    captured = bytearray()
    overflow = False
    while block := await read(_READ_BLOCK_BYTES):
        remaining = limit - len(captured)
        if remaining > 0:
            captured.extend(block[:remaining])
        overflow = overflow or len(block) > remaining
    return bytes(captured), overflow


async def _drain_stderr(stream: object) -> None:
    read = getattr(stream, "read")
    while await read(_READ_BLOCK_BYTES):
        pass


async def _cancel_and_await(*tasks: asyncio.Task[object]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _terminate_process(process: object) -> None:
    if getattr(process, "returncode", None) is not None:
        return
    try:
        process.terminate()
    except (AttributeError, ProcessLookupError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_TERM_TIMEOUT_SECONDS)
        return
    except (TimeoutError, ProcessLookupError):
        pass
    try:
        process.kill()
    except (AttributeError, ProcessLookupError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_TERM_TIMEOUT_SECONDS)
    except (TimeoutError, ProcessLookupError):
        pass


async def _await_task_through_cancellation(task: asyncio.Task[object]) -> tuple[object, bool]:
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


async def _await_shielded(task: asyncio.Task[object]) -> object:
    result, cancelled = await _await_task_through_cancellation(task)
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _run_owned_writer_operation(operation: Callable[..., object], *args: object, **kwargs: object) -> object:
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    return await _await_shielded(task)


async def _acquire_libpq_invocation(database_url: str, directory: Path) -> _LibpqInvocation:
    creation_task = asyncio.create_task(asyncio.to_thread(_create_libpq_invocation, database_url, directory))
    result, cancelled = await _await_task_through_cancellation(creation_task)
    if not isinstance(result, _LibpqInvocation):
        raise BackupCommandFailed
    if cancelled:
        try:
            await _release_owned_libpq_invocation(result)
        except BaseException:
            pass
        raise asyncio.CancelledError
    return result


async def _release_owned_libpq_invocation(invocation: _LibpqInvocation) -> None:
    cleanup_task = asyncio.create_task(asyncio.to_thread(_release_libpq_invocation_retrying_transient_errors, invocation))
    await _await_shielded(cleanup_task)


async def _cleanup_backup(process: object | None, stderr_task: asyncio.Task[object] | None, writer: BackupArchiveWriter | None) -> None:
    if process is not None:
        await _terminate_process(process)
    if stderr_task is not None:
        await _cancel_and_await(stderr_task)
    if writer is not None:
        try:
            await asyncio.to_thread(writer.abort)
        finally:
            await asyncio.to_thread(writer.close)


async def _close_backup_resources(process: object | None, stderr_task: asyncio.Task[object] | None, writer: BackupArchiveWriter | None) -> None:
    if process is not None:
        await _terminate_process(process)
    if stderr_task is not None:
        await _cancel_and_await(stderr_task)
    if writer is not None:
        await asyncio.to_thread(writer.close)


async def _dispose_audit_engine(engine: object, *, committed: bool) -> None:
    try:
        await engine.dispose()
    except BaseException:
        if not committed:
            raise


async def _record_backup_audit(database_url: str, manifest: BackupManifest) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.audit.service import AuditService, _bind_operator_audit_process
    from app.audit.sinks import TrustedOperationAuditSink
    from app.reliability.owner_refs import AuditHmacKeyring
    from deerflow.config.database_config import DatabaseConfig

    engine = create_async_engine(DatabaseConfig(url=database_url).sqlalchemy_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    committed = False
    try:
        service = AuditService(factory, AuditHmacKeyring.from_environment())
        sink = TrustedOperationAuditSink(service, process_context=_bind_operator_audit_process(service))
        async with factory() as session, session.begin():
            await sink.backup_created(
                session,
                backup_id=uuid.UUID(manifest.archive_id),
                table_count=manifest.table_count,
                tombstone_high_watermark=manifest.tombstone_journal_sequence,
                request_id=f"backup-{uuid.uuid4()}",
            )
        committed = True
    finally:
        await _dispose_audit_engine(engine, committed=committed)


async def create_backup(config: BackupConfig) -> BackupManifest:
    """Create one audited archive from a single exported PostgreSQL snapshot."""

    _validate_key_separation(config.key, config.database_url)
    invocation = await _acquire_libpq_invocation(config.database_url, config.output.parent)
    process = None
    stderr_task: asyncio.Task[object] | None = None
    writer: BackupArchiveWriter | None = None
    invocation_released = False
    audit_committed = False
    try:
        pg_dump_version = await _read_pg_dump_version()
        async with _exported_snapshot(config.database_url) as snapshot:
            process = await asyncio.create_subprocess_exec(
                *pg_dump_argv(config.database_url, snapshot_id=snapshot.snapshot_id),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(invocation.env),
                pass_fds=invocation.pass_fds,
            )
            if process.stdout is None or process.stderr is None:
                raise BackupCommandFailed
            stderr_task = asyncio.create_task(_drain_stderr(process.stderr))
            writer = BackupArchiveWriter.atomic(
                config.output,
                config.key,
                chunk_bytes=config.chunk_bytes,
                source_installation_id=snapshot.source_installation_id,
                schema_revision=snapshot.schema_revision,
                pg_dump_version=pg_dump_version,
                table_count=snapshot.table_count,
                archive_id=config.archive_id,
                _parent_directory=invocation.directory.duplicate(),
            )
            await _run_owned_writer_operation(writer.__enter__)
            buffer = bytearray()
            while data := await process.stdout.read(min(_READ_BLOCK_BYTES, config.chunk_bytes)):
                buffer.extend(data)
                while len(buffer) >= config.chunk_bytes:
                    plaintext = bytes(buffer[: config.chunk_bytes])
                    del buffer[: config.chunk_bytes]
                    await _run_owned_writer_operation(writer.write_chunk, plaintext)
            if buffer:
                await _run_owned_writer_operation(writer.write_chunk, bytes(buffer))
            returncode = await process.wait()
            await stderr_task
            if returncode != 0:
                raise BackupCommandFailed
        await _release_owned_libpq_invocation(invocation)
        invocation_released = True
        finalize_task = asyncio.create_task(
            asyncio.to_thread(
                writer.finalize,
                database_high_watermark=snapshot.database_high_watermark,
                tombstone_journal_sequence=snapshot.tombstone_journal_sequence,
            )
        )
        manifest = await _await_shielded(finalize_task)
        if not isinstance(manifest, BackupManifest):
            raise BackupCommandFailed
        audit_task = asyncio.create_task(_record_backup_audit(config.database_url, manifest))
        _, audit_cancelled = await _await_task_through_cancellation(audit_task)
        audit_committed = True
        close_task = asyncio.create_task(asyncio.to_thread(writer.close))
        _, close_cancelled = await _await_task_through_cancellation(close_task)
        if audit_cancelled or close_cancelled:
            raise asyncio.CancelledError
        return manifest
    except asyncio.CancelledError:
        cleanup = _close_backup_resources if audit_committed else _cleanup_backup
        cleanup_task = asyncio.create_task(cleanup(process, stderr_task, writer))
        try:
            await _await_shielded(cleanup_task)
        except asyncio.CancelledError:
            pass
        raise
    except FileExistsError:
        await _cleanup_backup(process, stderr_task, writer)
        raise
    except BaseException:
        if audit_committed:
            await _close_backup_resources(process, stderr_task, writer)
        else:
            await _cleanup_backup(process, stderr_task, writer)
        raise BackupCommandFailed from None
    finally:
        if not invocation_released:
            try:
                await _release_owned_libpq_invocation(invocation)
            except asyncio.CancelledError:
                pass
