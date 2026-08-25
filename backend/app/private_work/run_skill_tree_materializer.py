"""Bounded, exact Skill tree materialization primitives for Worker execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import threading
import unicodedata
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import ClassVar, Literal, Protocol

from sqlalchemy import and_, exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.asset_runtime_contracts import PrivateSkillManifest
from app.shared_assets.errors import SkillSecretConfigurationInvalid
from app.shared_assets.models import (
    AssetKind,
    AssetScope,
    ResolvedSkillSnapshot,
    SkillSecretRequirementSnapshot,
)
from app.shared_assets.run_snapshot_codec import (
    MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES,
    RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION,
    RunAssetSnapshotInvalid,
    decode_run_asset_snapshot,
    decode_run_skill_version_manifest,
    encoded_run_asset_snapshot_json_size,
)
from app.shared_assets.skill_archive import (
    MAX_SKILL_ARCHIVE_BYTES,
    MAX_SKILL_ARCHIVE_FILE_BYTES,
    MAX_SKILL_ARCHIVE_FILES,
)
from app.shared_assets.skill_secret_policy import (
    parse_skill_secret_declarations,
)
from deerflow.config.worker_config import (
    LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES,
    WorkerConfig,
)
from deerflow.persistence.private_work.model import (
    RunAssetVersionRow,
    RunSkillVersionRefRow,
)
from deerflow.persistence.shared_assets.skill_model import (
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.sandbox.sandbox_provider import (
    NotAcquired,
    Orphaned,
    ProviderRunMountLease,
    Released,
    RunMountReleaseOutcome,
    RunReadonlyMountSource,
    run_readonly_mount_manifest_text,
)
from deerflow.skills.parser import parse_skill_file
from deerflow.skills.types import Skill, SkillCategory
from deerflow.utils.asyncio import joined_to_thread

_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_SKILL_PATH_CHARS = 1024
_MAX_SKILL_PATH_BYTES = _MAX_SKILL_PATH_CHARS * 4
_MAX_SKILL_MEDIA_TYPE_CHARS = 255
_MAX_SKILL_MEDIA_TYPE_BYTES = _MAX_SKILL_MEDIA_TYPE_CHARS * 4
_OWNER_METADATA_FILE = "metadata.json"
_OWNER_METADATA_SCHEMA_VERSION = 2
_RUN_MOUNT_MANIFEST_FILE = ".actweave-run-mount.json"
_RUNTIME_SKILL_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
# Historical v2 stored per-file Base64 and the observed full ppt-master row is
# about 107 MiB. Keep this compatibility read ceiling separate from the 80 MiB
# v3/current writer gate; the legacy adapter still reserves the exclusive
# 1.5 GiB release-calibrated process envelope before PostgreSQL detoast.
MAX_LEGACY_V2_RUN_SKILL_SNAPSHOT_JSON_BYTES = 128 * 1024 * 1024


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_utc_timestamp(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Invalid materialization owner timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc_timestamp(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("Invalid materialization owner timestamp")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        raise ValueError("Invalid materialization owner timestamp") from None
    if _canonical_utc_timestamp(parsed) != value:
        raise ValueError("Invalid materialization owner timestamp")
    return parsed


class MaterializedRunSkillTreeStateError(RuntimeError):
    """Stable programming error for a consumed or mismatched owner token."""


class RunSkillTreeMaterializationStale(RuntimeError):
    """A Run Skill source no longer matches its admitted immutable plan."""


@dataclass(frozen=True, slots=True)
class MaterializationAttemptIdentity:
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    worker_id: uuid.UUID

    def __post_init__(self) -> None:
        if any(type(value) is not uuid.UUID for value in (self.job_id, self.attempt_id, self.worker_id)):
            raise ValueError("Invalid materialization Attempt identity")


@dataclass(frozen=True, slots=True)
class MaterializationAuthorityReadback:
    attempt_identity: MaterializationAttemptIdentity
    plan_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.attempt_identity) is not MaterializationAttemptIdentity or type(self.plan_fingerprint) is not str or _HEX_DIGEST.fullmatch(self.plan_fingerprint) is None:
            raise ValueError("Invalid materialization authority readback")


@dataclass(frozen=True, slots=True, repr=False)
class PinnedSkillVersionPlan:
    dependency_order: int
    scope: AssetScope
    asset_id: uuid.UUID
    version_id: uuid.UUID
    payload_checksum: str
    catalog_generation: int
    dependency_version_ids: tuple[uuid.UUID, ...]
    file_count: int
    content_size_bytes: int
    secret_requirements: tuple[SkillSecretRequirementSnapshot, ...]

    def __post_init__(self) -> None:
        if (
            type(self.dependency_order) is not int
            or self.dependency_order < 0
            or type(self.scope) is not AssetScope
            or type(self.asset_id) is not uuid.UUID
            or type(self.version_id) is not uuid.UUID
            or type(self.payload_checksum) is not str
            or _HEX_DIGEST.fullmatch(self.payload_checksum) is None
            or type(self.catalog_generation) is not int
            or self.catalog_generation < 0
            or type(self.dependency_version_ids) is not tuple
            or any(type(value) is not uuid.UUID for value in self.dependency_version_ids)
            or type(self.file_count) is not int
            or not 1 <= self.file_count <= MAX_SKILL_ARCHIVE_FILES
            or type(self.content_size_bytes) is not int
            or not 0 <= self.content_size_bytes <= MAX_SKILL_ARCHIVE_BYTES
            or type(self.secret_requirements) is not tuple
            or any(type(value) is not SkillSecretRequirementSnapshot for value in self.secret_requirements)
        ):
            raise ValueError("Invalid pinned Skill Version plan")


@dataclass(frozen=True, slots=True, repr=False)
class LegacyInlineRunSkillPlan:
    """Exact metadata identity for one self-contained v2/v3 Run Skill."""

    dependency_order: int
    scope: AssetScope
    asset_id: uuid.UUID
    version_id: uuid.UUID
    payload_checksum: str
    catalog_generation: int
    snapshot_schema_version: Literal[2, 3]
    file_count: int
    content_size_bytes: int
    secret_requirements: tuple[SkillSecretRequirementSnapshot, ...]

    def __post_init__(self) -> None:
        if (
            type(self.dependency_order) is not int
            or self.dependency_order < 0
            or type(self.scope) is not AssetScope
            or type(self.asset_id) is not uuid.UUID
            or type(self.version_id) is not uuid.UUID
            or type(self.payload_checksum) is not str
            or _HEX_DIGEST.fullmatch(self.payload_checksum) is None
            or type(self.catalog_generation) is not int
            or self.catalog_generation < 0
            or type(self.snapshot_schema_version) is not int
            or self.snapshot_schema_version not in {2, 3}
            or type(self.file_count) is not int
            or not 1 <= self.file_count <= MAX_SKILL_ARCHIVE_FILES
            or type(self.content_size_bytes) is not int
            or not 0 <= self.content_size_bytes <= MAX_SKILL_ARCHIVE_BYTES
            or type(self.secret_requirements) is not tuple
            or any(type(value) is not SkillSecretRequirementSnapshot for value in self.secret_requirements)
        ):
            raise ValueError("Invalid legacy inline Run Skill plan")


RunSkillVersionPlan = PinnedSkillVersionPlan | LegacyInlineRunSkillPlan


@dataclass(frozen=True, slots=True, repr=False)
class RunSkillTreeMaterializationPlan:
    project_id: uuid.UUID
    owner_user_id: str
    thread_id: str
    run_id: str
    runtime_kind: Literal["chat", "skill_builder"]
    attempt_identity: MaterializationAttemptIdentity
    plan_fingerprint: str
    skill_versions: tuple[RunSkillVersionPlan, ...]

    def __post_init__(self) -> None:
        if (
            type(self.project_id) is not uuid.UUID
            or type(self.owner_user_id) is not str
            or not self.owner_user_id
            or len(self.owner_user_id) > 36
            or type(self.thread_id) is not str
            or not self.thread_id
            or len(self.thread_id) > 64
            or type(self.run_id) is not str
            or not self.run_id
            or len(self.run_id) > 64
            or self.runtime_kind not in {"chat", "skill_builder"}
            or type(self.attempt_identity) is not MaterializationAttemptIdentity
            or type(self.plan_fingerprint) is not str
            or _HEX_DIGEST.fullmatch(self.plan_fingerprint) is None
            or type(self.skill_versions) is not tuple
            or not self.skill_versions
            or any(type(value) not in {PinnedSkillVersionPlan, LegacyInlineRunSkillPlan} for value in self.skill_versions)
        ):
            raise ValueError("Invalid Run Skill tree materialization plan")
        dependency_orders = tuple(value.dependency_order for value in self.skill_versions)
        exact_versions = tuple((value.asset_id, value.version_id) for value in self.skill_versions)
        if dependency_orders != tuple(sorted(dependency_orders)) or len(set(dependency_orders)) != len(dependency_orders) or len(set(exact_versions)) != len(exact_versions):
            raise ValueError("Invalid Run Skill tree materialization plan")


class RunSkillMaterializationAuthority(Protocol):
    """Short control-transaction port owned by the execution orchestrator."""

    async def read_materialization_authority(
        self,
        *,
        boundary: Literal["initial", "version", "final"],
        dependency_order: int | None,
    ) -> MaterializationAuthorityReadback: ...


@dataclass(frozen=True, slots=True)
class MaterializationOwnerMetadata:
    owner_id: uuid.UUID
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    worker_id: uuid.UUID
    state: Literal[
        "materializing",
        "materialized",
        "acquiring",
        "mounted",
        "release_pending",
    ]
    state_generation: int
    created_at: datetime
    updated_at: datetime
    provider_kind: str | None = None
    sandbox_id: str | None = None
    mount_lease_id: str | None = None
    release_reason_code: str | None = None

    def __post_init__(self) -> None:
        coordinates = (self.provider_kind, self.sandbox_id, self.mount_lease_id)
        if (
            any(
                type(value) is not uuid.UUID
                for value in (
                    self.owner_id,
                    self.job_id,
                    self.attempt_id,
                    self.worker_id,
                )
            )
            or self.state
            not in {
                "materializing",
                "materialized",
                "acquiring",
                "mounted",
                "release_pending",
            }
            or type(self.state_generation) is not int
            or self.state_generation < 1
            or type(self.created_at) is not datetime
            or type(self.updated_at) is not datetime
            or self.created_at.tzinfo is None
            or self.updated_at.tzinfo is None
            or self.created_at.utcoffset() is None
            or self.updated_at.utcoffset() is None
            or self.updated_at < self.created_at
            or (self.state in {"materializing", "materialized", "acquiring"} and (any(value is not None for value in coordinates) or self.release_reason_code is not None))
            or (self.state == "mounted" and (any(value is None for value in coordinates) or self.release_reason_code is not None or any(type(value) is not str or not value for value in coordinates)))
            or (
                self.state == "release_pending"
                and (
                    type(self.release_reason_code) is not str
                    or not self.release_reason_code
                    or (any(value is None for value in coordinates) and any(value is not None for value in coordinates))
                    or any(value is not None and (type(value) is not str or not value) for value in coordinates)
                )
            )
        ):
            raise ValueError("Invalid materialization owner metadata")

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": _OWNER_METADATA_SCHEMA_VERSION,
            "owner_id": str(self.owner_id),
            "job_id": str(self.job_id),
            "attempt_id": str(self.attempt_id),
            "worker_id": str(self.worker_id),
            "state": self.state,
            "state_generation": self.state_generation,
            "created_at": _canonical_utc_timestamp(self.created_at),
            "updated_at": _canonical_utc_timestamp(self.updated_at),
            "provider_kind": self.provider_kind,
            "sandbox_id": self.sandbox_id,
            "mount_lease_id": self.mount_lease_id,
            "release_reason_code": self.release_reason_code,
        }

    @classmethod
    def from_json(cls, value: object) -> MaterializationOwnerMetadata:
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "owner_id",
                "job_id",
                "attempt_id",
                "worker_id",
                "state",
                "state_generation",
                "created_at",
                "updated_at",
                "provider_kind",
                "sandbox_id",
                "mount_lease_id",
                "release_reason_code",
            }
            or value.get("schema_version") != _OWNER_METADATA_SCHEMA_VERSION
        ):
            raise ValueError("Invalid materialization owner metadata")
        try:
            return cls(
                owner_id=uuid.UUID(str(value["owner_id"])),
                job_id=uuid.UUID(str(value["job_id"])),
                attempt_id=uuid.UUID(str(value["attempt_id"])),
                worker_id=uuid.UUID(str(value["worker_id"])),
                state=value["state"],  # type: ignore[arg-type]
                state_generation=value["state_generation"],  # type: ignore[arg-type]
                created_at=_parse_utc_timestamp(value["created_at"]),
                updated_at=_parse_utc_timestamp(value["updated_at"]),
                provider_kind=value["provider_kind"],  # type: ignore[arg-type]
                sandbox_id=value["sandbox_id"],  # type: ignore[arg-type]
                mount_lease_id=value["mount_lease_id"],  # type: ignore[arg-type]
                release_reason_code=value["release_reason_code"],  # type: ignore[arg-type]
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("Invalid materialization owner metadata") from None


@dataclass(slots=True)
class _BudgetWaiter:
    source_kind: Literal["v4", "legacy"]
    weight_bytes: int
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    granted: bool = False


class _MaterializationMemoryReservation:
    __slots__ = ("_budget", "_entered", "_released", "source_kind", "weight_bytes")

    def __init__(
        self,
        budget: MaterializationMemoryBudget,
        *,
        source_kind: Literal["v4", "legacy"],
        weight_bytes: int,
    ) -> None:
        self._budget = budget
        self._entered = False
        self._released = False
        self.source_kind = source_kind
        self.weight_bytes = weight_bytes

    async def __aenter__(self) -> _MaterializationMemoryReservation:
        if self._entered:
            raise RuntimeError("Materialization memory reservation is single-use")
        self._entered = True
        await self._budget._acquire(self.source_kind, self.weight_bytes)
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        if not self._released:
            self._released = True
            self._budget._release(self.source_kind, self.weight_bytes)


class MaterializationMemoryBudget:
    """One total gate with a nested v4 aggregate, shared process-wide."""

    _process_lock: ClassVar[threading.Lock] = threading.Lock()
    _process_budget: ClassVar[MaterializationMemoryBudget | None] = None

    def __init__(
        self,
        *,
        capacity_bytes: int,
        v4_capacity_bytes: int | None = None,
    ) -> None:
        if type(capacity_bytes) is not int or capacity_bytes <= 0:
            raise ValueError("Materialization memory capacity must be positive")
        if v4_capacity_bytes is None:
            v4_capacity_bytes = capacity_bytes
        if type(v4_capacity_bytes) is not int or v4_capacity_bytes <= 0 or v4_capacity_bytes > capacity_bytes:
            raise ValueError("Materialization v4 memory capacity is invalid")
        self._capacity_bytes = capacity_bytes
        self._v4_capacity_bytes = v4_capacity_bytes
        self._in_use_bytes = 0
        self._v4_in_use_bytes = 0
        self._peak_in_use_bytes = 0
        self._peak_v4_in_use_bytes = 0
        self._lock = threading.Lock()
        self._waiters: deque[_BudgetWaiter] = deque()

    @classmethod
    def process_wide(
        cls,
        *,
        capacity_bytes: int,
        v4_capacity_bytes: int,
    ) -> MaterializationMemoryBudget:
        """Return the unique production gate, rejecting configuration drift."""

        with cls._process_lock:
            if cls._process_budget is None:
                cls._process_budget = cls(
                    capacity_bytes=capacity_bytes,
                    v4_capacity_bytes=v4_capacity_bytes,
                )
            elif cls._process_budget.capacity_bytes != capacity_bytes or cls._process_budget.v4_capacity_bytes != v4_capacity_bytes:
                raise RuntimeError(
                    "Process-wide materialization memory capacity changed",
                )
            return cls._process_budget

    @property
    def capacity_bytes(self) -> int:
        return self._capacity_bytes

    @property
    def in_use_bytes(self) -> int:
        with self._lock:
            return self._in_use_bytes

    @property
    def v4_capacity_bytes(self) -> int:
        return self._v4_capacity_bytes

    @property
    def v4_in_use_bytes(self) -> int:
        with self._lock:
            return self._v4_in_use_bytes

    @property
    def peak_in_use_bytes(self) -> int:
        with self._lock:
            return self._peak_in_use_bytes

    @property
    def peak_v4_in_use_bytes(self) -> int:
        with self._lock:
            return self._peak_v4_in_use_bytes

    def reserve_v4(
        self,
        *,
        content_size_bytes: int,
    ) -> _MaterializationMemoryReservation:
        return self._reservation("v4", content_size_bytes)

    def reserve_legacy(
        self,
        *,
        envelope_bytes: int,
    ) -> _MaterializationMemoryReservation:
        return self._reservation("legacy", envelope_bytes)

    def _reservation(
        self,
        source_kind: Literal["v4", "legacy"],
        weight_bytes: int,
    ) -> _MaterializationMemoryReservation:
        if type(weight_bytes) is not int or weight_bytes <= 0:
            raise ValueError("Materialization memory weight must be positive")
        if weight_bytes > self._capacity_bytes or (source_kind == "v4" and weight_bytes > self._v4_capacity_bytes):
            raise ValueError("Materialization memory weight exceeds process capacity")
        return _MaterializationMemoryReservation(
            self,
            source_kind=source_kind,
            weight_bytes=weight_bytes,
        )

    async def _acquire(
        self,
        source_kind: Literal["v4", "legacy"],
        weight_bytes: int,
    ) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if not self._waiters and self._can_grant_locked(
                source_kind,
                weight_bytes,
            ):
                self._grant_locked(source_kind, weight_bytes)
                return
            waiter = _BudgetWaiter(
                source_kind=source_kind,
                weight_bytes=weight_bytes,
                loop=loop,
                future=loop.create_future(),
            )
            self._waiters.append(waiter)
        try:
            await waiter.future
        except asyncio.CancelledError:
            with self._lock:
                if waiter.granted:
                    self._in_use_bytes -= waiter.weight_bytes
                    if waiter.source_kind == "v4":
                        self._v4_in_use_bytes -= waiter.weight_bytes
                else:
                    try:
                        self._waiters.remove(waiter)
                    except ValueError:
                        pass
                self._wake_waiters_locked()
            raise

    def _release(
        self,
        source_kind: Literal["v4", "legacy"],
        weight_bytes: int,
    ) -> None:
        with self._lock:
            if weight_bytes > self._in_use_bytes or (source_kind == "v4" and weight_bytes > self._v4_in_use_bytes):
                raise RuntimeError("Materialization memory reservation underflow")
            self._in_use_bytes -= weight_bytes
            if source_kind == "v4":
                self._v4_in_use_bytes -= weight_bytes
            self._wake_waiters_locked()

    def _can_grant_locked(
        self,
        source_kind: Literal["v4", "legacy"],
        weight_bytes: int,
    ) -> bool:
        return self._in_use_bytes + weight_bytes <= self._capacity_bytes and (source_kind != "v4" or self._v4_in_use_bytes + weight_bytes <= self._v4_capacity_bytes)

    def _grant_locked(
        self,
        source_kind: Literal["v4", "legacy"],
        weight_bytes: int,
    ) -> None:
        self._in_use_bytes += weight_bytes
        if source_kind == "v4":
            self._v4_in_use_bytes += weight_bytes
        self._peak_in_use_bytes = max(
            self._peak_in_use_bytes,
            self._in_use_bytes,
        )
        self._peak_v4_in_use_bytes = max(
            self._peak_v4_in_use_bytes,
            self._v4_in_use_bytes,
        )

    def _wake_waiters_locked(self) -> None:
        while self._waiters:
            waiter = self._waiters[0]
            if not self._can_grant_locked(
                waiter.source_kind,
                waiter.weight_bytes,
            ):
                return
            self._waiters.popleft()
            waiter.granted = True
            self._grant_locked(waiter.source_kind, waiter.weight_bytes)
            waiter.loop.call_soon_threadsafe(
                self._complete_waiter,
                waiter.future,
            )

    @staticmethod
    def _complete_waiter(future: asyncio.Future[None]) -> None:
        if not future.done():
            future.set_result(None)


@dataclass(frozen=True, slots=True)
class SkillVersionFileMetadata:
    path: str
    media_type: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.path) is not str
            or type(self.media_type) is not str
            or not self.media_type
            or self.media_type != self.media_type.strip()
            or type(self.size_bytes) is not int
            or self.size_bytes < 0
            or type(self.sha256) is not str
            or _HEX_DIGEST.fullmatch(self.sha256) is None
        ):
            raise ValueError("Invalid Skill Version file metadata")


@dataclass(frozen=True, slots=True)
class SkillVersionFileContent:
    path: str
    size_bytes: int
    sha256: str
    content: bytes

    def __post_init__(self) -> None:
        if type(self.path) is not str or type(self.size_bytes) is not int or self.size_bytes < 0 or type(self.sha256) is not str or _HEX_DIGEST.fullmatch(self.sha256) is None or type(self.content) is not bytes:
            raise ValueError("Invalid Skill Version file content")

    def metadata(self, *, media_type: str) -> SkillVersionFileMetadata:
        return SkillVersionFileMetadata(
            path=self.path,
            media_type=media_type,
            size_bytes=self.size_bytes,
            sha256=self.sha256,
        )


@dataclass(frozen=True, slots=True)
class V4ContentBatch:
    first_path: str
    last_path: str
    expected_paths: tuple[str, ...]
    file_count: int
    content_size_bytes: int
    oversized_singleton: bool
    expected_metadata: tuple[SkillVersionFileMetadata, ...]


@dataclass(frozen=True, slots=True)
class MaterializedSkillArchiveFacts:
    file_count: int
    content_size_bytes: int
    payload_checksum: str


def _canonical_skill_path(raw_path: str) -> str:
    windows_path = PureWindowsPath(raw_path)
    posix_path = raw_path.replace("\\", "/")
    if not raw_path or "\x00" in raw_path or ":" in raw_path or windows_path.drive or windows_path.is_absolute() or posix_path.startswith("/") or ".." in PurePosixPath(posix_path).parts:
        raise ValueError("Skill Version file path is invalid")
    normalized = unicodedata.normalize(
        "NFC",
        posixpath.normpath(posix_path).removeprefix("./"),
    )
    if not normalized or normalized == "." or len(normalized) > _MAX_SKILL_PATH_CHARS or len(normalized.encode("utf-8")) > _MAX_SKILL_PATH_BYTES:
        raise ValueError("Skill Version file path is invalid")
    return normalized


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("Materialization file write made no progress")
        remaining = remaining[written:]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_owner_metadata(
    owner_root: Path,
    metadata: MaterializationOwnerMetadata,
) -> None:
    _validate_owner_root_coordinate(owner_root)
    if owner_root.name != metadata.owner_id.hex:
        raise ValueError("Materialization owner metadata mismatch")
    payload = (
        json.dumps(
            metadata.as_json(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    temporary = owner_root / f".{_OWNER_METADATA_FILE}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, owner_root / _OWNER_METADATA_FILE)
        os.chmod(owner_root / _OWNER_METADATA_FILE, 0o600, follow_symlinks=False)
        _fsync_directory(owner_root)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_owner_metadata(
    owner_root: Path,
    *,
    expected_owner_id: uuid.UUID,
) -> MaterializationOwnerMetadata:
    _validate_owner_root_coordinate(owner_root)
    descriptor = os.open(
        owner_root / _OWNER_METADATA_FILE,
        os.O_RDONLY | os.O_NOFOLLOW,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > 4096:
            raise ValueError("Invalid materialization owner metadata")
        payload = os.read(descriptor, 4097)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size) or len(payload) != before.st_size:
            raise ValueError("Materialization owner metadata changed")
    finally:
        os.close(descriptor)
    try:
        metadata = MaterializationOwnerMetadata.from_json(
            json.loads(payload.decode("utf-8")),
        )
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("Invalid materialization owner metadata") from None
    if metadata.owner_id != expected_owner_id:
        raise ValueError("Materialization owner metadata mismatch")
    return metadata


def _validate_owner_root_coordinate(owner_root: Path) -> None:
    if len(owner_root.name) != 32:
        raise ValueError("Invalid materialization owner coordinate")
    try:
        if uuid.UUID(hex=owner_root.name).hex != owner_root.name:
            raise ValueError("Invalid materialization owner coordinate")
        parent_status = owner_root.parent.lstat()
        owner_status = owner_root.lstat()
    except (OSError, ValueError):
        raise ValueError("Untrusted materialization root") from None
    if stat.S_ISLNK(parent_status.st_mode) or not stat.S_ISDIR(parent_status.st_mode) or stat.S_ISLNK(owner_status.st_mode) or not stat.S_ISDIR(owner_status.st_mode) or stat.S_IMODE(owner_status.st_mode) != 0o700:
        raise ValueError("Untrusted materialization root")


def read_materialization_owner_metadata(
    materialization_root: Path,
    owner_id: uuid.UUID,
) -> MaterializationOwnerMetadata:
    """Read one exact owner record without traversing outside the dedicated root."""

    if not isinstance(materialization_root, Path) or not materialization_root.is_absolute() or ".." in materialization_root.parts or type(owner_id) is not uuid.UUID:
        raise ValueError("Invalid materialization owner coordinate")
    return _read_owner_metadata(
        materialization_root / owner_id.hex,
        expected_owner_id=owner_id,
    )


def remove_materialization_owner_if_unchanged(
    materialization_root: Path,
    expected: MaterializationOwnerMetadata,
) -> bool:
    """Delete one owner root only while its durable lifecycle record is unchanged."""

    if type(expected) is not MaterializationOwnerMetadata:
        raise ValueError("Invalid materialization owner metadata")
    owner_root = materialization_root / expected.owner_id.hex
    try:
        observed = read_materialization_owner_metadata(
            materialization_root,
            expected.owner_id,
        )
    except FileNotFoundError:
        return False
    if observed != expected:
        raise MaterializedRunSkillTreeStateError(
            "Materialization owner lifecycle changed during reap",
        )
    _remove_owner_root(owner_root)
    return True


def _create_owner_root(
    materialization_root: Path,
    owner_id: uuid.UUID,
    identity: MaterializationAttemptIdentity,
) -> MaterializationOwnerMetadata:
    materialization_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_status = materialization_root.lstat()
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise ValueError("Untrusted materialization root")
    owner_root = materialization_root / owner_id.hex
    created_at = _utc_now()
    metadata = MaterializationOwnerMetadata(
        owner_id=owner_id,
        job_id=identity.job_id,
        attempt_id=identity.attempt_id,
        worker_id=identity.worker_id,
        state="materializing",
        state_generation=1,
        created_at=created_at,
        updated_at=created_at,
    )
    os.mkdir(owner_root, mode=0o700)
    try:
        os.chmod(owner_root, 0o700)
        _atomic_write_owner_metadata(owner_root, metadata)
        os.mkdir(owner_root / ".staging", mode=0o700)
        os.chmod(owner_root / ".staging", 0o700)
        _fsync_directory(owner_root)
        _fsync_directory(materialization_root)
    except BaseException:
        _remove_owner_root(owner_root)
        raise
    return metadata


def _write_staged_file(
    staging_root: Path,
    relative_path: str,
    content: bytes,
) -> None:
    if type(relative_path) is not str or _canonical_skill_path(relative_path) != relative_path or type(content) is not bytes or len(content) > MAX_SKILL_ARCHIVE_FILE_BYTES:
        raise ValueError("Invalid materialized Skill file")
    parts = PurePosixPath(relative_path).parts
    parent = staging_root
    for part in parts[:-1]:
        parent = parent / part
        try:
            os.mkdir(parent, mode=0o700)
        except FileExistsError:
            pass
        parent_status = parent.lstat()
        if stat.S_ISLNK(parent_status.st_mode) or not stat.S_ISDIR(
            parent_status.st_mode,
        ):
            raise ValueError("Materialized Skill path contains a link")
        os.chmod(parent, 0o700)
    destination = parent / parts[-1]
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(destination, 0o600, follow_symlinks=False)


def _promote_staged_skill(
    staging_root: Path,
    source_relative_root: str,
    destination_relative_root: str,
) -> None:
    if _canonical_skill_path(source_relative_root) != source_relative_root or _canonical_skill_path(destination_relative_root) != destination_relative_root:
        raise ValueError("Invalid materialized Skill promotion")
    source = staging_root.joinpath(*PurePosixPath(source_relative_root).parts)
    destination = staging_root.joinpath(
        *PurePosixPath(destination_relative_root).parts,
    )
    source_status = source.lstat()
    if stat.S_ISLNK(source_status.st_mode) or not stat.S_ISDIR(
        source_status.st_mode,
    ):
        raise ValueError("Invalid materialized Skill promotion")
    parent = staging_root
    destination_parts = PurePosixPath(destination_relative_root).parts
    for part in destination_parts[:-1]:
        parent = parent / part
        try:
            os.mkdir(parent, mode=0o700)
        except FileExistsError:
            pass
        parent_status = parent.lstat()
        if stat.S_ISLNK(parent_status.st_mode) or not stat.S_ISDIR(
            parent_status.st_mode,
        ):
            raise ValueError("Materialized Skill path contains a link")
        os.chmod(parent, 0o700)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Materialized Skill runtime root conflicts")
    os.replace(source, destination)
    _fsync_directory(destination.parent)


def _publish_owner_tree(
    owner_root: Path,
    metadata: MaterializationOwnerMetadata,
) -> RunReadonlyMountSource:
    staging_root = owner_root / ".staging"
    tree_root = owner_root / "tree"
    _write_staged_file(
        staging_root,
        _RUN_MOUNT_MANIFEST_FILE,
        run_readonly_mount_manifest_text(metadata.owner_id).encode("utf-8"),
    )
    directories: list[Path] = [staging_root]
    regular_files: list[Path] = []
    skill_manifest_found = False
    for current, names, files in os.walk(staging_root, followlinks=False):
        current_path = Path(current)
        for name in names:
            path = current_path / name
            entry = path.lstat()
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                raise ValueError("Materialized Skill tree contains a link or special entry")
            directories.append(path)
        for name in files:
            path = current_path / name
            entry = path.lstat()
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                raise ValueError("Materialized Skill tree contains a link or special entry")
            regular_files.append(path)
            if path.name == "SKILL.md":
                skill_manifest_found = True
    if not skill_manifest_found:
        raise ValueError("Materialized Skill tree has no SKILL.md")
    for path in regular_files:
        os.chmod(path, 0o444, follow_symlinks=False)
    for path in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        os.chmod(path, 0o555, follow_symlinks=False)
        _fsync_directory(path)
    if tree_root.exists() or tree_root.is_symlink():
        raise ValueError("Materialized Skill tree was already published")
    os.replace(staging_root, tree_root)
    _fsync_directory(owner_root)
    _atomic_write_owner_metadata(
        owner_root,
        replace(
            metadata,
            state="materialized",
            state_generation=metadata.state_generation + 1,
            updated_at=_utc_now(),
        ),
    )
    return RunReadonlyMountSource(
        owner_id=metadata.owner_id,
        worker_root=tree_root,
    )


def _remove_owner_root(owner_root: Path) -> None:
    try:
        owner_exists = owner_root.exists() or owner_root.is_symlink()
    except OSError:
        owner_exists = True
    if not owner_exists:
        return
    _validate_owner_root_coordinate(owner_root)
    try:
        root_status = owner_root.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(root_status.st_mode):
        owner_root.unlink()
        _fsync_directory(owner_root.parent)
        return
    if not stat.S_ISDIR(root_status.st_mode):
        raise ValueError("Materialization owner root is not a directory")
    for current, names, files in os.walk(owner_root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            try:
                entry = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(entry.st_mode):
                os.chmod(path, 0o600, follow_symlinks=False)
        for name in names:
            path = current_path / name
            try:
                entry = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(entry.st_mode) and not stat.S_ISLNK(entry.st_mode):
                os.chmod(path, 0o700, follow_symlinks=False)
        os.chmod(current_path, 0o700, follow_symlinks=False)
    shutil.rmtree(owner_root)
    _fsync_directory(owner_root.parent)


def _remove_not_acquired_owner(
    owner_root: Path,
    owner_id: uuid.UUID,
) -> None:
    metadata = _read_owner_metadata(
        owner_root,
        expected_owner_id=owner_id,
    )
    if metadata.state != "materialized":
        raise MaterializedRunSkillTreeStateError(
            "Not-acquired proof requires materialized owner state",
        )
    _remove_owner_root(owner_root)


def _persist_mount_acquiring(
    owner_root: Path,
    owner_id: uuid.UUID,
) -> None:
    metadata = _read_owner_metadata(
        owner_root,
        expected_owner_id=owner_id,
    )
    if metadata.state != "materialized":
        raise MaterializedRunSkillTreeStateError(
            "Mount acquiring requires materialized lifecycle state",
        )
    _atomic_write_owner_metadata(
        owner_root,
        replace(
            metadata,
            state="acquiring",
            state_generation=metadata.state_generation + 1,
            updated_at=_utc_now(),
        ),
    )


def _persist_mount_mounted(
    owner_root: Path,
    owner_id: uuid.UUID,
    lease: ProviderRunMountLease,
) -> None:
    if type(lease) is not ProviderRunMountLease or lease.owner_id != owner_id:
        raise MaterializedRunSkillTreeStateError(
            "Provider mount lease does not match materialized tree",
        )
    metadata = _read_owner_metadata(
        owner_root,
        expected_owner_id=owner_id,
    )
    if metadata.state == "mounted":
        persisted = _metadata_provider_lease(metadata)
        if persisted == lease:
            return
        raise MaterializedRunSkillTreeStateError(
            "Mounted provider lease does not match materialized tree",
        )
    if metadata.state != "acquiring":
        raise MaterializedRunSkillTreeStateError(
            "Mount confirmation requires acquiring lifecycle state",
        )
    _atomic_write_owner_metadata(
        owner_root,
        replace(
            metadata,
            state="mounted",
            state_generation=metadata.state_generation + 1,
            updated_at=_utc_now(),
            provider_kind=lease.provider_kind,
            sandbox_id=lease.sandbox_id,
            mount_lease_id=lease.mount_lease_id,
        ),
    )


def _metadata_provider_lease(
    metadata: MaterializationOwnerMetadata,
) -> ProviderRunMountLease | None:
    coordinates = (
        metadata.provider_kind,
        metadata.sandbox_id,
        metadata.mount_lease_id,
    )
    if all(value is None for value in coordinates):
        return None
    if any(value is None for value in coordinates):
        raise MaterializedRunSkillTreeStateError(
            "Materialized tree provider lease is incomplete",
        )
    return ProviderRunMountLease(
        owner_id=metadata.owner_id,
        provider_kind=metadata.provider_kind,
        sandbox_id=metadata.sandbox_id,
        mount_lease_id=metadata.mount_lease_id,
    )


def _remove_released_owner(
    owner_root: Path,
    owner_id: uuid.UUID,
    outcome: Released,
) -> None:
    metadata = _read_owner_metadata(
        owner_root,
        expected_owner_id=owner_id,
    )
    if metadata.state not in {"acquiring", "mounted", "release_pending"}:
        raise MaterializedRunSkillTreeStateError(
            "Released proof requires provider lifecycle state",
        )
    persisted_lease = _metadata_provider_lease(metadata)
    if persisted_lease is not None and not outcome.matches_lease(
        persisted_lease,
    ):
        raise MaterializedRunSkillTreeStateError(
            "Released proof does not match persisted provider lease",
        )
    _remove_owner_root(owner_root)


def _persist_release_pending(
    owner_root: Path,
    owner_id: uuid.UUID,
    outcome: Orphaned,
) -> None:
    metadata = _read_owner_metadata(
        owner_root,
        expected_owner_id=owner_id,
    )
    if metadata.state not in {"acquiring", "mounted", "release_pending"}:
        raise MaterializedRunSkillTreeStateError(
            "Orphaned outcome requires provider lifecycle state",
        )
    if outcome.last_lifecycle_state != metadata.state:
        raise MaterializedRunSkillTreeStateError(
            "Orphaned outcome lifecycle state does not match materialized tree",
        )
    persisted_lease = _metadata_provider_lease(metadata)
    if persisted_lease is not None and not outcome.matches_lease(
        persisted_lease,
    ):
        raise MaterializedRunSkillTreeStateError(
            "Orphaned outcome does not match persisted provider lease",
        )
    _atomic_write_owner_metadata(
        owner_root,
        replace(
            metadata,
            state="release_pending",
            state_generation=metadata.state_generation + 1,
            updated_at=_utc_now(),
            provider_kind=(persisted_lease.provider_kind if persisted_lease is not None else outcome.provider_kind),
            sandbox_id=(persisted_lease.sandbox_id if persisted_lease is not None else outcome.sandbox_id),
            mount_lease_id=(persisted_lease.mount_lease_id if persisted_lease is not None else outcome.mount_lease_id),
            release_reason_code=outcome.reason_code,
        ),
    )


class PendingMaterializedRunSkillTree:
    """Exclusive caller-owned token before runtime adoption."""

    __slots__ = (
        "_owner_root",
        "_state",
        "_state_lock",
        "manifests",
        "skills",
        "source",
    )

    def __init__(
        self,
        *,
        owner_root: Path,
        source: RunReadonlyMountSource,
        manifests: tuple[PrivateSkillManifest, ...],
        skills: tuple[Skill, ...],
    ) -> None:
        self._owner_root = owner_root
        self.source = source
        self.manifests = manifests
        self.skills = skills
        self._state = "owned"
        self._state_lock = threading.Lock()

    def transfer_to(
        self,
        owner: MaterializedRunSkillTreeOwner,
    ) -> RuntimeOwnedMaterializedRunSkillTree:
        adopt = getattr(owner, "adopt_materialized_skill_tree", None)
        if not callable(adopt):
            raise ValueError("Invalid materialized Skill tree owner")
        with self._state_lock:
            if self._state != "owned":
                raise MaterializedRunSkillTreeStateError(
                    "Pending materialized Skill tree token is not active",
                )
            runtime = RuntimeOwnedMaterializedRunSkillTree(
                owner_root=self._owner_root,
                source=self.source,
                manifests=self.manifests,
                skills=self.skills,
            )
            try:
                adopt(runtime)
            except BaseException:
                runtime._abort_transfer()
                raise
            self._state = "transferred"
            runtime._activate_after_adoption()
            return runtime

    async def aclose(self) -> None:
        with self._state_lock:
            if self._state in {"closed", "transferred"}:
                return
            if self._state != "owned":
                raise MaterializedRunSkillTreeStateError(
                    "Materialized Skill tree token is busy",
                )
            self._state = "closing"
        try:
            await joined_to_thread(_remove_owner_root, self._owner_root)
        except asyncio.CancelledError:
            absent = not await joined_to_thread(self._owner_root.exists)
            with self._state_lock:
                self._state = "closed" if absent else "owned"
            raise
        except BaseException:
            with self._state_lock:
                self._state = "owned"
            raise
        else:
            with self._state_lock:
                self._state = "closed"


class MaterializedRunSkillTreeOwner(Protocol):
    def adopt_materialized_skill_tree(
        self,
        tree: RuntimeOwnedMaterializedRunSkillTree,
    ) -> None: ...


class RuntimeOwnedMaterializedRunSkillTree:
    """Runtime-owned token finalized only by typed provider release evidence."""

    __slots__ = (
        "_operation_lock",
        "_owner_root",
        "_state",
        "_state_lock",
        "manifests",
        "skills",
        "source",
    )

    def __init__(
        self,
        *,
        owner_root: Path,
        source: RunReadonlyMountSource,
        manifests: tuple[PrivateSkillManifest, ...],
        skills: tuple[Skill, ...],
    ) -> None:
        self._owner_root = owner_root
        self.source = source
        self.manifests = manifests
        self.skills = skills
        self._state = "provisional"
        self._state_lock = threading.Lock()
        self._operation_lock = asyncio.Lock()

    def _abort_transfer(self) -> None:
        with self._state_lock:
            if self._state == "provisional":
                self._state = "invalid"

    def _activate_after_adoption(self) -> None:
        self._state = "active"

    async def persist_mount_acquiring(self) -> None:
        """Durably fence provider acquisition before any provider call."""

        async with self._operation_lock:
            with self._state_lock:
                if self._state != "active":
                    raise MaterializedRunSkillTreeStateError(
                        "Runtime materialized Skill tree token is not active",
                    )
            await joined_to_thread(
                _persist_mount_acquiring,
                self._owner_root,
                self.source.owner_id,
            )

    async def provider_acquire_may_have_started(self) -> bool:
        """Return false only while durable metadata proves no transaction A."""

        async with self._operation_lock:
            with self._state_lock:
                if self._state != "active":
                    raise MaterializedRunSkillTreeStateError(
                        "Runtime materialized Skill tree token is not active",
                    )
            metadata = await joined_to_thread(
                _read_owner_metadata,
                self._owner_root,
                expected_owner_id=self.source.owner_id,
            )
            if metadata.state == "materialized":
                return False
            if metadata.state in {"acquiring", "mounted", "release_pending"}:
                return True
            raise MaterializedRunSkillTreeStateError(
                "Materialized Skill tree has no provider acquisition state",
            )

    async def persist_mount_mounted(
        self,
        lease: ProviderRunMountLease,
    ) -> None:
        """Durably bind the exact provider lease after provider readback."""

        if type(lease) is not ProviderRunMountLease or not lease.matches_source(
            self.source,
        ):
            raise MaterializedRunSkillTreeStateError(
                "Provider mount lease does not match materialized tree",
            )
        async with self._operation_lock:
            with self._state_lock:
                if self._state != "active":
                    raise MaterializedRunSkillTreeStateError(
                        "Runtime materialized Skill tree token is not active",
                    )
            await joined_to_thread(
                _persist_mount_mounted,
                self._owner_root,
                self.source.owner_id,
                lease,
            )

    async def read_mount_lifecycle_state(
        self,
    ) -> Literal["acquiring", "mounted", "release_pending"]:
        """Read the durable owner state used to classify orphan evidence."""

        async with self._operation_lock:
            with self._state_lock:
                if self._state != "active":
                    raise MaterializedRunSkillTreeStateError(
                        "Runtime materialized Skill tree token is not active",
                    )
            metadata = await joined_to_thread(
                _read_owner_metadata,
                self._owner_root,
                expected_owner_id=self.source.owner_id,
            )
            if metadata.state == "acquiring":
                return "acquiring"
            if metadata.state == "mounted":
                return "mounted"
            if metadata.state == "release_pending":
                return "release_pending"
            raise MaterializedRunSkillTreeStateError(
                "Materialized Skill tree has no provider lifecycle state",
            )

    async def finalize(
        self,
        outcome: RunMountReleaseOutcome,
    ) -> None:
        if type(outcome) not in {NotAcquired, Released, Orphaned} or not outcome.matches_source(self.source):
            raise MaterializedRunSkillTreeStateError(
                "Run mount release outcome does not match materialized tree",
            )
        async with self._operation_lock:
            with self._state_lock:
                if self._state != "active":
                    raise MaterializedRunSkillTreeStateError(
                        "Runtime materialized Skill tree token is not active",
                    )
                self._state = "finalizing"
            if type(outcome) is Orphaned:
                await self._finalize_orphaned(outcome)
            elif type(outcome) is NotAcquired:
                await self._finalize_not_acquired()
            else:
                await self._finalize_released(outcome)

    async def _finalize_not_acquired(self) -> None:
        try:
            await joined_to_thread(
                _remove_not_acquired_owner,
                self._owner_root,
                self.source.owner_id,
            )
        except asyncio.CancelledError:
            absent = not await joined_to_thread(self._owner_root.exists)
            with self._state_lock:
                self._state = "invalid" if absent else "active"
            raise
        except BaseException:
            with self._state_lock:
                self._state = "active"
            raise
        else:
            with self._state_lock:
                self._state = "invalid"

    async def _finalize_released(self, outcome: Released) -> None:
        try:
            await joined_to_thread(
                _remove_released_owner,
                self._owner_root,
                self.source.owner_id,
                outcome,
            )
        except asyncio.CancelledError:
            absent = not await joined_to_thread(self._owner_root.exists)
            with self._state_lock:
                self._state = "invalid" if absent else "active"
            raise
        except BaseException:
            with self._state_lock:
                self._state = "active"
            raise
        else:
            with self._state_lock:
                self._state = "invalid"

    async def _finalize_orphaned(self, outcome: Orphaned) -> None:
        try:
            await joined_to_thread(
                _persist_release_pending,
                self._owner_root,
                self.source.owner_id,
                outcome,
            )
        except asyncio.CancelledError:
            try:
                observed = await joined_to_thread(
                    _read_owner_metadata,
                    self._owner_root,
                    expected_owner_id=self.source.owner_id,
                )
            except BaseException:
                observed = None
            with self._state_lock:
                self._state = "invalid" if observed is not None and observed.state == "release_pending" else "active"
            raise
        except BaseException:
            with self._state_lock:
                self._state = "active"
            raise
        else:
            with self._state_lock:
                self._state = "invalid"


class MaterializingRunSkillTree:
    """One unpublished staging tree owned by its materializer caller."""

    __slots__ = (
        "_content_size_bytes",
        "_file_count",
        "_metadata",
        "_operation_lock",
        "_owner_root",
        "_staging_root",
        "_state",
    )

    def __init__(
        self,
        *,
        owner_root: Path,
        metadata: MaterializationOwnerMetadata,
    ) -> None:
        self._owner_root = owner_root
        self._staging_root = owner_root / ".staging"
        self._metadata = metadata
        self._operation_lock = asyncio.Lock()
        self._state = "open"
        self._file_count = 0
        self._content_size_bytes = 0

    @property
    def owner_id(self) -> uuid.UUID:
        return self._metadata.owner_id

    @property
    def published_tree_root(self) -> Path:
        return self._owner_root / "tree"

    async def write_file(self, relative_path: str, content: bytes) -> None:
        async with self._operation_lock:
            self._require_open()
            if type(content) is not bytes or self._file_count + 1 > MAX_SKILL_ARCHIVE_FILES or self._content_size_bytes + len(content) > MAX_SKILL_ARCHIVE_BYTES:
                await self._cleanup_after_failure(
                    ValueError("Materialized Skill tree exceeds archive limits"),
                )
            try:
                await joined_to_thread(
                    _write_staged_file,
                    self._staging_root,
                    relative_path,
                    content,
                )
            except BaseException as primary:
                await self._cleanup_after_failure(primary)
            self._file_count += 1
            self._content_size_bytes += len(content)

    async def stage_source_file(
        self,
        relative_path: str,
        content: bytes,
    ) -> None:
        """Write one already metadata-bounded source row into private staging."""

        async with self._operation_lock:
            self._require_open()
            try:
                await joined_to_thread(
                    _write_staged_file,
                    self._staging_root,
                    relative_path,
                    content,
                )
            except BaseException as primary:
                await self._cleanup_after_failure(primary)

    async def promote_staged_skill(
        self,
        *,
        source_relative_root: str,
        destination_relative_root: str,
    ) -> None:
        async with self._operation_lock:
            self._require_open()
            try:
                await joined_to_thread(
                    _promote_staged_skill,
                    self._staging_root,
                    source_relative_root,
                    destination_relative_root,
                )
            except BaseException as primary:
                await self._cleanup_after_failure(primary)

    async def publish(
        self,
        *,
        manifests: tuple[PrivateSkillManifest, ...],
        skills: tuple[Skill, ...],
    ) -> PendingMaterializedRunSkillTree:
        if type(manifests) is not tuple or type(skills) is not tuple:
            raise ValueError("Invalid materialized Skill manifests")
        async with self._operation_lock:
            self._require_open()
            try:
                source = await joined_to_thread(
                    _publish_owner_tree,
                    self._owner_root,
                    self._metadata,
                )
            except BaseException as primary:
                await self._cleanup_after_failure(primary)
            self._state = "published"
            return PendingMaterializedRunSkillTree(
                owner_root=self._owner_root,
                source=source,
                manifests=manifests,
                skills=skills,
            )

    async def aclose(self) -> None:
        async with self._operation_lock:
            if self._state in {"closed", "published"}:
                return
            self._require_open()
            try:
                await joined_to_thread(_remove_owner_root, self._owner_root)
            except asyncio.CancelledError:
                absent = not await joined_to_thread(self._owner_root.exists)
                self._state = "closed" if absent else "open"
                raise
            else:
                self._state = "closed"

    def _require_open(self) -> None:
        if self._state != "open":
            raise MaterializedRunSkillTreeStateError(
                "Materializing Skill tree token is not active",
            )

    async def _cleanup_after_failure(self, primary: BaseException) -> None:
        try:
            await joined_to_thread(_remove_owner_root, self._owner_root)
        except BaseException as cleanup_error:
            self._state = "open"
            raise primary from cleanup_error
        self._state = "closed"
        raise primary


@dataclass(frozen=True, slots=True)
class _MaterializedRunSkillVersion:
    plan: RunSkillVersionPlan
    source_relative_root: str
    parsed: Skill


def _parsed_skill_secret_requirements(
    parsed: Skill,
) -> tuple[SkillSecretRequirementSnapshot, ...]:
    return tuple(
        SkillSecretRequirementSnapshot(
            name=value.name,
            target_env=value.target_env or value.name,
            optional=value.optional,
        )
        for value in parsed.required_secrets
    )


class LegacyInlineRunSkillSourceAdapter:
    """Reader-first adapter for one self-contained v2/v3 Run Skill row."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        if not callable(session_factory):
            raise ValueError("Invalid legacy Run Skill session factory")
        self._session_factory = session_factory

    async def materialize_version(
        self,
        *,
        run_plan: RunSkillTreeMaterializationPlan,
        version_plan: LegacyInlineRunSkillPlan,
        builder: MaterializingRunSkillTree,
        memory_budget: MaterializationMemoryBudget,
        batch_planner: RunSkillTreeMaterializer,
    ) -> _MaterializedRunSkillVersion:
        if (
            type(run_plan) is not RunSkillTreeMaterializationPlan
            or type(version_plan) is not LegacyInlineRunSkillPlan
            or type(builder) is not MaterializingRunSkillTree
            or type(memory_budget) is not MaterializationMemoryBudget
            or type(batch_planner) is not RunSkillTreeMaterializer
        ):
            raise ValueError("Invalid legacy Run Skill materialization input")
        source_relative_root = f".incoming-{version_plan.dependency_order}-{version_plan.version_id.hex}"
        try:
            async with memory_budget.reserve_legacy(
                envelope_bytes=memory_budget.capacity_bytes,
            ):
                async with self._session_factory() as session, session.begin():
                    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                    encoded = await self._read_exact_snapshot(
                        session,
                        run_plan=run_plan,
                        version_plan=version_plan,
                    )
                encoded_limit = MAX_LEGACY_V2_RUN_SKILL_SNAPSHOT_JSON_BYTES if version_plan.snapshot_schema_version == 2 else MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES
                if encoded_run_asset_snapshot_json_size(encoded) > encoded_limit:
                    raise RunSkillTreeMaterializationStale(
                        "Legacy Run Skill snapshot exceeds its encoded limit",
                    )
                decoded = decode_run_asset_snapshot(encoded)
                if (
                    type(decoded) is not ResolvedSkillSnapshot
                    or decoded.scope is not version_plan.scope
                    or decoded.asset_id != version_plan.asset_id
                    or decoded.version_id != version_plan.version_id
                    or decoded.checksum != version_plan.payload_checksum
                    or decoded.catalog_generation != version_plan.catalog_generation
                ):
                    raise RunSkillTreeMaterializationStale(
                        "Legacy Run Skill snapshot metadata changed",
                    )
                facts = batch_planner.archive_facts(
                    tuple(
                        SkillVersionFileMetadata(
                            path=file.path,
                            media_type=file.media_type,
                            size_bytes=len(file.content),
                            sha256=hashlib.sha256(file.content).hexdigest(),
                        )
                        for file in decoded.files
                    )
                )
                if facts != MaterializedSkillArchiveFacts(
                    file_count=version_plan.file_count,
                    content_size_bytes=version_plan.content_size_bytes,
                    payload_checksum=version_plan.payload_checksum,
                ):
                    raise RunSkillTreeMaterializationStale(
                        "Legacy Run Skill facts changed",
                    )
                paths = tuple(value.path for value in decoded.files)
                if "SKILL.md" not in paths or any(PurePosixPath(path).name == "SKILL.md" and path != "SKILL.md" for path in paths):
                    raise RunSkillTreeMaterializationStale(
                        "Legacy Run Skill runtime manifest is invalid",
                    )
                for archive_file in decoded.files:
                    await builder.stage_source_file(
                        f"{source_relative_root}/{archive_file.path}",
                        archive_file.content,
                    )
                parsed = await joined_to_thread(
                    parse_skill_file,
                    builder._staging_root / source_relative_root / "SKILL.md",
                    SkillCategory.CUSTOM,
                    Path(version_plan.asset_id.hex),
                )
                if parsed is None or _RUNTIME_SKILL_NAME.fullmatch(parsed.name) is None or _parsed_skill_secret_requirements(parsed) != decoded.secret_requirements or decoded.secret_requirements != version_plan.secret_requirements:
                    raise RunSkillTreeMaterializationStale(
                        "Legacy Run Skill runtime manifest changed",
                    )
                del archive_file, decoded, encoded, paths
            return _MaterializedRunSkillVersion(
                plan=version_plan,
                source_relative_root=source_relative_root,
                parsed=parsed,
            )
        except (
            RunAssetSnapshotInvalid,
            SkillSecretConfigurationInvalid,
            ValueError,
        ) as error:
            if isinstance(error, RunSkillTreeMaterializationStale):
                raise
            raise RunSkillTreeMaterializationStale(
                "Legacy Run Skill snapshot is invalid",
            ) from error

    @staticmethod
    async def _read_exact_snapshot(
        session: AsyncSession,
        *,
        run_plan: RunSkillTreeMaterializationPlan,
        version_plan: LegacyInlineRunSkillPlan,
    ) -> Mapping[str, object]:
        asset = RunAssetVersionRow
        ref = RunSkillVersionRefRow
        encoded = (
            await session.execute(
                select(asset.snapshot_json).where(
                    asset.project_id == run_plan.project_id,
                    asset.owner_user_id == run_plan.owner_user_id,
                    asset.thread_id == run_plan.thread_id,
                    asset.run_id == run_plan.run_id,
                    asset.asset_kind == AssetKind.SKILL.value,
                    asset.dependency_order == version_plan.dependency_order,
                    asset.asset_scope == version_plan.scope.value,
                    asset.asset_id == version_plan.asset_id,
                    asset.version_id == version_plan.version_id,
                    asset.payload_checksum == version_plan.payload_checksum,
                    asset.catalog_generation == version_plan.catalog_generation,
                    asset.snapshot_schema_version == version_plan.snapshot_schema_version,
                    ~exists().where(
                        and_(
                            ref.project_id == asset.project_id,
                            ref.owner_user_id == asset.owner_user_id,
                            ref.thread_id == asset.thread_id,
                            ref.run_id == asset.run_id,
                            ref.asset_kind == asset.asset_kind,
                            ref.dependency_order == asset.dependency_order,
                        )
                    ),
                )
            )
        ).scalar_one_or_none()
        if not isinstance(encoded, Mapping):
            raise RunSkillTreeMaterializationStale(
                "Legacy Run Skill snapshot is missing",
            )
        if encoded.get("schema_version") != version_plan.snapshot_schema_version:
            raise RunSkillTreeMaterializationStale(
                "Legacy Run Skill snapshot schema changed",
            )
        return encoded


class PinnedSkillVersionSourceAdapter:
    """Metadata-first PostgreSQL reader for exact immutable v4 Skill refs."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        if not callable(session_factory):
            raise ValueError("Invalid pinned Skill Version session factory")
        self._session_factory = session_factory

    async def materialize_version(
        self,
        *,
        run_plan: RunSkillTreeMaterializationPlan,
        version_plan: PinnedSkillVersionPlan,
        builder: MaterializingRunSkillTree,
        memory_budget: MaterializationMemoryBudget,
        batch_planner: RunSkillTreeMaterializer,
    ) -> _MaterializedRunSkillVersion:
        if (
            type(run_plan) is not RunSkillTreeMaterializationPlan
            or type(version_plan) is not PinnedSkillVersionPlan
            or type(builder) is not MaterializingRunSkillTree
            or type(memory_budget) is not MaterializationMemoryBudget
            or type(batch_planner) is not RunSkillTreeMaterializer
        ):
            raise ValueError("Invalid pinned Skill Version materialization input")
        source_relative_root = f".incoming-{version_plan.dependency_order}-{version_plan.version_id.hex}"
        try:
            async with memory_budget.reserve_v4(
                content_size_bytes=version_plan.content_size_bytes,
            ):
                async with self._session_factory() as session, session.begin():
                    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                    await self._assert_exact_metadata(
                        session,
                        run_plan=run_plan,
                        version_plan=version_plan,
                    )
                    metadata = await self._read_file_metadata(
                        session,
                        run_plan=run_plan,
                        version_plan=version_plan,
                    )
                    metadata_paths = tuple(value.path for value in metadata)
                    if "SKILL.md" not in metadata_paths or any(PurePosixPath(path).name == "SKILL.md" and path != "SKILL.md" for path in metadata_paths):
                        raise RunSkillTreeMaterializationStale(
                            "Pinned Skill Version runtime manifest is invalid",
                        )
                    facts = batch_planner.archive_facts(metadata)
                    if facts != MaterializedSkillArchiveFacts(
                        file_count=version_plan.file_count,
                        content_size_bytes=version_plan.content_size_bytes,
                        payload_checksum=version_plan.payload_checksum,
                    ):
                        raise RunSkillTreeMaterializationStale(
                            "Pinned Skill Version facts changed",
                        )
                    batches = batch_planner.plan_v4_content_batches(metadata)
                    for batch in batches:
                        await self._read_and_write_content_batch(
                            session,
                            run_plan=run_plan,
                            version_plan=version_plan,
                            batch=batch,
                            builder=builder,
                            source_relative_root=source_relative_root,
                        )
                parsed = await joined_to_thread(
                    parse_skill_file,
                    builder._staging_root / source_relative_root / "SKILL.md",
                    SkillCategory.CUSTOM,
                    Path(version_plan.asset_id.hex),
                )
                if parsed is None or _RUNTIME_SKILL_NAME.fullmatch(parsed.name) is None or _parsed_skill_secret_requirements(parsed) != version_plan.secret_requirements:
                    raise RunSkillTreeMaterializationStale(
                        "Pinned Skill Version runtime manifest changed",
                    )
            return _MaterializedRunSkillVersion(
                plan=version_plan,
                source_relative_root=source_relative_root,
                parsed=parsed,
            )
        except (
            RunAssetSnapshotInvalid,
            SkillSecretConfigurationInvalid,
            ValueError,
        ) as error:
            if isinstance(error, RunSkillTreeMaterializationStale):
                raise
            raise RunSkillTreeMaterializationStale(
                "Pinned Skill Version is invalid",
            ) from error

    async def _assert_exact_metadata(
        self,
        session: AsyncSession,
        *,
        run_plan: RunSkillTreeMaterializationPlan,
        version_plan: PinnedSkillVersionPlan,
    ) -> None:
        asset = RunAssetVersionRow
        ref = RunSkillVersionRefRow
        skill = SkillRow
        version = SkillVersionRow
        row = (
            await session.execute(
                select(
                    asset.asset_kind.label("parent_kind"),
                    asset.dependency_order.label("parent_order"),
                    asset.asset_scope.label("parent_scope"),
                    asset.asset_id.label("parent_asset_id"),
                    asset.version_id.label("parent_version_id"),
                    asset.payload_checksum.label("parent_checksum"),
                    asset.catalog_generation.label("parent_generation"),
                    asset.snapshot_schema_version.label("parent_schema"),
                    asset.snapshot_json.label("parent_manifest"),
                    ref.asset_kind.label("ref_kind"),
                    ref.dependency_order.label("ref_order"),
                    ref.asset_scope.label("ref_scope"),
                    ref.snapshot_schema_version.label("ref_schema"),
                    ref.skill_project_id.label("ref_skill_project_id"),
                    ref.skill_id.label("ref_skill_id"),
                    ref.skill_version_id.label("ref_version_id"),
                    ref.payload_checksum.label("ref_checksum"),
                    ref.file_count.label("ref_file_count"),
                    ref.content_size_bytes.label("ref_content_size"),
                    skill.scope.label("skill_scope"),
                    skill.project_id.label("skill_project_id"),
                    version.skill_id.label("version_skill_id"),
                    version.id.label("version_id"),
                    version.payload_checksum.label("version_checksum"),
                    version.file_count.label("version_file_count"),
                    version.content_size_bytes.label("version_content_size"),
                    version.files_sealed.label("version_files_sealed"),
                    version.secret_requirements.label("version_secrets"),
                )
                .select_from(asset)
                .join(
                    ref,
                    and_(
                        ref.project_id == asset.project_id,
                        ref.owner_user_id == asset.owner_user_id,
                        ref.thread_id == asset.thread_id,
                        ref.run_id == asset.run_id,
                        ref.asset_kind == asset.asset_kind,
                        ref.dependency_order == asset.dependency_order,
                        ref.asset_scope == asset.asset_scope,
                        ref.skill_id == asset.asset_id,
                        ref.skill_version_id == asset.version_id,
                        ref.payload_checksum == asset.payload_checksum,
                        ref.snapshot_schema_version == asset.snapshot_schema_version,
                    ),
                )
                .join(skill, skill.id == asset.asset_id)
                .join(
                    version,
                    and_(
                        version.skill_id == asset.asset_id,
                        version.id == asset.version_id,
                    ),
                )
                .where(
                    asset.project_id == run_plan.project_id,
                    asset.owner_user_id == run_plan.owner_user_id,
                    asset.thread_id == run_plan.thread_id,
                    asset.run_id == run_plan.run_id,
                    asset.asset_kind == AssetKind.SKILL.value,
                    asset.dependency_order == version_plan.dependency_order,
                )
            )
        ).one_or_none()
        if row is None:
            raise RunSkillTreeMaterializationStale(
                "Pinned Skill Version metadata is missing",
            )
        values = row._mapping
        manifest_value = values["parent_manifest"]
        if not isinstance(manifest_value, Mapping):
            raise RunSkillTreeMaterializationStale(
                "Pinned Skill Version manifest is invalid",
            )
        manifest = decode_run_skill_version_manifest(manifest_value)
        declarations = parse_skill_secret_declarations(
            values["version_secrets"],
            request_id="run-skill-materialization",
        )
        expected_project_id = None if version_plan.scope is AssetScope.SYSTEM else run_plan.project_id
        expected = (
            AssetKind.SKILL.value,
            version_plan.dependency_order,
            version_plan.scope.value,
            version_plan.asset_id,
            version_plan.version_id,
            version_plan.payload_checksum,
            version_plan.catalog_generation,
            RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION,
            AssetKind.SKILL.value,
            version_plan.dependency_order,
            version_plan.scope.value,
            RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION,
            expected_project_id,
            version_plan.asset_id,
            version_plan.version_id,
            version_plan.payload_checksum,
            version_plan.file_count,
            version_plan.content_size_bytes,
            version_plan.scope.value,
            expected_project_id,
            version_plan.asset_id,
            version_plan.version_id,
            version_plan.payload_checksum,
            version_plan.file_count,
            version_plan.content_size_bytes,
            True,
        )
        observed = tuple(
            values[key]
            for key in (
                "parent_kind",
                "parent_order",
                "parent_scope",
                "parent_asset_id",
                "parent_version_id",
                "parent_checksum",
                "parent_generation",
                "parent_schema",
                "ref_kind",
                "ref_order",
                "ref_scope",
                "ref_schema",
                "ref_skill_project_id",
                "ref_skill_id",
                "ref_version_id",
                "ref_checksum",
                "ref_file_count",
                "ref_content_size",
                "skill_scope",
                "skill_project_id",
                "version_skill_id",
                "version_id",
                "version_checksum",
                "version_file_count",
                "version_content_size",
                "version_files_sealed",
            )
        )
        expected_declarations = tuple(
            SkillSecretRequirementSnapshot(
                name=value.name,
                target_env=value.target_env,
                optional=value.optional,
            )
            for value in declarations
        )
        if (
            observed != expected
            or manifest.kind is not AssetKind.SKILL
            or manifest.scope is not version_plan.scope
            or manifest.asset_id != version_plan.asset_id
            or manifest.version_id != version_plan.version_id
            or manifest.checksum != version_plan.payload_checksum
            or manifest.catalog_generation != version_plan.catalog_generation
            or manifest.dependency_version_ids != version_plan.dependency_version_ids
            or manifest.file_count != version_plan.file_count
            or manifest.content_size_bytes != version_plan.content_size_bytes
            or expected_declarations != version_plan.secret_requirements
        ):
            raise RunSkillTreeMaterializationStale(
                "Pinned Skill Version metadata changed",
            )

    async def _read_file_metadata(
        self,
        session: AsyncSession,
        *,
        run_plan: RunSkillTreeMaterializationPlan,
        version_plan: PinnedSkillVersionPlan,
    ) -> tuple[SkillVersionFileMetadata, ...]:
        ref = RunSkillVersionRefRow
        file = SkillVersionFileRow
        rows = (
            await session.execute(
                select(
                    file.path,
                    file.media_type,
                    file.size_bytes,
                    file.sha256,
                )
                .select_from(ref)
                .join(file, file.skill_version_id == ref.skill_version_id)
                .where(
                    ref.project_id == run_plan.project_id,
                    ref.owner_user_id == run_plan.owner_user_id,
                    ref.thread_id == run_plan.thread_id,
                    ref.run_id == run_plan.run_id,
                    ref.asset_kind == AssetKind.SKILL.value,
                    ref.dependency_order == version_plan.dependency_order,
                    ref.skill_id == version_plan.asset_id,
                    ref.skill_version_id == version_plan.version_id,
                )
                .order_by(file.path.collate("C"))
            )
        ).all()
        try:
            metadata = tuple(
                SkillVersionFileMetadata(
                    path=path,
                    media_type=media_type,
                    size_bytes=size_bytes,
                    sha256=sha256,
                )
                for path, media_type, size_bytes, sha256 in rows
            )
        except (TypeError, ValueError):
            raise RunSkillTreeMaterializationStale(
                "Pinned Skill Version file metadata is invalid",
            ) from None
        return metadata

    async def _read_and_write_content_batch(
        self,
        session: AsyncSession,
        *,
        run_plan: RunSkillTreeMaterializationPlan,
        version_plan: PinnedSkillVersionPlan,
        batch: V4ContentBatch,
        builder: MaterializingRunSkillTree,
        source_relative_root: str,
    ) -> None:
        ref = RunSkillVersionRefRow
        file = SkillVersionFileRow
        result = await session.stream(
            select(
                file.path,
                file.size_bytes,
                file.sha256,
                file.content,
            )
            .select_from(ref)
            .join(file, file.skill_version_id == ref.skill_version_id)
            .where(
                ref.project_id == run_plan.project_id,
                ref.owner_user_id == run_plan.owner_user_id,
                ref.thread_id == run_plan.thread_id,
                ref.run_id == run_plan.run_id,
                ref.asset_kind == AssetKind.SKILL.value,
                ref.dependency_order == version_plan.dependency_order,
                ref.skill_id == version_plan.asset_id,
                ref.skill_version_id == version_plan.version_id,
                file.path.collate("C") >= batch.first_path,
                file.path.collate("C") <= batch.last_path,
            )
            .order_by(file.path.collate("C"))
        )
        observed_count = 0
        observed_bytes = 0
        try:
            async for row in result:
                if observed_count >= batch.file_count:
                    raise RunSkillTreeMaterializationStale(
                        "Pinned Skill Version content batch has extra rows",
                    )
                expected = batch.expected_metadata[observed_count]
                path, size_bytes, sha256, content = row
                if type(content) is not bytes or path != expected.path or size_bytes != expected.size_bytes or sha256 != expected.sha256 or len(content) != expected.size_bytes or hashlib.sha256(content).hexdigest() != expected.sha256:
                    raise RunSkillTreeMaterializationStale(
                        "Pinned Skill Version content changed",
                    )
                await builder.stage_source_file(
                    f"{source_relative_root}/{path}",
                    content,
                )
                observed_count += 1
                observed_bytes += len(content)
                del content, row
        finally:
            await result.close()
        if observed_count != batch.file_count or observed_bytes != batch.content_size_bytes:
            raise RunSkillTreeMaterializationStale(
                "Pinned Skill Version content batch is incomplete",
            )


class RunSkillTreeMaterializer:
    """Deep Worker module for bounded reads and uniquely-owned Skill trees."""

    def __init__(
        self,
        *,
        materialization_root: Path,
        worker_config: WorkerConfig,
        legacy_source_adapter: LegacyInlineRunSkillSourceAdapter | None = None,
        pinned_source_adapter: PinnedSkillVersionSourceAdapter | None = None,
    ) -> None:
        if (
            not isinstance(materialization_root, Path)
            or not materialization_root.is_absolute()
            or ".." in materialization_root.parts
            or type(worker_config) is not WorkerConfig
            or (legacy_source_adapter is not None and type(legacy_source_adapter) is not LegacyInlineRunSkillSourceAdapter)
            or (pinned_source_adapter is not None and type(pinned_source_adapter) is not PinnedSkillVersionSourceAdapter)
        ):
            raise ValueError("Invalid Run Skill materializer configuration")
        if legacy_source_adapter is not None and worker_config.materialization_max_inflight_bytes < LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES:
            raise ValueError("Run Skill materialization budget does not cover the legacy envelope")
        self._materialization_root = materialization_root
        self._worker_config = worker_config
        self._legacy_source_adapter = legacy_source_adapter
        self._pinned_source_adapter = pinned_source_adapter
        self._memory_budget = MaterializationMemoryBudget.process_wide(
            capacity_bytes=worker_config.materialization_max_inflight_bytes,
            v4_capacity_bytes=(worker_config.materialization_v4_max_inflight_bytes),
        )

    async def materialize(
        self,
        *,
        plan: RunSkillTreeMaterializationPlan,
        authority: RunSkillMaterializationAuthority,
    ) -> PendingMaterializedRunSkillTree:
        if (
            type(plan) is not RunSkillTreeMaterializationPlan
            or any(type(value) is LegacyInlineRunSkillPlan and self._legacy_source_adapter is None for value in plan.skill_versions)
            or any(type(value) is PinnedSkillVersionPlan and self._pinned_source_adapter is None for value in plan.skill_versions)
        ):
            raise ValueError("Invalid Run Skill materialization request")
        await self._assert_authority(
            plan,
            authority,
            boundary="initial",
            dependency_order=None,
        )
        builder = await self.begin_attempt(plan.attempt_identity)
        manifests: list[PrivateSkillManifest] = []
        skills: list[Skill] = []
        runtime_name_assets: dict[str, uuid.UUID] = {}
        materialized_asset_ids: set[uuid.UUID] = set()
        try:
            for version_plan in plan.skill_versions:
                await self._assert_authority(
                    plan,
                    authority,
                    boundary="version",
                    dependency_order=version_plan.dependency_order,
                )
                if type(version_plan) is LegacyInlineRunSkillPlan:
                    legacy_source_adapter = self._legacy_source_adapter
                    if legacy_source_adapter is None:
                        raise ValueError("Legacy Run Skill source is unavailable")
                    materialized = await legacy_source_adapter.materialize_version(
                        run_plan=plan,
                        version_plan=version_plan,
                        builder=builder,
                        memory_budget=self._memory_budget,
                        batch_planner=self,
                    )
                else:
                    pinned_source_adapter = self._pinned_source_adapter
                    if pinned_source_adapter is None:
                        raise ValueError("Pinned Skill Version source is unavailable")
                    materialized = await pinned_source_adapter.materialize_version(
                        run_plan=plan,
                        version_plan=version_plan,
                        builder=builder,
                        memory_budget=self._memory_budget,
                        batch_planner=self,
                    )
                parsed = materialized.parsed
                existing_asset = runtime_name_assets.get(parsed.name)
                if existing_asset is not None and existing_asset != version_plan.asset_id:
                    raise RunSkillTreeMaterializationStale(
                        "Run Skill runtime name conflicts",
                    )
                runtime_name_assets[parsed.name] = version_plan.asset_id
                category = SkillCategory.PUBLIC if version_plan.scope is AssetScope.SYSTEM else SkillCategory.CUSTOM
                first_asset_version = version_plan.asset_id not in materialized_asset_ids
                materialized_asset_ids.add(version_plan.asset_id)
                base_relative_root = parsed.name if category is SkillCategory.PUBLIC else version_plan.asset_id.hex
                relative_root = base_relative_root if first_asset_version else (f".versions/{version_plan.asset_id.hex}/{version_plan.version_id.hex}")
                await builder.promote_staged_skill(
                    source_relative_root=materialized.source_relative_root,
                    destination_relative_root=(f"{category.value}/{relative_root}"),
                )
                future_skill_root = builder.published_tree_root / category.value / relative_root
                manifests.append(
                    PrivateSkillManifest(
                        asset_id=version_plan.asset_id,
                        version_id=version_plan.version_id,
                        relative_root=relative_root,
                    )
                )
                skills.append(
                    replace(
                        parsed,
                        skill_dir=future_skill_root,
                        skill_file=future_skill_root / "SKILL.md",
                        relative_path=Path(relative_root),
                        category=category,
                        enabled=True,
                        runtime_read_only=True,
                    )
                )
            await self._assert_authority(
                plan,
                authority,
                boundary="final",
                dependency_order=None,
            )
            return await builder.publish(
                manifests=tuple(manifests),
                skills=tuple(skills),
            )
        except BaseException as primary:
            try:
                await builder.aclose()
            except BaseException as cleanup_error:
                raise primary from cleanup_error
            raise

    @staticmethod
    async def _assert_authority(
        plan: RunSkillTreeMaterializationPlan,
        authority: RunSkillMaterializationAuthority,
        *,
        boundary: Literal["initial", "version", "final"],
        dependency_order: int | None,
    ) -> None:
        read = getattr(authority, "read_materialization_authority", None)
        if not callable(read):
            raise ValueError("Invalid Run Skill materialization authority")
        readback = await read(
            boundary=boundary,
            dependency_order=dependency_order,
        )
        if type(readback) is not MaterializationAuthorityReadback or readback.attempt_identity != plan.attempt_identity or readback.plan_fingerprint != plan.plan_fingerprint:
            raise RunSkillTreeMaterializationStale(
                "Run Skill materialization authority changed",
            )

    async def begin_attempt(
        self,
        identity: MaterializationAttemptIdentity,
    ) -> MaterializingRunSkillTree:
        if type(identity) is not MaterializationAttemptIdentity:
            raise ValueError("Invalid materialization Attempt identity")
        owner_id = uuid.uuid4()
        owner_root = self._materialization_root / owner_id.hex
        try:
            metadata = await joined_to_thread(
                _create_owner_root,
                self._materialization_root,
                owner_id,
                identity,
            )
        except BaseException as primary:
            try:
                await joined_to_thread(_remove_owner_root, owner_root)
            except BaseException as cleanup_error:
                raise primary from cleanup_error
            raise
        return MaterializingRunSkillTree(
            owner_root=owner_root,
            metadata=metadata,
        )

    async def inspect_owner(
        self,
        owner_id: uuid.UUID,
    ) -> MaterializationOwnerMetadata:
        if type(owner_id) is not uuid.UUID:
            raise ValueError("Invalid materialization owner")
        return await joined_to_thread(
            _read_owner_metadata,
            self._materialization_root / owner_id.hex,
            expected_owner_id=owner_id,
        )

    def reserve_v4_source(
        self,
        *,
        content_size_bytes: int,
    ) -> _MaterializationMemoryReservation:
        return self._memory_budget.reserve_v4(
            content_size_bytes=content_size_bytes,
        )

    def reserve_legacy_source(
        self,
        *,
        envelope_bytes: int,
    ) -> _MaterializationMemoryReservation:
        return self._memory_budget.reserve_legacy(envelope_bytes=envelope_bytes)

    def plan_v4_content_batches(
        self,
        metadata: Sequence[SkillVersionFileMetadata],
    ) -> tuple[V4ContentBatch, ...]:
        rows = tuple(metadata)
        self._validate_metadata(rows)
        batches: list[V4ContentBatch] = []
        current: list[SkillVersionFileMetadata] = []
        current_bytes = 0

        def flush() -> None:
            nonlocal current, current_bytes
            if not current:
                return
            paths = tuple(row.path for row in current)
            batches.append(
                V4ContentBatch(
                    first_path=paths[0],
                    last_path=paths[-1],
                    expected_paths=paths,
                    file_count=len(paths),
                    content_size_bytes=current_bytes,
                    oversized_singleton=False,
                    expected_metadata=tuple(current),
                )
            )
            current = []
            current_bytes = 0

        for row in rows:
            if row.size_bytes > self._worker_config.materialization_batch_max_bytes:
                flush()
                batches.append(
                    V4ContentBatch(
                        first_path=row.path,
                        last_path=row.path,
                        expected_paths=(row.path,),
                        file_count=1,
                        content_size_bytes=row.size_bytes,
                        oversized_singleton=True,
                        expected_metadata=(row,),
                    )
                )
                continue
            if current and (len(current) >= self._worker_config.materialization_batch_max_files or current_bytes + row.size_bytes > self._worker_config.materialization_batch_max_bytes):
                flush()
            current.append(row)
            current_bytes += row.size_bytes
        flush()
        return tuple(batches)

    @staticmethod
    def validate_v4_content_batch(
        batch: V4ContentBatch,
        rows: Sequence[SkillVersionFileContent],
    ) -> None:
        if type(batch) is not V4ContentBatch:
            raise ValueError("Invalid v4 content batch")
        result = tuple(rows)
        if any(type(row) is not SkillVersionFileContent for row in result):
            raise ValueError("Invalid v4 content query result")
        if tuple(row.path for row in result) != batch.expected_paths:
            raise ValueError("v4 content query result does not match its plan")
        if len(result) != batch.file_count or sum(row.size_bytes for row in result) != batch.content_size_bytes:
            raise ValueError("v4 content query result does not match its plan")
        for expected, actual in zip(
            batch.expected_metadata,
            result,
            strict=True,
        ):
            if actual.path != expected.path or actual.size_bytes != expected.size_bytes or actual.sha256 != expected.sha256 or len(actual.content) != expected.size_bytes or hashlib.sha256(actual.content).hexdigest() != expected.sha256:
                raise ValueError("v4 content query result does not match its plan")

    def archive_facts(
        self,
        metadata: Sequence[SkillVersionFileMetadata],
    ) -> MaterializedSkillArchiveFacts:
        rows = tuple(metadata)
        self._validate_metadata(rows)
        digest = hashlib.sha256()
        digest.update(b"[")
        total_bytes = 0
        for index, row in enumerate(rows):
            if index:
                digest.update(b",")
            digest.update(
                json.dumps(
                    {
                        "path": row.path,
                        "sha256": row.sha256,
                        "size_bytes": row.size_bytes,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
            total_bytes += row.size_bytes
        digest.update(b"]")
        return MaterializedSkillArchiveFacts(
            file_count=len(rows),
            content_size_bytes=total_bytes,
            payload_checksum=digest.hexdigest(),
        )

    @staticmethod
    def _validate_metadata(
        rows: tuple[SkillVersionFileMetadata, ...],
    ) -> None:
        if not rows or len(rows) > MAX_SKILL_ARCHIVE_FILES:
            raise ValueError("Skill Version file count is invalid")
        if any(type(row) is not SkillVersionFileMetadata for row in rows):
            raise ValueError("Skill Version file metadata is invalid")
        if rows != tuple(sorted(rows, key=lambda row: row.path)):
            raise ValueError("Skill Version file order is not canonical")
        paths: set[str] = set()
        identities: set[str] = set()
        total_size = 0
        for row in rows:
            if _canonical_skill_path(row.path) != row.path:
                raise ValueError("Skill Version file path is not canonical")
            identity = unicodedata.normalize("NFC", row.path.casefold())
            identity_parts = PurePosixPath(identity).parts
            if row.path in paths or identity in identities or any(PurePosixPath(*identity_parts[:index]).as_posix() in identities for index in range(1, len(identity_parts))):
                raise ValueError("Skill Version file path is duplicated")
            if len(row.media_type) > _MAX_SKILL_MEDIA_TYPE_CHARS or len(row.media_type.encode("utf-8")) > _MAX_SKILL_MEDIA_TYPE_BYTES:
                raise ValueError("Skill Version media type is invalid")
            if row.size_bytes > MAX_SKILL_ARCHIVE_FILE_BYTES:
                raise ValueError("Skill Version file exceeds 64 MiB")
            total_size += row.size_bytes
            if total_size > MAX_SKILL_ARCHIVE_BYTES:
                raise ValueError("Skill Version content exceeds 100 MiB")
            paths.add(row.path)
            identities.add(identity)
        for identity in identities:
            parts = PurePosixPath(identity).parts
            if any(PurePosixPath(*parts[:index]).as_posix() in identities for index in range(1, len(parts))):
                raise ValueError("Skill Version file path is duplicated")


__all__ = [
    "MaterializationAttemptIdentity",
    "MaterializationAuthorityReadback",
    "MaterializationMemoryBudget",
    "MaterializationOwnerMetadata",
    "MaterializedSkillArchiveFacts",
    "MaterializedRunSkillTreeStateError",
    "MaterializedRunSkillTreeOwner",
    "MaterializingRunSkillTree",
    "MAX_LEGACY_V2_RUN_SKILL_SNAPSHOT_JSON_BYTES",
    "LegacyInlineRunSkillPlan",
    "LegacyInlineRunSkillSourceAdapter",
    "PendingMaterializedRunSkillTree",
    "PinnedSkillVersionPlan",
    "PinnedSkillVersionSourceAdapter",
    "RuntimeOwnedMaterializedRunSkillTree",
    "RunSkillMaterializationAuthority",
    "RunSkillTreeMaterializer",
    "RunSkillTreeMaterializationPlan",
    "RunSkillTreeMaterializationStale",
    "SkillVersionFileContent",
    "SkillVersionFileMetadata",
    "V4ContentBatch",
    "read_materialization_owner_metadata",
    "remove_materialization_owner_if_unchanged",
]
