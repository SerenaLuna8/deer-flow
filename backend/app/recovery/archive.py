"""Authenticated, atomic on-disk archives for trusted PostgreSQL backups.

The archive format deliberately stores only encrypted pg_dump custom-format
bytes.  It has no restore or tombstone replay responsibilities; those belong
to the follow-on recovery task.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CHUNK_SIZE = 1_048_576
SCHEMA_REVISION = 1
_NONCE_BYTES = 12
_TAG_BYTES = 16
_KEY_BYTES = 32
_ARCHIVE_INFO = b"deerflow-recovery-archive-v1"
_CHUNK_INFO = b"deerflow-recovery-chunk-v1"
_MANIFEST_INFO = b"deerflow-recovery-manifest-v1"


class BackupAuthenticationFailed(RuntimeError):
    """Archive authenticity or integrity could not be verified."""

    def __init__(self) -> None:
        super().__init__("BACKUP_AUTHENTICATION_FAILED")


class BackupKeyMissing(RuntimeError):
    """The independent operator backup key is absent."""

    def __init__(self) -> None:
        super().__init__("BACKUP_KEY_REQUIRED")


class BackupKeyInvalid(RuntimeError):
    """The independent operator backup key is malformed."""

    def __init__(self) -> None:
        super().__init__("BACKUP_KEY_INVALID")


class BackupCommandFailed(RuntimeError):
    """pg_dump failed; no stderr/stdout or connection details are retained."""

    def __init__(self) -> None:
        super().__init__("BACKUP_COMMAND_FAILED")


@dataclass(frozen=True)
class BackupChunk:
    index: int
    nonce: str
    ciphertext_sha256: str
    ciphertext_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "nonce": self.nonce,
            "ciphertext_sha256": self.ciphertext_sha256,
            "ciphertext_bytes": self.ciphertext_bytes,
        }


@dataclass(frozen=True)
class BackupManifest:
    archive_id: str
    schema_revision: int
    source_installation_id: str
    chunk_bytes: int
    chunks: tuple[BackupChunk, ...]
    database_high_watermark: int
    tombstone_journal_sequence: int
    tool: str = "pg_dump --format=custom --no-owner --no-acl"

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def as_dict(self) -> dict[str, object]:
        return {
            "archive_id": self.archive_id,
            "schema_revision": self.schema_revision,
            "source_installation_id": self.source_installation_id,
            "chunk_bytes": self.chunk_bytes,
            "chunks": [chunk.as_dict() for chunk in self.chunks],
            "database_high_watermark": self.database_high_watermark,
            "tombstone_journal_sequence": self.tombstone_journal_sequence,
            "tool": self.tool,
        }


@dataclass(frozen=True)
class BackupConfig:
    database_url: str
    output: Path
    key: bytes
    source_installation_id: str
    chunk_bytes: int = CHUNK_SIZE
    database_high_watermark: int | None = None
    tombstone_journal_sequence: int | None = None
    archive_id: str | None = None

    def __post_init__(self) -> None:
        _validate_key(self.key)
        if self.chunk_bytes < 1:
            raise ValueError("chunk_bytes must be positive")
        if not self.source_installation_id:
            raise ValueError("source_installation_id is required")
        if (self.database_high_watermark is not None and self.database_high_watermark < 0) or (self.tombstone_journal_sequence is not None and self.tombstone_journal_sequence < 0):
            raise ValueError("recovery high-watermarks must be non-negative")


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
        raise BackupKeyInvalid


def load_backup_key(value: str | None = None) -> bytes:
    """Load the only accepted backup key source, without exposing its value."""

    encoded = value if value is not None else os.environ.get("DEER_FLOW_BACKUP_KEY")
    if not encoded:
        raise BackupKeyMissing
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise BackupKeyInvalid from None
    _validate_key(key)
    return key


def _derive_key(master_key: bytes, purpose: bytes) -> bytes:
    _validate_key(master_key)
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_BYTES, salt=None, info=_ARCHIVE_INFO + b":" + purpose).derive(master_key)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _aad(archive_id: str, source_installation_id: str, index: int) -> bytes:
    return _canonical_json(
        {
            "archive_id": archive_id,
            "chunk_index": index,
            "schema_revision": SCHEMA_REVISION,
            "source_installation_id": source_installation_id,
        }
    )


def _manifest_signature(key: bytes, manifest: dict[str, object]) -> str:
    return hmac.new(_derive_key(key, _MANIFEST_INFO), _canonical_json(manifest), hashlib.sha256).hexdigest()


def _chunk_name(index: int) -> str:
    return f"{index:08d}.bin"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class BackupArchiveWriter:
    """Synchronous archive writer; async callers must invoke it via to_thread."""

    def __init__(self, output: Path, key: bytes, *, chunk_bytes: int = CHUNK_SIZE, source_installation_id: str, archive_id: str | None = None) -> None:
        _validate_key(key)
        if chunk_bytes < 1 or not source_installation_id:
            raise ValueError("invalid backup archive configuration")
        self.output = Path(output)
        self._key = key
        self._chunk_bytes = chunk_bytes
        self._source_installation_id = source_installation_id
        self._archive_id = archive_id or str(uuid.uuid4())
        self._staging = self.output.parent / f".{self.output.name}.{uuid.uuid4().hex}.part"
        self._chunks: list[BackupChunk] = []
        self._finalized = False

    @classmethod
    def atomic(cls, output: Path, key: bytes, *, chunk_bytes: int = CHUNK_SIZE, source_installation_id: str, archive_id: str | None = None) -> BackupArchiveWriter:
        return cls(output, key, chunk_bytes=chunk_bytes, source_installation_id=source_installation_id, archive_id=archive_id)

    def __enter__(self) -> BackupArchiveWriter:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.output.parent, 0o700)
        if self.output.exists():
            raise FileExistsError("BACKUP_OUTPUT_EXISTS")
        self._staging.mkdir(mode=0o700)
        os.chmod(self._staging, 0o700)
        chunks = self._staging / "chunks"
        chunks.mkdir(mode=0o700)
        os.chmod(chunks, 0o700)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self._finalized:
            self.abort()

    def abort(self) -> None:
        shutil.rmtree(self._staging, ignore_errors=True)

    def write_chunk(self, plaintext: bytes) -> None:
        if self._finalized or not plaintext or len(plaintext) > self._chunk_bytes:
            raise ValueError("invalid backup chunk")
        index = len(self._chunks)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(_derive_key(self._key, _CHUNK_INFO)).encrypt(nonce, plaintext, _aad(self._archive_id, self._source_installation_id, index))
        chunk_path = self._staging / "chunks" / _chunk_name(index)
        descriptor = os.open(chunk_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
        self._chunks.append(BackupChunk(index=index, nonce=base64.b64encode(nonce).decode("ascii"), ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest(), ciphertext_bytes=len(ciphertext)))

    def finalize(self, *, database_high_watermark: int, tombstone_journal_sequence: int) -> BackupManifest:
        if self._finalized or database_high_watermark < 0 or tombstone_journal_sequence < 0:
            raise ValueError("invalid backup finalization")
        manifest = BackupManifest(
            archive_id=self._archive_id,
            schema_revision=SCHEMA_REVISION,
            source_installation_id=self._source_installation_id,
            chunk_bytes=self._chunk_bytes,
            chunks=tuple(self._chunks),
            database_high_watermark=database_high_watermark,
            tombstone_journal_sequence=tombstone_journal_sequence,
        )
        body = manifest.as_dict()
        envelope = {"manifest": body, "signature": _manifest_signature(self._key, body)}
        manifest_path = self._staging / "manifest.json"
        descriptor = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(_canonical_json(envelope))
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        os.replace(self._staging, self.output)
        _fsync_directory(self.output.parent)
        self._finalized = True
        return manifest


class BackupArchiveReader:
    """Fail-closed reader which releases no plaintext before full verification."""

    def __init__(self, key: bytes) -> None:
        _validate_key(key)
        self._key = key

    def verified_chunks(self, archive: Path) -> Iterator[bytes]:
        try:
            manifest = self._load_manifest(Path(archive))
            plaintext = self._verify_all_chunks(Path(archive), manifest)
        except BackupAuthenticationFailed:
            raise
        except Exception:
            raise BackupAuthenticationFailed from None
        yield from plaintext

    def _load_manifest(self, archive: Path) -> BackupManifest:
        envelope = json.loads((archive / "manifest.json").read_bytes())
        if not isinstance(envelope, dict) or set(envelope) != {"manifest", "signature"}:
            raise BackupAuthenticationFailed
        body, signature = envelope["manifest"], envelope["signature"]
        if not isinstance(body, dict) or not isinstance(signature, str) or not hmac.compare_digest(signature, _manifest_signature(self._key, body)):
            raise BackupAuthenticationFailed
        chunks_data = body.get("chunks")
        if not isinstance(chunks_data, list) or body.get("schema_revision") != SCHEMA_REVISION:
            raise BackupAuthenticationFailed
        chunks = tuple(
            BackupChunk(
                index=int(entry["index"]),
                nonce=str(entry["nonce"]),
                ciphertext_sha256=str(entry["ciphertext_sha256"]),
                ciphertext_bytes=int(entry["ciphertext_bytes"]),
            )
            for entry in chunks_data
            if isinstance(entry, dict)
        )
        if len(chunks) != len(chunks_data) or [chunk.index for chunk in chunks] != list(range(len(chunks))):
            raise BackupAuthenticationFailed
        return BackupManifest(
            archive_id=str(body["archive_id"]),
            schema_revision=int(body["schema_revision"]),
            source_installation_id=str(body["source_installation_id"]),
            chunk_bytes=int(body["chunk_bytes"]),
            chunks=chunks,
            database_high_watermark=int(body["database_high_watermark"]),
            tombstone_journal_sequence=int(body["tombstone_journal_sequence"]),
            tool=str(body["tool"]),
        )

    def _verify_all_chunks(self, archive: Path, manifest: BackupManifest) -> list[bytes]:
        plaintext: list[bytes] = []
        nonces: set[str] = set()
        for chunk in manifest.chunks:
            if chunk.nonce in nonces:
                raise BackupAuthenticationFailed
            nonces.add(chunk.nonce)
            ciphertext = (archive / "chunks" / _chunk_name(chunk.index)).read_bytes()
            if len(ciphertext) != chunk.ciphertext_bytes or not hmac.compare_digest(hashlib.sha256(ciphertext).hexdigest(), chunk.ciphertext_sha256):
                raise BackupAuthenticationFailed
            nonce = base64.b64decode(chunk.nonce, validate=True)
            if len(nonce) != _NONCE_BYTES:
                raise BackupAuthenticationFailed
            try:
                decrypted = AESGCM(_derive_key(self._key, _CHUNK_INFO)).decrypt(nonce, ciphertext, _aad(manifest.archive_id, manifest.source_installation_id, chunk.index))
            except InvalidTag:
                raise BackupAuthenticationFailed from None
            if not decrypted or len(decrypted) > manifest.chunk_bytes:
                raise BackupAuthenticationFailed
            plaintext.append(decrypted)
        return plaintext


def pg_dump_argv(database_url: str) -> tuple[str, str, str, str, str]:
    """Fixed argv form; callers must use create_subprocess_exec, never a shell."""

    return ("pg_dump", "--format=custom", "--no-owner", "--no-acl", database_url)


async def read_high_watermarks(database_url: str) -> tuple[int, int]:
    """Read public recovery cursors before pg_dump begins its snapshot."""

    try:
        import asyncpg

        connection = await asyncpg.connect(database_url)
        try:
            row = await connection.fetchrow(
                "SELECT COALESCE((SELECT MAX(high_watermark) FROM thread_event_sequences), 0) AS database_high_watermark, COALESCE((SELECT MAX(journal_sequence) FROM deletion_tombstones), 0) AS tombstone_journal_sequence"
            )
            return int(row["database_high_watermark"]), int(row["tombstone_journal_sequence"])
        finally:
            await connection.close()
    except Exception:
        raise BackupCommandFailed from None


async def create_backup(config: BackupConfig) -> BackupManifest:
    """Stream pg_dump into an authenticated archive without blocking the loop."""

    if config.database_high_watermark is None or config.tombstone_journal_sequence is None:
        database_high_watermark, tombstone_journal_sequence = await read_high_watermarks(config.database_url)
    else:
        database_high_watermark = config.database_high_watermark
        tombstone_journal_sequence = config.tombstone_journal_sequence
    process = await asyncio.create_subprocess_exec(*pg_dump_argv(config.database_url), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    if process.stdout is None or process.stderr is None:
        raise BackupCommandFailed
    stderr_task = asyncio.create_task(process.stderr.read())
    writer = BackupArchiveWriter.atomic(
        config.output,
        config.key,
        chunk_bytes=config.chunk_bytes,
        source_installation_id=config.source_installation_id,
        archive_id=config.archive_id,
    )
    try:
        await asyncio.to_thread(writer.__enter__)
        while chunk := await process.stdout.read(config.chunk_bytes):
            await asyncio.to_thread(writer.write_chunk, chunk)
        returncode = await process.wait()
        await stderr_task
        if returncode != 0:
            raise BackupCommandFailed
        return await asyncio.to_thread(
            writer.finalize,
            database_high_watermark=database_high_watermark,
            tombstone_journal_sequence=tombstone_journal_sequence,
        )
    except BaseException:
        await asyncio.to_thread(writer.abort)
        if getattr(process, "returncode", None) is None:
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                try:
                    terminate()
                    await process.wait()
                except ProcessLookupError:
                    pass
        if not stderr_task.done():
            stderr_task.cancel()
        raise
