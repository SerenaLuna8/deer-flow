"""Operator-owned authenticated deletion journal.

Only encrypted replay coordinates are stored on disk.  The public envelope is
monotonic and hash chained so a restore can reject gaps, rollback, truncation,
wrong keys, and record tampering before releasing a coordinate.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.engine import make_url

_FORMAT_VERSION = 1
_KEY_BYTES = 32
_NONCE_BYTES = 12
_SALT_BYTES = 32
_ZERO_DIGEST = "0" * 64
_MAX_LINE_BYTES = 4 * 1024 * 1024
_HEADER_INFO = b"deerflow-tombstone-journal-header-v1\x00"
_ENTRY_INFO = b"deerflow-tombstone-journal-entry-v1\x00"
_REPOSITORY_ROOT = Path(__file__).parents[3]
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class TombstoneJournalUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("TOMBSTONE_JOURNAL_UNAVAILABLE")


class TombstoneAuthenticationFailed(TombstoneJournalUnavailable):
    def __init__(self) -> None:
        RuntimeError.__init__(self, "TOMBSTONE_AUTHENTICATION_FAILED")


class TombstoneSequenceGap(TombstoneJournalUnavailable):
    def __init__(self) -> None:
        RuntimeError.__init__(self, "TOMBSTONE_SEQUENCE_GAP")


class TombstoneSequenceRollback(TombstoneJournalUnavailable):
    def __init__(self) -> None:
        RuntimeError.__init__(self, "TOMBSTONE_SEQUENCE_ROLLBACK")


def _uuid(value: str | uuid.UUID | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValueError("required tombstone coordinate is missing")
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("invalid tombstone coordinate") from None


@dataclass(frozen=True, slots=True)
class TombstoneRecord:
    resource_kind: str
    project_id: str | uuid.UUID | None
    owner_user_id: str | uuid.UUID | None
    file_id: str | uuid.UUID | None
    project_ids: tuple[str | uuid.UUID, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.resource_kind not in {"project", "account", "file"}:
            raise ValueError("unsupported tombstone resource kind")
        if not isinstance(self.idempotency_key, str) or not 1 <= len(self.idempotency_key) <= 256:
            raise ValueError("invalid tombstone idempotency key")
        if self.resource_kind == "file":
            project_id = _uuid(self.project_id, required=True)
            owner_user_id = _uuid(self.owner_user_id, required=True)
            file_id = _uuid(self.file_id, required=True)
            project_ids: tuple[str, ...] = ()
        elif self.resource_kind == "project":
            project_id = _uuid(self.project_id, required=True)
            owner_user_id = None
            file_id = None
            project_ids = ()
            if self.owner_user_id is not None or self.file_id is not None or self.project_ids:
                raise ValueError("project tombstone has invalid coordinates")
        else:
            project_id = None
            owner_user_id = _uuid(self.owner_user_id, required=True)
            file_id = None
            project_ids = tuple(sorted({_uuid(item, required=True) for item in self.project_ids}))
            if self.project_id is not None or self.file_id is not None or not project_ids:
                raise ValueError("account tombstone has invalid coordinates")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "owner_user_id", owner_user_id)
        object.__setattr__(self, "file_id", file_id)
        object.__setattr__(self, "project_ids", project_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "version": _FORMAT_VERSION,
            "resource_kind": self.resource_kind,
            "project_id": self.project_id,
            "owner_user_id": self.owner_user_id,
            "file_id": self.file_id,
            "project_ids": list(self.project_ids),
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, value: object) -> TombstoneRecord:
        if not isinstance(value, dict) or set(value) != {
            "version",
            "resource_kind",
            "project_id",
            "owner_user_id",
            "file_id",
            "project_ids",
            "idempotency_key",
        }:
            raise TombstoneAuthenticationFailed
        if value["version"] != _FORMAT_VERSION or not isinstance(value["project_ids"], list):
            raise TombstoneAuthenticationFailed
        try:
            return cls(
                resource_kind=value["resource_kind"],
                project_id=value["project_id"],
                owner_user_id=value["owner_user_id"],
                file_id=value["file_id"],
                project_ids=tuple(value["project_ids"]),
                idempotency_key=value["idempotency_key"],
            )
        except (TypeError, ValueError):
            raise TombstoneAuthenticationFailed from None


@dataclass(frozen=True, slots=True)
class TombstoneReceipt:
    sequence: int
    previous_digest: str
    ciphertext_digest: str
    record_digest: str


@dataclass(frozen=True, slots=True)
class TombstoneEntry:
    sequence: int
    previous_digest: str
    ciphertext_digest: str
    record_digest: str
    record: TombstoneRecord

    @property
    def receipt(self) -> TombstoneReceipt:
        return TombstoneReceipt(
            self.sequence,
            self.previous_digest,
            self.ciphertext_digest,
            self.record_digest,
        )


@dataclass(frozen=True, slots=True)
class TombstoneSnapshot:
    journal_id: str
    high_watermark: int
    entries: tuple[TombstoneEntry, ...]


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_b64(value: object, *, length: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise TombstoneAuthenticationFailed
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise TombstoneAuthenticationFailed from None
    if length is not None and len(decoded) != length:
        raise TombstoneAuthenticationFailed
    return decoded


def _derive_entry_key(key: bytes, *, journal_id: str, salt: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=salt,
        info=_ENTRY_INFO + journal_id.encode("ascii"),
    ).derive(key)


def _header_check(key: bytes, body: dict[str, object]) -> str:
    mac_key = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=_decode_b64(body["salt"], length=_SALT_BYTES),
        info=_HEADER_INFO + str(body["journal_id"]).encode("ascii"),
    ).derive(key)
    return hmac.new(mac_key, _canonical(body), hashlib.sha256).hexdigest()


def _safe_external_path(path: Path) -> Path:
    lexical = path.expanduser().absolute()
    try:
        repository = _REPOSITORY_ROOT.resolve(strict=True)
        current = Path(lexical.anchor)
        for part in lexical.parts[1:-1]:
            current /= part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                break
            if stat.S_ISLNK(info.st_mode):
                raise TombstoneJournalUnavailable
        candidate = lexical.resolve(strict=False)
        if candidate == repository or repository in candidate.parents:
            raise TombstoneJournalUnavailable
        candidate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = os.lstat(candidate.parent)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TombstoneJournalUnavailable
        os.chmod(candidate.parent, 0o700)
        try:
            target = os.lstat(candidate)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(target.st_mode) or not stat.S_ISREG(target.st_mode):
                raise TombstoneJournalUnavailable
        return candidate
    except TombstoneJournalUnavailable:
        raise
    except OSError:
        raise TombstoneJournalUnavailable from None


class TombstoneJournal:
    """Synchronous file authority with async offload entrypoints."""

    def __init__(self, path: Path, key: bytes) -> None:
        if not isinstance(path, Path):
            path = Path(path)
        if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
            raise TombstoneJournalUnavailable
        self.path = path
        self._key = key

    def _open_locked(self, *, require_existing: bool) -> tuple[int, Path]:
        path = _safe_external_path(self.path)
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        if not require_existing:
            flags |= os.O_CREAT
        try:
            descriptor = os.open(path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return descriptor, path
        except FileNotFoundError:
            raise TombstoneJournalUnavailable from None
        except OSError:
            raise TombstoneJournalUnavailable from None

    @staticmethod
    def _close_locked(descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _load_locked(self, descriptor: int, path: Path, *, allow_create_header: bool) -> TombstoneSnapshot:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            with os.fdopen(os.dup(descriptor), "rb") as handle:
                lines = []
                for raw in handle:
                    if len(raw) > _MAX_LINE_BYTES or not raw.endswith(b"\n"):
                        raise TombstoneAuthenticationFailed
                    lines.append(raw)
            if not lines:
                if not allow_create_header:
                    raise TombstoneJournalUnavailable
                journal_id = str(uuid.uuid4())
                body = {
                    "version": _FORMAT_VERSION,
                    "journal_id": journal_id,
                    "salt": _b64(os.urandom(_SALT_BYTES)),
                }
                header = {**body, "key_check": _header_check(self._key, body)}
                encoded = _canonical(header) + b"\n"
                self._write_all(descriptor, encoded)
                os.fsync(descriptor)
                parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
                return TombstoneSnapshot(journal_id, 0, ())
            try:
                header = json.loads(lines[0])
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise TombstoneAuthenticationFailed from None
            if not isinstance(header, dict) or set(header) != {"version", "journal_id", "salt", "key_check"}:
                raise TombstoneAuthenticationFailed
            body = {key: header[key] for key in ("version", "journal_id", "salt")}
            if body["version"] != _FORMAT_VERSION:
                raise TombstoneAuthenticationFailed
            try:
                journal_id = str(uuid.UUID(str(body["journal_id"])))
            except (TypeError, ValueError, AttributeError):
                raise TombstoneAuthenticationFailed from None
            _decode_b64(body["salt"], length=_SALT_BYTES)
            if not isinstance(header["key_check"], str) or not hmac.compare_digest(
                header["key_check"],
                _header_check(self._key, body),
            ):
                raise TombstoneAuthenticationFailed
            entry_key = _derive_entry_key(
                self._key,
                journal_id=journal_id,
                salt=_decode_b64(body["salt"], length=_SALT_BYTES),
            )
            entries: list[TombstoneEntry] = []
            previous = _ZERO_DIGEST
            for expected_sequence, raw in enumerate(lines[1:], start=1):
                entry = self._parse_entry(
                    raw,
                    expected_sequence=expected_sequence,
                    expected_previous=previous,
                    journal_id=journal_id,
                    entry_key=entry_key,
                )
                entries.append(entry)
                previous = entry.record_digest
            return TombstoneSnapshot(journal_id, len(entries), tuple(entries))
        except TombstoneJournalUnavailable:
            raise
        except Exception:
            raise TombstoneAuthenticationFailed from None

    @staticmethod
    def _parse_entry(
        raw: bytes,
        *,
        expected_sequence: int,
        expected_previous: str,
        journal_id: str,
        entry_key: bytes,
    ) -> TombstoneEntry:
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TombstoneAuthenticationFailed from None
        keys = {
            "version",
            "sequence",
            "previous_digest",
            "nonce",
            "ciphertext",
            "ciphertext_digest",
            "record_digest",
        }
        if not isinstance(envelope, dict) or set(envelope) != keys:
            raise TombstoneAuthenticationFailed
        if envelope["version"] != _FORMAT_VERSION or type(envelope["sequence"]) is not int:
            raise TombstoneAuthenticationFailed
        if envelope["sequence"] != expected_sequence:
            raise TombstoneSequenceGap
        if envelope["previous_digest"] != expected_previous:
            raise TombstoneSequenceGap
        nonce = _decode_b64(envelope["nonce"], length=_NONCE_BYTES)
        if nonce != expected_sequence.to_bytes(_NONCE_BYTES, "big"):
            raise TombstoneAuthenticationFailed
        ciphertext = _decode_b64(envelope["ciphertext"])
        ciphertext_digest = str(envelope["ciphertext_digest"])
        record_digest = str(envelope["record_digest"])
        if _HEX_DIGEST.fullmatch(ciphertext_digest) is None or _HEX_DIGEST.fullmatch(record_digest) is None:
            raise TombstoneAuthenticationFailed
        if not hmac.compare_digest(ciphertext_digest, hashlib.sha256(ciphertext).hexdigest()):
            raise TombstoneAuthenticationFailed
        digest_body = {key: envelope[key] for key in keys - {"record_digest"}}
        if not hmac.compare_digest(record_digest, hashlib.sha256(_canonical(digest_body)).hexdigest()):
            raise TombstoneAuthenticationFailed
        aad = _canonical(
            {
                "version": _FORMAT_VERSION,
                "journal_id": journal_id,
                "sequence": expected_sequence,
                "previous_digest": expected_previous,
            }
        )
        try:
            plaintext = AESGCM(entry_key).decrypt(nonce, ciphertext, aad)
            record = TombstoneRecord.from_dict(json.loads(plaintext))
        except (InvalidTag, json.JSONDecodeError, UnicodeDecodeError):
            raise TombstoneAuthenticationFailed from None
        return TombstoneEntry(
            expected_sequence,
            expected_previous,
            ciphertext_digest,
            record_digest,
            record,
        )

    @staticmethod
    def _write_all(descriptor: int, value: bytes) -> None:
        view = memoryview(value)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("journal write failed")
            view = view[written:]

    def snapshot(self, *, require_existing: bool = False) -> TombstoneSnapshot:
        descriptor, path = self._open_locked(require_existing=require_existing)
        try:
            return self._load_locked(descriptor, path, allow_create_header=not require_existing)
        finally:
            self._close_locked(descriptor)

    def replay_after(self, high_watermark: int) -> tuple[TombstoneEntry, ...]:
        if type(high_watermark) is not int or high_watermark < 0:
            raise TombstoneSequenceRollback
        snapshot = self.snapshot(require_existing=True)
        if high_watermark > snapshot.high_watermark:
            raise TombstoneSequenceRollback
        return snapshot.entries[high_watermark:]

    def append_and_fsync(
        self,
        record: TombstoneRecord,
        *,
        committed_sequence: int,
    ) -> TombstoneReceipt:
        if type(record) is not TombstoneRecord or type(committed_sequence) is not int or committed_sequence < 0:
            raise TombstoneJournalUnavailable
        descriptor, path = self._open_locked(require_existing=False)
        try:
            snapshot = self._load_locked(descriptor, path, allow_create_header=True)
            for entry in snapshot.entries:
                if entry.record.idempotency_key == record.idempotency_key:
                    if entry.record != record:
                        raise TombstoneJournalUnavailable
                    return entry.receipt
            if snapshot.high_watermark != committed_sequence:
                raise TombstoneJournalUnavailable
            sequence = snapshot.high_watermark + 1
            previous = snapshot.entries[-1].record_digest if snapshot.entries else _ZERO_DIGEST
            os.lseek(descriptor, 0, os.SEEK_SET)
            with os.fdopen(os.dup(descriptor), "rb") as handle:
                header = json.loads(handle.readline())
            journal_id = str(header["journal_id"])
            salt = _decode_b64(header["salt"], length=_SALT_BYTES)
            entry_key = _derive_entry_key(self._key, journal_id=journal_id, salt=salt)
            nonce = sequence.to_bytes(_NONCE_BYTES, "big")
            aad = _canonical(
                {
                    "version": _FORMAT_VERSION,
                    "journal_id": journal_id,
                    "sequence": sequence,
                    "previous_digest": previous,
                }
            )
            ciphertext = AESGCM(entry_key).encrypt(nonce, _canonical(record.as_dict()), aad)
            digest_body: dict[str, object] = {
                "version": _FORMAT_VERSION,
                "sequence": sequence,
                "previous_digest": previous,
                "nonce": _b64(nonce),
                "ciphertext": _b64(ciphertext),
                "ciphertext_digest": hashlib.sha256(ciphertext).hexdigest(),
            }
            record_digest = hashlib.sha256(_canonical(digest_body)).hexdigest()
            encoded = _canonical({**digest_body, "record_digest": record_digest}) + b"\n"
            offset = os.lseek(descriptor, 0, os.SEEK_END)
            try:
                self._write_all(descriptor, encoded)
                os.fsync(descriptor)
            except BaseException:
                try:
                    os.ftruncate(descriptor, offset)
                    os.fsync(descriptor)
                except BaseException:
                    pass
                raise TombstoneJournalUnavailable from None
            return TombstoneReceipt(
                sequence,
                previous,
                str(digest_body["ciphertext_digest"]),
                record_digest,
            )
        finally:
            self._close_locked(descriptor)

    async def append(
        self,
        record: TombstoneRecord,
        *,
        committed_sequence: int,
    ) -> TombstoneReceipt:
        return await asyncio.to_thread(
            self.append_and_fsync,
            record,
            committed_sequence=committed_sequence,
        )


def _decode_environment_key(name: str, value: str | None) -> bytes:
    if not value:
        raise TombstoneJournalUnavailable
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise TombstoneJournalUnavailable from None
    if len(decoded) != _KEY_BYTES:
        raise TombstoneJournalUnavailable
    return decoded


def _known_secret_material() -> tuple[bytes, ...]:
    values: list[bytes] = []
    for name in ("DEER_FLOW_BACKUP_KEY", "AUTH_JWT_SECRET"):
        raw = os.getenv(name)
        if not raw:
            continue
        try:
            values.append(base64.b64decode(raw, validate=True))
        except (binascii.Error, ValueError):
            values.append(raw.encode("utf-8"))
    raw_keyring = os.getenv("DEER_FLOW_AUDIT_KEYRING_JSON")
    if raw_keyring:
        try:
            parsed = json.loads(raw_keyring)
            if isinstance(parsed, dict):
                values.extend(base64.b64decode(value, validate=True) for value in parsed.values() if isinstance(value, str))
        except (json.JSONDecodeError, binascii.Error, ValueError):
            raise TombstoneJournalUnavailable from None
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        try:
            password = make_url(database_url).password
        except Exception:
            raise TombstoneJournalUnavailable from None
        if password:
            values.append(password.encode("utf-8"))
    return tuple(values)


def load_journal_key(value: str | None = None) -> bytes:
    key = _decode_environment_key(
        "DEER_FLOW_RECOVERY_JOURNAL_KEY",
        value if value is not None else os.getenv("DEER_FLOW_RECOVERY_JOURNAL_KEY"),
    )
    if any(hmac.compare_digest(key, secret) for secret in _known_secret_material()):
        raise TombstoneJournalUnavailable
    return key


__all__ = [
    "TombstoneAuthenticationFailed",
    "TombstoneEntry",
    "TombstoneJournal",
    "TombstoneJournalUnavailable",
    "TombstoneReceipt",
    "TombstoneRecord",
    "TombstoneSequenceGap",
    "TombstoneSequenceRollback",
    "TombstoneSnapshot",
    "load_journal_key",
]
