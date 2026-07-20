from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from scripts.release_acceptance.contracts import canonical_digest, canonical_json_bytes

_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,126}\.json$")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_FORBIDDEN_KEY_FRAGMENTS = (
    "prompt",
    "message",
    "memory",
    "output",
    "content",
    "payload",
    "exception",
    "database_url",
    "secret",
    "cookie",
    "credential",
    "private_key",
    "access_token",
    "refresh_token",
    "nonce",
    "ciphertext",
)
_FORBIDDEN_EXACT_KEYS = frozenset(
    {
        "account_id",
        "project_id",
        "owner_user_id",
        "user_id",
        "membership_id",
        "thread_id",
        "run_id",
        "file_id",
        "artifact_id",
        "resource_id",
        "path",
        "raw_path",
        "file_path",
        "absolute_path",
    }
)


class ForbiddenEvidenceField(ValueError):
    """Evidence contains a forbidden key or raw absolute path."""


class UnsafeEvidenceRoot(ValueError):
    """Evidence root is not an owned regular directory."""


class UnsafeEvidencePath(ValueError):
    """Evidence filename escapes the invocation directory."""


@dataclass(frozen=True, slots=True)
class EvidenceArtifact:
    name: str
    sha256: str
    size_bytes: int


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_digest(value: Mapping[str, object] | BaseModel) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    payload.pop("manifest_sha256", None)
    return canonical_digest(payload)


class EvidenceWriter:
    def __init__(self, output_root: Path, *, acceptance_run_id: uuid.UUID) -> None:
        self.output_root = output_root
        self.acceptance_run_id = acceptance_run_id
        self._run_identity: tuple[int, int] | None = None

    @property
    def run_directory(self) -> Path:
        return self.output_root / str(self.acceptance_run_id)

    def prepare(self) -> Path:
        run_directory, _created = self._prepare_run_directory()
        return run_directory

    def write(self, evidence: BaseModel, *, name: str = "manifest.json") -> EvidenceArtifact:
        return self.write_json(name, evidence)

    def write_json(self, name: str, value: object) -> EvidenceArtifact:
        self._validate_name(name)
        serializable = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        _reject_forbidden(serializable)
        encoded = canonical_json_bytes(serializable)
        run_directory, created = self._prepare_run_directory()
        temporary = run_directory / f".{name}.{uuid.uuid4().hex}.tmp"
        target = run_directory / name
        descriptor = -1
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            self._fsync_directory(run_directory)
            return EvidenceArtifact(name=name, sha256=hashlib.sha256(encoded).hexdigest(), size_bytes=len(encoded))
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            if created:
                try:
                    run_directory.rmdir()
                except OSError:
                    pass
                else:
                    self._run_identity = None
            raise

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _SAFE_NAME.fullmatch(name) or Path(name).name != name:
            raise UnsafeEvidencePath("UNSAFE_EVIDENCE_PATH")

    def _prepare_run_directory(self) -> tuple[Path, bool]:
        root = self.output_root
        try:
            root_info = os.lstat(root)
        except FileNotFoundError:
            parent_info = os.lstat(root.parent)
            if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
                raise UnsafeEvidenceRoot("UNSAFE_EVIDENCE_ROOT") from None
            root.mkdir(mode=0o700)
            root_info = os.lstat(root)
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            raise UnsafeEvidenceRoot("UNSAFE_EVIDENCE_ROOT")
        run_directory = self.run_directory
        created = False
        if self._run_identity is None:
            try:
                run_directory.mkdir(mode=0o700)
            except FileExistsError:
                raise UnsafeEvidenceRoot("EVIDENCE_RUN_ALREADY_EXISTS") from None
            info = os.lstat(run_directory)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise UnsafeEvidenceRoot("UNSAFE_EVIDENCE_RUN_ROOT")
            self._run_identity = (info.st_dev, info.st_ino)
            created = True
            self._fsync_directory(root)
        else:
            info = os.lstat(run_directory)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != self._run_identity:
                raise UnsafeEvidenceRoot("EVIDENCE_RUN_IDENTITY_MISMATCH")
        return run_directory, created

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _reject_forbidden(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ForbiddenEvidenceField("EVIDENCE_KEY_NOT_STRING")
            normalized = key.casefold().replace("-", "_")
            if normalized in _FORBIDDEN_EXACT_KEYS or any(fragment in normalized for fragment in _FORBIDDEN_KEY_FRAGMENTS) or normalized.endswith("_path"):
                raise ForbiddenEvidenceField("FORBIDDEN_EVIDENCE_FIELD")
            _reject_forbidden(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_forbidden(item)
        return
    if isinstance(value, str) and (value.startswith(("/", "~/", "~\\")) or _WINDOWS_ABSOLUTE.match(value)):
        raise ForbiddenEvidenceField("ABSOLUTE_EVIDENCE_VALUE")
    if isinstance(value, BaseModel):
        _reject_forbidden(value.model_dump(mode="json"))
