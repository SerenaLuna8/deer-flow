import asyncio
import hashlib
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from deerflow.config import get_app_config
from deerflow.private_scope import PrivateResourceScope
from deerflow.reflection import resolve_class
from deerflow.sandbox.exceptions import SandboxRuntimeError
from deerflow.sandbox.sandbox import Sandbox


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
        if not self.run_id or not self.container_path.startswith("/") or ".." in PurePosixPath(self.container_path).parts or normalized_container != self.container_path.rstrip("/") or not Path(self.host_path).is_absolute():
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
        """Acquire a private lease or fail closed when unsupported.

        A private authority requires bounded, no-link-following secure file
        primitives in addition to allocation isolation. Providers must opt in
        by overriding this method; reusing legacy acquire is not sufficient.
        """

        del thread_id, scope, user_id, run_id, mounts
        raise SandboxRuntimeError("Private file authority is unsupported by this sandbox provider")

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
            cleanup_task = asyncio.create_task(asyncio.to_thread(self.release, lease.sandbox_id))
            await _await_joined_thread(cleanup_task)
            raise asyncio.CancelledError
        return lease

    async def release_private_async(self, lease: PrivateSandboxLease) -> None:
        if type(lease) is not PrivateSandboxLease:
            raise SandboxRuntimeError("Invalid private sandbox lease")
        task = asyncio.create_task(asyncio.to_thread(self.release, lease.sandbox_id))
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
            raise SandboxRuntimeError("Configured sandbox provider does not support run-scoped read-only mounts")
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
            raise SandboxRuntimeError("Configured sandbox provider does not support run-scoped read-only mounts")

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
