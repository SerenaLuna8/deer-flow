import asyncio
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from deerflow.config import get_app_config
from deerflow.error_codes import PublicRunError, PublicRunErrorCode
from deerflow.private_scope import PrivateResourceScope
from deerflow.reflection import resolve_class
from deerflow.sandbox.exceptions import SandboxRuntimeError
from deerflow.sandbox.sandbox import Sandbox

_PRIVATE_SANDBOX_SECURE_ROOTS = (
    "/mnt/user-data/workspace",
    "/mnt/user-data/uploads",
    "/mnt/user-data/outputs",
)

_PROVIDER_KIND = re.compile(r"[a-z0-9](?:[a-z0-9_.-]{0,63})\Z")
_REASON_CODE = re.compile(r"[a-z0-9](?:[a-z0-9_]{0,63})\Z")
RUN_READONLY_MOUNT_MANIFEST_PATH = ".actweave-run-mount.json"
RUN_READONLY_MOUNT_MANIFEST_MAX_BYTES = 512
_RUN_READONLY_MOUNT_MAX_ENTRIES = 100_000


def _validated_opaque_identifier(value: str, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > 255 or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"Invalid {field}")
    return value


def run_readonly_mount_manifest_text(owner_id: uuid.UUID) -> str:
    """Return the canonical, bounded owner manifest written into one tree."""

    if type(owner_id) is not uuid.UUID:
        raise ValueError("Invalid run read-only mount owner")
    return (
        json.dumps(
            {
                "owner_id": str(owner_id),
                "schema_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


@dataclass(frozen=True, slots=True)
class ValidatedRunReadonlyMountSource:
    source: "RunReadonlyMountSource"
    probe_relative_path: str
    probe_content: str


def validate_run_readonly_mount_source(
    source: "RunReadonlyMountSource",
    *,
    trusted_root: Path,
) -> ValidatedRunReadonlyMountSource:
    """Validate the provider-independent owner/tree and bounded probe contract."""

    if type(source) is not RunReadonlyMountSource or not isinstance(trusted_root, Path) or not trusted_root.is_absolute() or ".." in trusted_root.parts:
        raise SandboxRuntimeError("Invalid run read-only mount source")
    owner_root = trusted_root / source.owner_id.hex
    expected_tree = owner_root / "tree"
    try:
        trusted_status = trusted_root.lstat()
        trusted_resolved = trusted_root.resolve(strict=True)
        owner_status = owner_root.lstat()
        tree_status = source.worker_root.lstat()
        tree_resolved = source.worker_root.resolve(strict=True)
    except OSError as exc:
        raise SandboxRuntimeError("Untrusted run read-only mount source") from exc
    if (
        source.worker_root != expected_tree
        or stat.S_ISLNK(trusted_status.st_mode)
        or not stat.S_ISDIR(trusted_status.st_mode)
        or stat.S_ISLNK(owner_status.st_mode)
        or not stat.S_ISDIR(owner_status.st_mode)
        or stat.S_IMODE(owner_status.st_mode) != 0o700
        or stat.S_ISLNK(tree_status.st_mode)
        or not stat.S_ISDIR(tree_status.st_mode)
        or stat.S_IMODE(tree_status.st_mode) != 0o555
        or tree_resolved != trusted_resolved / source.owner_id.hex / "tree"
    ):
        raise SandboxRuntimeError("Untrusted run read-only mount source")

    skill_manifest_found = False
    entry_count = 0
    try:
        for path in source.worker_root.rglob("*"):
            entry_count += 1
            if entry_count > _RUN_READONLY_MOUNT_MAX_ENTRIES:
                raise SandboxRuntimeError("Run read-only mount has too many entries")
            status = path.lstat()
            mode = status.st_mode
            if stat.S_ISLNK(mode):
                raise SandboxRuntimeError("Untrusted run read-only mount source")
            if stat.S_ISDIR(mode):
                if stat.S_IMODE(mode) != 0o555:
                    raise SandboxRuntimeError("Run read-only mount directory mode is invalid")
                continue
            if not stat.S_ISREG(mode) or status.st_nlink != 1:
                raise SandboxRuntimeError("Untrusted run read-only mount source")
            if stat.S_IMODE(mode) != 0o444:
                raise SandboxRuntimeError("Run read-only mount file mode is invalid")
            if path.name == "SKILL.md":
                skill_manifest_found = True
    except OSError as exc:
        raise SandboxRuntimeError("Untrusted run read-only mount source") from exc
    if not skill_manifest_found:
        raise SandboxRuntimeError("Run read-only mount has no Skill manifest")

    manifest_path = source.worker_root / RUN_READONLY_MOUNT_MANIFEST_PATH
    descriptor = -1
    try:
        if not hasattr(os, "O_NOFOLLOW"):
            raise OSError("No-follow file reads are unavailable")
        descriptor = os.open(
            manifest_path,
            os.O_RDONLY | os.O_NOFOLLOW,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o444 or before.st_size > RUN_READONLY_MOUNT_MANIFEST_MAX_BYTES:
            raise OSError("Invalid run read-only mount manifest")
        content = os.read(
            descriptor,
            RUN_READONLY_MOUNT_MANIFEST_MAX_BYTES + 1,
        )
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size) or len(content) != before.st_size:
            raise OSError("Run read-only mount manifest changed")
    except OSError as exc:
        raise SandboxRuntimeError("Run read-only mount manifest is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    expected = run_readonly_mount_manifest_text(source.owner_id).encode("utf-8")
    if content != expected:
        raise SandboxRuntimeError("Run read-only mount manifest owner mismatch")
    return ValidatedRunReadonlyMountSource(
        source=source,
        probe_relative_path=RUN_READONLY_MOUNT_MANIFEST_PATH,
        probe_content=expected.decode("utf-8"),
    )


@dataclass(frozen=True, slots=True)
class RunReadonlyMountSource:
    """Materializer-owned immutable tree offered to one Sandbox provider."""

    owner_id: uuid.UUID
    worker_root: Path

    def __post_init__(self) -> None:
        if type(self.owner_id) is not uuid.UUID or not isinstance(
            self.worker_root,
            Path,
        ):
            raise ValueError("Invalid run read-only mount source")
        if not self.worker_root.is_absolute() or ".." in self.worker_root.parts or "\x00" in str(self.worker_root):
            raise ValueError("Invalid run read-only mount source")


@dataclass(frozen=True, slots=True)
class ProviderRunMountLease:
    """Exact provider coordinates for one acquired run read-only mount."""

    owner_id: uuid.UUID
    provider_kind: str
    sandbox_id: str
    mount_lease_id: str

    def __post_init__(self) -> None:
        if (
            type(self.owner_id) is not uuid.UUID
            or _PROVIDER_KIND.fullmatch(
                self.provider_kind,
            )
            is None
        ):
            raise ValueError("Invalid provider run mount lease")
        _validated_opaque_identifier(self.sandbox_id, field="sandbox identifier")
        _validated_opaque_identifier(
            self.mount_lease_id,
            field="mount lease identifier",
        )

    def matches_source(self, source: RunReadonlyMountSource) -> bool:
        return type(source) is RunReadonlyMountSource and self.owner_id == source.owner_id

    def _coordinates(self) -> tuple[uuid.UUID, str, str, str]:
        return (
            self.owner_id,
            self.provider_kind,
            self.sandbox_id,
            self.mount_lease_id,
        )


@dataclass(frozen=True, slots=True)
class ProviderMountAbsentProof:
    """Provider-confirmed absence for the exact lease coordinates."""

    owner_id: uuid.UUID
    provider_kind: str
    sandbox_id: str
    mount_lease_id: str

    def __post_init__(self) -> None:
        ProviderRunMountLease(
            owner_id=self.owner_id,
            provider_kind=self.provider_kind,
            sandbox_id=self.sandbox_id,
            mount_lease_id=self.mount_lease_id,
        )

    @classmethod
    def from_lease(
        cls,
        lease: ProviderRunMountLease,
    ) -> "ProviderMountAbsentProof":
        if type(lease) is not ProviderRunMountLease:
            raise ValueError("Invalid provider run mount lease")
        return cls(
            owner_id=lease.owner_id,
            provider_kind=lease.provider_kind,
            sandbox_id=lease.sandbox_id,
            mount_lease_id=lease.mount_lease_id,
        )

    def matches_lease(self, lease: ProviderRunMountLease) -> bool:
        return type(lease) is ProviderRunMountLease and self._coordinates() == lease._coordinates()

    def _coordinates(self) -> tuple[uuid.UUID, str, str, str]:
        return (
            self.owner_id,
            self.provider_kind,
            self.sandbox_id,
            self.mount_lease_id,
        )


@dataclass(frozen=True, slots=True)
class ProviderRunMountOwnerAbsentProof:
    """Provider-confirmed absence of every mount carrying one exact owner label."""

    owner_id: uuid.UUID
    provider_kind: str

    def __post_init__(self) -> None:
        if type(self.owner_id) is not uuid.UUID or _PROVIDER_KIND.fullmatch(self.provider_kind) is None:
            raise ValueError("Invalid provider owner absence proof")

    def matches_owner(self, owner_id: uuid.UUID) -> bool:
        return type(owner_id) is uuid.UUID and self.owner_id == owner_id


@dataclass(frozen=True, slots=True)
class ProviderRunMountOwnerUnknown:
    """Fail-closed provider reconciliation result without absence proof."""

    owner_id: uuid.UUID
    reason_code: str
    provider_kind: str | None = None

    def __post_init__(self) -> None:
        if type(self.owner_id) is not uuid.UUID or _REASON_CODE.fullmatch(self.reason_code) is None or (self.provider_kind is not None and _PROVIDER_KIND.fullmatch(self.provider_kind) is None):
            raise ValueError("Invalid provider owner reconciliation result")


type ProviderRunMountOwnerReconciliation = ProviderRunMountOwnerAbsentProof | ProviderRunMountOwnerUnknown


@dataclass(frozen=True, slots=True)
class NotAcquired:
    """Proof that provider acquisition never began for this owner."""

    owner_id: uuid.UUID
    last_lifecycle_state: Literal["materialized"] = "materialized"

    def __post_init__(self) -> None:
        if type(self.owner_id) is not uuid.UUID or self.last_lifecycle_state != "materialized":
            raise ValueError("Invalid not-acquired mount outcome")

    def matches_source(self, source: RunReadonlyMountSource) -> bool:
        return type(source) is RunReadonlyMountSource and self.owner_id == source.owner_id


@dataclass(frozen=True, slots=True)
class Released:
    """Terminal release backed by exact provider absence proof."""

    proof: ProviderMountAbsentProof

    def __post_init__(self) -> None:
        if type(self.proof) is not ProviderMountAbsentProof:
            raise ValueError("Invalid released mount outcome")

    @property
    def owner_id(self) -> uuid.UUID:
        return self.proof.owner_id

    def matches_source(self, source: RunReadonlyMountSource) -> bool:
        return type(source) is RunReadonlyMountSource and self.owner_id == source.owner_id

    def matches_lease(self, lease: ProviderRunMountLease) -> bool:
        return self.proof.matches_lease(lease)


@dataclass(frozen=True, slots=True)
class Orphaned:
    """Stable unknown outcome retained until exact provider readback succeeds."""

    owner_id: uuid.UUID
    reason_code: str
    last_lifecycle_state: Literal["acquiring", "mounted", "release_pending"]
    provider_kind: str | None = None
    sandbox_id: str | None = None
    mount_lease_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.owner_id) is not uuid.UUID or _REASON_CODE.fullmatch(self.reason_code) is None or self.last_lifecycle_state not in {"acquiring", "mounted", "release_pending"}:
            raise ValueError("Invalid orphaned mount outcome")
        coordinates = (
            self.provider_kind,
            self.sandbox_id,
            self.mount_lease_id,
        )
        if any(value is None for value in coordinates):
            if any(value is not None for value in coordinates):
                raise ValueError("Incomplete orphaned mount lease")
            return
        ProviderRunMountLease(
            owner_id=self.owner_id,
            provider_kind=self.provider_kind,
            sandbox_id=self.sandbox_id,
            mount_lease_id=self.mount_lease_id,
        )

    @classmethod
    def from_lease(
        cls,
        lease: ProviderRunMountLease,
        *,
        reason_code: str,
        last_lifecycle_state: Literal[
            "acquiring",
            "mounted",
            "release_pending",
        ],
    ) -> "Orphaned":
        if type(lease) is not ProviderRunMountLease:
            raise ValueError("Invalid provider run mount lease")
        return cls(
            owner_id=lease.owner_id,
            provider_kind=lease.provider_kind,
            sandbox_id=lease.sandbox_id,
            mount_lease_id=lease.mount_lease_id,
            reason_code=reason_code,
            last_lifecycle_state=last_lifecycle_state,
        )

    def matches_source(self, source: RunReadonlyMountSource) -> bool:
        return type(source) is RunReadonlyMountSource and self.owner_id == source.owner_id

    def matches_lease(self, lease: ProviderRunMountLease) -> bool:
        return (
            type(lease) is ProviderRunMountLease
            and self.provider_kind is not None
            and (
                self.owner_id,
                self.provider_kind,
                self.sandbox_id,
                self.mount_lease_id,
            )
            == lease._coordinates()
        )


type RunMountReleaseOutcome = NotAcquired | Released | Orphaned


def merge_run_mount_release_outcome(
    previous: RunMountReleaseOutcome,
    observed: RunMountReleaseOutcome,
) -> RunMountReleaseOutcome:
    """Keep release evidence monotonic under retries and late readback."""

    if type(previous) not in {NotAcquired, Released, Orphaned} or type(
        observed,
    ) not in {NotAcquired, Released, Orphaned}:
        raise TypeError("Invalid run mount release outcome")
    if previous.owner_id != observed.owner_id:
        raise ValueError("Run mount outcomes have different owners")
    if type(previous) is NotAcquired:
        if type(observed) is NotAcquired:
            return previous
        raise ValueError("Run mount not-acquired proof conflicts with provider evidence")

    previous_lease = (
        previous.proof._coordinates()
        if type(previous) is Released
        else (
            (
                previous.owner_id,
                previous.provider_kind,
                previous.sandbox_id,
                previous.mount_lease_id,
            )
            if type(previous) is Orphaned and previous.provider_kind is not None
            else None
        )
    )
    observed_lease = (
        observed.proof._coordinates()
        if type(observed) is Released
        else (
            (
                observed.owner_id,
                observed.provider_kind,
                observed.sandbox_id,
                observed.mount_lease_id,
            )
            if type(observed) is Orphaned and observed.provider_kind is not None
            else None
        )
    )
    if previous_lease is not None and observed_lease is not None and previous_lease != observed_lease:
        raise ValueError("Run mount outcomes refer to a different mount lease")

    if type(previous) is Released:
        return previous
    if type(observed) is Released:
        return observed
    if type(observed) is NotAcquired:
        raise ValueError("Provider acquisition cannot become not-acquired")
    return previous


class RunMountAcquireCancelled(asyncio.CancelledError):
    """Cancellation carrying the acquired lease and its cleanup evidence."""

    __slots__ = ("_lease", "_release_outcome")

    def __init__(
        self,
        lease: ProviderRunMountLease,
        release_outcome: Released | Orphaned,
    ) -> None:
        if (
            type(lease) is not ProviderRunMountLease
            or type(
                release_outcome,
            )
            not in {Released, Orphaned}
            or not release_outcome.matches_lease(lease)
        ):
            raise ValueError("Invalid cancelled run mount cleanup evidence")
        super().__init__("Run read-only mount acquisition was cancelled")
        self._lease = lease
        self._release_outcome = release_outcome

    @property
    def lease(self) -> ProviderRunMountLease:
        return self._lease

    @property
    def release_outcome(self) -> Released | Orphaned:
        return self._release_outcome


class RunMountReleaseCancelled(asyncio.CancelledError):
    """Cancellation carrying the completed provider release evidence."""

    __slots__ = ("_lease", "_release_outcome")

    def __init__(
        self,
        lease: ProviderRunMountLease,
        release_outcome: RunMountReleaseOutcome,
    ) -> None:
        if type(lease) is not ProviderRunMountLease or type(
            release_outcome,
        ) not in {NotAcquired, Released, Orphaned}:
            raise ValueError("Invalid cancelled run mount release evidence")
        if type(release_outcome) is NotAcquired:
            matches = release_outcome.owner_id == lease.owner_id
        else:
            matches = release_outcome.matches_lease(lease)
        if not matches:
            raise ValueError("Cancelled run mount release evidence mismatch")
        super().__init__("Run read-only mount release was cancelled")
        self._lease = lease
        self._release_outcome = release_outcome

    @property
    def lease(self) -> ProviderRunMountLease:
        return self._lease

    @property
    def release_outcome(self) -> RunMountReleaseOutcome:
        return self._release_outcome


async def _await_joined_thread(task: asyncio.Task) -> tuple[object, bool]:
    """Join a blocking provider call even when its async caller is cancelled."""

    cancellation_pending = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancellation_pending = True
    return task.result(), cancellation_pending


@dataclass(frozen=True, slots=True)
class RunScopedReadOnlyMount:
    """Trusted run-owned host tree exposed at one read-only sandbox path."""

    run_id: str
    container_path: str
    host_path: str

    def __post_init__(self) -> None:
        normalized_container = PurePosixPath(self.container_path).as_posix()
        windows_host = PureWindowsPath(self.host_path)
        host_is_absolute = Path(self.host_path).is_absolute() or windows_host.is_absolute()
        if not self.run_id or not self.container_path.startswith("/") or ".." in PurePosixPath(self.container_path).parts or normalized_container != self.container_path.rstrip("/") or not host_is_absolute or ".." in windows_host.parts:
            raise ValueError("Invalid run-scoped read-only mount")


@dataclass(frozen=True, slots=True)
class PrivateSandboxLease:
    sandbox_id: str
    run_id: str
    relative_root: str


def private_sandbox_relative_root(
    scope: PrivateResourceScope,
    thread_id: str,
) -> str:
    if type(scope) is not PrivateResourceScope:
        raise SandboxRuntimeError("Invalid private sandbox scope")
    if not thread_id or "/" in thread_id or "\\" in thread_id or thread_id in {".", ".."}:
        raise SandboxRuntimeError("Invalid private sandbox thread")
    return f"projects/{scope.project_id}/users/{scope.owner_user_id}/threads/{thread_id}"


class SandboxProvider(ABC):
    """Abstract base class for sandbox providers"""

    uses_thread_data_mounts: bool = False
    needs_upload_permission_adjustment: bool = True
    _supports_isolated_private_file_authority: bool = False

    def run_readonly_mounts_ready(self) -> bool:
        """Return whether this process can execute exact v4 Skill mounts."""

        return False

    @staticmethod
    def _private_storage_key(scope: PrivateResourceScope) -> str:
        relative = private_sandbox_relative_root(scope, "scope")
        return f"private-{hashlib.sha256(relative.encode()).hexdigest()[:24]}"

    def acquire_private(
        self,
        thread_id: str,
        *,
        scope: PrivateResourceScope,
        user_id: str,
        run_id: str,
        mounts: tuple[RunScopedReadOnlyMount, ...] = (),
    ) -> PrivateSandboxLease:
        """Acquire one fresh, scope-bound private lease or fail closed.

        A private authority requires bounded, no-link-following secure file
        primitives in addition to allocation isolation. Providers must opt in
        by overriding this method; reusing legacy acquire is not sufficient.
        """

        if not self._supports_isolated_private_file_authority:
            raise SandboxRuntimeError("Private file authority is unsupported by this sandbox provider")
        if type(scope) is not PrivateResourceScope:
            raise SandboxRuntimeError("Invalid private sandbox scope")
        if user_id != scope.owner_user_id:
            raise SandboxRuntimeError("Private sandbox owner mismatch")
        if not run_id or "/" in run_id or "\\" in run_id:
            raise SandboxRuntimeError("Invalid private sandbox run")
        relative_root = private_sandbox_relative_root(scope, thread_id)
        for mount in mounts:
            if type(mount) is not RunScopedReadOnlyMount or mount.run_id != run_id:
                raise SandboxRuntimeError("Invalid private sandbox mount")

        sandbox_id: str | None = None
        try:
            sandbox_id = self._acquire_private_fresh(
                scope=scope,
                thread_id=thread_id,
                run_id=run_id,
                mounts=mounts,
            )
            if not isinstance(sandbox_id, str) or not sandbox_id:
                raise SandboxRuntimeError("Invalid private sandbox identifier")
            sandbox = self.get(sandbox_id)
            if sandbox is None:
                raise SandboxRuntimeError("Private sandbox was not registered")
            # Exercise the real secure metadata boundary before returning a
            # lease.  Merely allocating a fresh VM is not enough.
            for root in _PRIVATE_SANDBOX_SECURE_ROOTS:
                tuple(
                    sandbox.list_secure_files(
                        root,
                        max_entries=1,
                    )
                )
            lock, leases = self._private_lease_state()
            with lock:
                if sandbox_id in leases or any(registered.run_id == run_id for registered in leases.values()):
                    raise SandboxRuntimeError("Private sandbox lease already active")
                lease = PrivateSandboxLease(
                    sandbox_id=sandbox_id,
                    run_id=run_id,
                    relative_root=relative_root,
                )
                leases[sandbox_id] = lease
            return lease
        except BaseException:
            if sandbox_id:
                try:
                    self._destroy_private_sandbox(sandbox_id)
                except Exception:
                    pass
            raise

    def _acquire_private_fresh(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
        mounts: tuple[RunScopedReadOnlyMount, ...],
    ) -> str:
        del scope, thread_id, run_id, mounts
        raise SandboxRuntimeError("Private file authority is unsupported by this sandbox provider")

    def _destroy_private_sandbox(self, sandbox_id: str) -> None:
        self.release(sandbox_id)

    def _private_lease_state(
        self,
    ) -> tuple[threading.RLock, dict[str, PrivateSandboxLease]]:
        lock = getattr(self, "_private_lease_lock", None)
        leases = getattr(self, "_private_leases", None)
        if lock is None or leases is None:
            provider_lock = getattr(self, "_lock", None)
            if provider_lock is None:
                lock = lock or threading.RLock()
                leases = leases or {}
                self._private_lease_lock = lock
                self._private_leases = leases
            else:
                with provider_lock:
                    lock = getattr(self, "_private_lease_lock", None)
                    if lock is None:
                        lock = threading.RLock()
                        self._private_lease_lock = lock
                    leases = getattr(self, "_private_leases", None)
                    if leases is None:
                        leases = {}
                        self._private_leases = leases
        return lock, leases

    def release_private(self, lease: PrivateSandboxLease) -> None:
        """Destroy exactly one private run sandbox; never park it warm."""

        if type(lease) is not PrivateSandboxLease:
            raise SandboxRuntimeError("Invalid private sandbox lease")
        if not self._supports_isolated_private_file_authority:
            # LocalSandboxProvider owns its own private reservation contract and
            # predates the remote registry in this base class.
            self.release(lease.sandbox_id)
            return
        lock, leases = self._private_lease_state()
        with lock:
            registered = leases.get(lease.sandbox_id)
            if registered != lease:
                raise SandboxRuntimeError("Invalid or inactive private sandbox lease")
            releasing = getattr(self, "_private_releasing", None)
            if releasing is None:
                releasing = set()
                self._private_releasing = releasing
            if lease.sandbox_id in releasing:
                raise SandboxRuntimeError("Private sandbox lease is already releasing")
            releasing.add(lease.sandbox_id)
        try:
            self._destroy_private_sandbox(lease.sandbox_id)
        except BaseException:
            with lock:
                releasing.discard(lease.sandbox_id)
            raise
        with lock:
            releasing.discard(lease.sandbox_id)
            leases.pop(lease.sandbox_id, None)

    async def acquire_private_async(
        self,
        thread_id: str,
        *,
        scope: PrivateResourceScope,
        user_id: str,
        run_id: str,
        mounts: tuple[RunScopedReadOnlyMount, ...] = (),
    ) -> PrivateSandboxLease:
        task = asyncio.create_task(
            asyncio.to_thread(
                self.acquire_private,
                thread_id,
                scope=scope,
                user_id=user_id,
                run_id=run_id,
                mounts=mounts,
            )
        )
        result, cancellation_pending = await _await_joined_thread(task)
        lease = result
        if type(lease) is not PrivateSandboxLease:
            raise SandboxRuntimeError("Invalid private sandbox lease")
        if cancellation_pending:
            cleanup_task = asyncio.create_task(asyncio.to_thread(self.release_private, lease))
            await _await_joined_thread(cleanup_task)
            raise asyncio.CancelledError
        return lease

    async def release_private_async(self, lease: PrivateSandboxLease) -> None:
        if type(lease) is not PrivateSandboxLease:
            raise SandboxRuntimeError("Invalid private sandbox lease")
        task = asyncio.create_task(asyncio.to_thread(self.release_private, lease))
        _, cancellation_pending = await _await_joined_thread(task)
        if cancellation_pending:
            raise asyncio.CancelledError

    @abstractmethod
    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        """Acquire a sandbox environment and return its ID.

        Returns:
            The ID of the acquired sandbox environment.
        """
        pass

    async def acquire_async(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        """Acquire a sandbox without blocking the event loop.

        Most sandbox providers expose a synchronous lifecycle API because local
        Docker/provisioner operations are blocking. Async runtimes should call
        this method so those blocking operations run in a worker thread instead
        of stalling the event loop.
        """
        return await asyncio.to_thread(self.acquire, thread_id, user_id=user_id)

    def acquire_with_mounts(
        self,
        thread_id: str,
        *,
        user_id: str,
        mounts: tuple[RunScopedReadOnlyMount, ...],
    ) -> str:
        """Acquire a run-isolated sandbox or fail closed when unsupported."""

        if mounts:
            raise PublicRunError(PublicRunErrorCode.SANDBOX_READ_ONLY_MOUNTS_UNSUPPORTED)
        return self.acquire(thread_id, user_id=user_id)

    async def acquire_with_mounts_async(
        self,
        thread_id: str,
        *,
        user_id: str,
        mounts: tuple[RunScopedReadOnlyMount, ...],
    ) -> str:
        return await asyncio.to_thread(
            self.acquire_with_mounts,
            thread_id,
            user_id=user_id,
            mounts=mounts,
        )

    def validate_run_scoped_mounts(
        self,
        thread_id: str,
        *,
        user_id: str,
        mounts: tuple[RunScopedReadOnlyMount, ...],
    ) -> None:
        """Side-effect-free capability preflight before any model invocation."""

        del thread_id, user_id
        if mounts:
            raise PublicRunError(PublicRunErrorCode.SANDBOX_READ_ONLY_MOUNTS_UNSUPPORTED)

    def release_run_scoped_mounts(
        self,
        thread_id: str,
        *,
        user_id: str,
        mounts: tuple[RunScopedReadOnlyMount, ...],
    ) -> None:
        """Drop provider state owned by one run-specific mount set."""

        del thread_id, user_id, mounts

    async def release_run_scoped_mounts_async(
        self,
        thread_id: str,
        *,
        user_id: str,
        mounts: tuple[RunScopedReadOnlyMount, ...],
    ) -> None:
        await asyncio.to_thread(
            self.release_run_scoped_mounts,
            thread_id,
            user_id=user_id,
            mounts=mounts,
        )

    def prepare_run_readonly_mount(
        self,
        thread_id: str,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        source: RunReadonlyMountSource,
    ) -> ProviderRunMountLease:
        """Acquire and read back one exact provider-owned read-only mount."""

        del thread_id, scope, run_id, source
        raise SandboxRuntimeError(
            "Run read-only mounts are unsupported by this sandbox provider",
        )

    async def prepare_run_readonly_mount_async(
        self,
        thread_id: str,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        source: RunReadonlyMountSource,
    ) -> ProviderRunMountLease:
        task = asyncio.create_task(
            asyncio.to_thread(
                self.prepare_run_readonly_mount,
                thread_id,
                scope=scope,
                run_id=run_id,
                source=source,
            ),
        )
        result, cancellation_pending = await _await_joined_thread(task)
        lease = result
        if type(lease) is not ProviderRunMountLease:
            raise SandboxRuntimeError("Invalid provider run mount lease")
        if cancellation_pending:
            cleanup_task = asyncio.create_task(
                asyncio.to_thread(self.release_run_readonly_mount, lease),
            )
            try:
                cleanup_result, _cleanup_cancelled = await _await_joined_thread(
                    cleanup_task,
                )
            except asyncio.CancelledError:
                cleanup_result = Orphaned.from_lease(
                    lease,
                    reason_code="cancel_cleanup_unconfirmed",
                    last_lifecycle_state="release_pending",
                )
            except Exception:
                cleanup_result = Orphaned.from_lease(
                    lease,
                    reason_code="cancel_cleanup_unconfirmed",
                    last_lifecycle_state="release_pending",
                )
            if type(cleanup_result) not in {Released, Orphaned} or not cleanup_result.matches_lease(lease):
                cleanup_result = Orphaned.from_lease(
                    lease,
                    reason_code="cancel_cleanup_unconfirmed",
                    last_lifecycle_state="release_pending",
                )
            raise RunMountAcquireCancelled(lease, cleanup_result)
        return lease

    def readback_run_readonly_mount(
        self,
        lease: ProviderRunMountLease,
    ) -> ProviderRunMountLease:
        """Confirm the exact lease is active, readable, and read-only."""

        del lease
        raise SandboxRuntimeError(
            "Run read-only mount readback is unsupported by this sandbox provider",
        )

    async def readback_run_readonly_mount_async(
        self,
        lease: ProviderRunMountLease,
    ) -> ProviderRunMountLease:
        task = asyncio.create_task(
            asyncio.to_thread(self.readback_run_readonly_mount, lease),
        )
        result, cancellation_pending = await _await_joined_thread(task)
        if cancellation_pending:
            raise asyncio.CancelledError
        if type(result) is not ProviderRunMountLease:
            raise SandboxRuntimeError("Invalid provider run mount readback")
        return result

    def release_run_readonly_mount(
        self,
        lease: ProviderRunMountLease,
    ) -> RunMountReleaseOutcome:
        """Release the exact lease and return proof-bearing typed evidence."""

        del lease
        raise SandboxRuntimeError(
            "Run read-only mount release is unsupported by this sandbox provider",
        )

    async def release_run_readonly_mount_async(
        self,
        lease: ProviderRunMountLease,
    ) -> RunMountReleaseOutcome:
        task = asyncio.create_task(
            asyncio.to_thread(self.release_run_readonly_mount, lease),
        )
        result, cancellation_pending = await _await_joined_thread(task)
        if type(result) not in {NotAcquired, Released, Orphaned}:
            raise SandboxRuntimeError("Invalid provider run mount release outcome")
        if cancellation_pending:
            raise RunMountReleaseCancelled(lease, result)
        return result

    def ensure_run_readonly_mount_owner_absent(
        self,
        owner_id: uuid.UUID,
        *,
        persisted_lease: ProviderRunMountLease | None,
    ) -> ProviderRunMountOwnerReconciliation:
        """Destroy/read back every exact owner-labeled mount, or fail closed.

        This recovery interface is intentionally separate from ordinary lease
        release: a restarted Worker may only have the durable owner label and
        no recoverable in-process lease. Providers without exact enumeration,
        destroy, and absence readback keep v4 orphan cleanup unavailable.
        """

        if type(owner_id) is not uuid.UUID or (persisted_lease is not None and (type(persisted_lease) is not ProviderRunMountLease or persisted_lease.owner_id != owner_id)):
            raise SandboxRuntimeError("Invalid run read-only mount owner")
        return ProviderRunMountOwnerUnknown(
            owner_id=owner_id,
            provider_kind=(persisted_lease.provider_kind if persisted_lease is not None else None),
            reason_code="owner_reconciliation_unsupported",
        )

    async def ensure_run_readonly_mount_owner_absent_async(
        self,
        owner_id: uuid.UUID,
        *,
        persisted_lease: ProviderRunMountLease | None,
    ) -> ProviderRunMountOwnerReconciliation:
        task = asyncio.create_task(
            asyncio.to_thread(
                self.ensure_run_readonly_mount_owner_absent,
                owner_id,
                persisted_lease=persisted_lease,
            ),
        )
        result, cancellation_pending = await _await_joined_thread(task)
        if cancellation_pending:
            raise asyncio.CancelledError
        if (
            type(result)
            not in {
                ProviderRunMountOwnerAbsentProof,
                ProviderRunMountOwnerUnknown,
            }
            or result.owner_id != owner_id
        ):
            raise SandboxRuntimeError(
                "Invalid provider owner reconciliation result",
            )
        return result

    @abstractmethod
    def get(self, sandbox_id: str) -> Sandbox | None:
        """Get a sandbox environment by ID.

        Args:
            sandbox_id: The ID of the sandbox environment to retain.
        """
        pass

    @abstractmethod
    def release(self, sandbox_id: str) -> None:
        """Release a sandbox environment.

        Args:
            sandbox_id: The ID of the sandbox environment to destroy.
        """
        pass

    def reset(self) -> None:
        """Clear cached state that survives provider instance replacement."""
        pass


_default_sandbox_provider: SandboxProvider | None = None
# Guards every read and write of `_default_sandbox_provider`. The singleton is
# reachable from more than one OS thread (e.g. the main event loop and the Feishu
# channel thread, which runs its own loop), so a bare check-then-create can double
# initialize the provider, and an unsynchronized reset/shutdown racing a get can
# hand a caller `None` or a torn instance. Every access to the global below takes
# this lock, including the read+return in `get_sandbox_provider()`.
#
# The lock guards only the reference swap. Provider callbacks (`__init__`,
# `reset()`, `shutdown()`) and the dynamic import in `resolve_class()` run
# *outside* the lock: they are plugin-supplied (`config.sandbox.use` resolves to
# an arbitrary class) and may be slow or, worse, re-enter these lifecycle
# functions. Holding a non-reentrant `threading.Lock` across them would
# self-deadlock such a provider and would block every concurrent `get()` during a
# slow teardown. Keeping callbacks off the lock avoids both.
_provider_lock = threading.Lock()


def get_sandbox_provider(**kwargs) -> SandboxProvider:
    """Get the sandbox provider singleton.

    Returns a cached singleton instance. Use `reset_sandbox_provider()` to clear
    the cache, or `shutdown_sandbox_provider()` to properly shutdown and clear.

    Returns:
        A sandbox provider instance.
    """
    global _default_sandbox_provider
    # Fast path: a single locked read so a concurrent reset/shutdown can't null
    # the global between the check and the return.
    with _provider_lock:
        if _default_sandbox_provider is not None:
            return _default_sandbox_provider

    # Cold start. Resolve + construct outside the lock: the import and the
    # provider constructor are plugin code and must not run under a non-reentrant
    # lock. The construction may race another caller; we reconcile under the lock.
    config = get_app_config()
    cls = resolve_class(config.sandbox.use, SandboxProvider)
    provider = cls(**kwargs)

    with _provider_lock:
        if _default_sandbox_provider is None:
            _default_sandbox_provider = provider
            return provider
        # We lost the install race: another thread got there first. `winner` is
        # read under the same lock, so it is always a live instance, never None.
        winner = _default_sandbox_provider

    # Discard the instance we just built (outside the lock). For providers with
    # side-effectful constructors (e.g. AioSandboxProvider starts an idle-checker
    # thread), this tears down the orphan so it does not leak — issue #3721.
    if hasattr(provider, "shutdown"):
        provider.shutdown()
    return winner


def reset_sandbox_provider() -> None:
    """Reset the sandbox provider singleton.

    This clears the cached instance without calling shutdown.
    The next call to `get_sandbox_provider()` will create a new instance.
    Useful for testing or when switching configurations.

    Providers can override `reset()` to clear any module-level state they keep
    alive across instances (for example, `LocalSandboxProvider`'s cached
    `LocalSandbox` singleton). Without it, config/mount changes would not take
    effect on the next acquire().

    Note: If the provider has active sandboxes, they will be orphaned.
    Use `shutdown_sandbox_provider()` for proper cleanup.
    """
    global _default_sandbox_provider
    # Detach the reference under the lock, then run the provider's `reset()`
    # callback outside it (see the `_provider_lock` note).
    with _provider_lock:
        provider = _default_sandbox_provider
        _default_sandbox_provider = None
    if provider is not None:
        provider.reset()


def shutdown_sandbox_provider() -> None:
    """Shutdown and reset the sandbox provider.

    This properly shuts down the provider (releasing all sandboxes)
    before clearing the singleton. Call this when the application
    is shutting down or when you need to completely reset the sandbox system.
    """
    global _default_sandbox_provider
    # Detach the reference under the lock, then run the (potentially slow)
    # `shutdown()` callback outside it (see the `_provider_lock` note).
    with _provider_lock:
        provider = _default_sandbox_provider
        _default_sandbox_provider = None
    if provider is not None and hasattr(provider, "shutdown"):
        provider.shutdown()


def set_sandbox_provider(provider: SandboxProvider) -> None:
    """Set a custom sandbox provider instance.

    This allows injecting a custom or mock provider for testing purposes.

    Note: any previously installed provider is replaced but not shut down; the
    caller owns the lifecycle of the instance it is overwriting.

    Args:
        provider: The SandboxProvider instance to use.
    """
    global _default_sandbox_provider
    with _provider_lock:
        _default_sandbox_provider = provider
