"""Private Run file finalization and cancellation-safe cleanup.

The Worker may be cancelled repeatedly while a file authority is committing or
releasing resources.  Those operations must finish before the Run admission
barrier is cleared, without cancelling the operation that owns the resource.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from deerflow.file_authority import RunFileAuthority
from deerflow.sandbox.sandbox_provider import (
    NotAcquired,
    Orphaned,
    Released,
    RunMountReleaseOutcome,
)
from deerflow.workspace_changes.types import WorkspaceChangeResult

logger = logging.getLogger(__name__)

_PRIVATE_CLEANUP_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class DeferredCancellationResult[T]:
    """A completed task plus whether its caller was cancelled while joining it."""

    task: asyncio.Task[T]
    cancellation_pending: bool

    def result(self) -> T:
        """Return the joined operation's result, preserving its exact exception."""

        return self.task.result()


async def await_despite_cancellation[T](
    operation: Awaitable[T],
) -> DeferredCancellationResult[T]:
    """Join *operation* while deferring cancellation of the current task.

    ``asyncio.shield`` prevents one cancellation from reaching the operation,
    but the caller can receive additional ``CancelledError`` instances while
    it is still waiting.  Keep joining the same task and report that deferred
    cancellation to the lifecycle owner, which decides when it is safe to
    propagate it.
    """

    task = asyncio.create_task(operation)
    cancellation_pending = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                break
            cancellation_pending = True
        except BaseException:
            # Retrieve and re-raise the operation's exact outcome through
            # ``DeferredCancellationResult.result`` at the policy boundary.
            break
    return DeferredCancellationResult(
        task=task,
        cancellation_pending=cancellation_pending,
    )


@dataclass(slots=True)
class PrivateFileLifecycle:
    """Own one Run's private file authority state and terminal operations."""

    run_id: str
    authority: RunFileAuthority | None
    set_finalizing: Callable[[str, bool], Awaitable[None]]
    cleanup_max_attempts: int = _PRIVATE_CLEANUP_MAX_ATTEMPTS
    _restored: bool = field(default=False, init=False)
    _finalized: bool = field(default=False, init=False)
    _failed: bool = field(default=False, init=False)
    _cleanup_cancellation_pending: bool = field(default=False, init=False)
    _finalization_result: object | None = field(default=None, init=False)
    _release_completed: bool = field(default=False, init=False)
    _release_outcome: RunMountReleaseOutcome | None = field(
        default=None,
        init=False,
    )

    @property
    def enabled(self) -> bool:
        return self.authority is not None

    @property
    def finalization_result(self) -> object | None:
        return self._finalization_result

    @property
    def workspace_changes(self) -> object | None:
        return getattr(self._finalization_result, "workspace_changes", None)

    @property
    def cancellation_pending(self) -> bool:
        return self._cleanup_cancellation_pending

    async def enter_finalizing(self) -> None:
        if self.enabled:
            await self.set_finalizing(self.run_id, True)

    async def restore(self) -> None:
        authority = self.authority
        if authority is None:
            return
        restore = getattr(authority, "restore", None)
        if not callable(restore):
            raise RuntimeError("Private file authority is unavailable")
        await restore()
        self._restored = True

    async def mark_failed(self) -> None:
        """Durably fail private finalization before publishing a terminal Run."""

        authority = self.authority
        if authority is None or self._finalized or self._failed:
            return
        outcome = await await_despite_cancellation(authority.mark_failed())
        try:
            outcome.result()
        except Exception:
            logger.warning(
                "Private file finalization failure marker failed for run %s",
                self.run_id,
                exc_info=True,
            )
        finally:
            self._failed = True
            self._cleanup_cancellation_pending |= outcome.cancellation_pending

    async def finalize(self) -> None:
        authority = self.authority
        if authority is None or not self._restored or self._finalized:
            return
        await self.set_finalizing(self.run_id, True)
        outcome = await await_despite_cancellation(authority.finalize())
        try:
            self._finalization_result = outcome.result()
        except BaseException:
            await self.mark_failed()
            raise
        self._finalized = True
        if outcome.cancellation_pending:
            raise asyncio.CancelledError

    def output_delivery_satisfied(self) -> bool:
        """Require one trusted current-Run artifact when this turn made outputs."""

        raw_produced_outputs = getattr(
            self._finalization_result,
            "produced_output_paths",
            None,
        )
        if raw_produced_outputs is not None:
            if not isinstance(raw_produced_outputs, tuple):
                return False
            produced_outputs: set[str] = set()
            for logical_path in raw_produced_outputs:
                if type(logical_path) is not str or "\\" in logical_path:
                    return False
                path = PurePosixPath(logical_path)
                if path.is_absolute() or path.as_posix() != logical_path or ".." in path.parts or len(path.parts) < 2 or path.parts[0] != "outputs":
                    return False
                produced_outputs.add(logical_path)
        else:
            produced_outputs = set()
        changes = self.workspace_changes
        if raw_produced_outputs is None and isinstance(
            changes,
            WorkspaceChangeResult,
        ):
            for change in changes.files:
                if change.root != "outputs" or change.status not in {
                    "created",
                    "modified",
                }:
                    continue
                prefix = "/mnt/user-data/"
                if not change.path.startswith(prefix):
                    return False
                logical_path = change.path.removeprefix(prefix)
                path = PurePosixPath(logical_path)
                if path.as_posix() != logical_path or ".." in path.parts or len(path.parts) < 2 or path.parts[0] != "outputs":
                    return False
                produced_outputs.add(logical_path)
        elif raw_produced_outputs is None and isinstance(changes, Mapping):
            produced_outputs = {path for state in ("created", "modified") for path in changes.get(state, ()) if isinstance(path, str) and path.startswith("outputs/")}
        elif raw_produced_outputs is None:
            return True
        if not produced_outputs:
            return True

        presented_outputs: set[str] = set()
        artifacts = getattr(self._finalization_result, "artifacts", ())
        if isinstance(artifacts, (list, tuple)):
            for artifact in artifacts:
                metadata = getattr(artifact, "metadata", None)
                if not isinstance(metadata, Mapping):
                    continue
                logical_path = metadata.get("logical_path")
                if isinstance(logical_path, str):
                    presented_outputs.add(logical_path)
        return not produced_outputs.isdisjoint(presented_outputs)

    async def output_delivery_status(self) -> str:
        """Read the server-owned continuation obligation after finalization."""

        authority = self.authority
        if authority is None:
            return "not_required"
        reader = getattr(authority, "output_delivery_status", None)
        if not callable(reader):
            return "not_required"
        status = await reader()
        if not isinstance(status, str):
            raise RuntimeError("Private output delivery authority is unavailable")
        return status

    async def join_cleanup(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        failure_message: str,
    ) -> bool:
        """Join and retry one private cleanup despite repeated cancellation."""

        cancellation_pending = False
        try:
            for attempt in range(1, self.cleanup_max_attempts + 1):
                outcome = await await_despite_cancellation(operation())
                cancellation_pending |= outcome.cancellation_pending
                if outcome.task.cancelled():
                    logger.warning(
                        "%s (attempt %d/%d)",
                        failure_message,
                        attempt,
                        self.cleanup_max_attempts,
                    )
                    continue
                try:
                    outcome.result()
                    return True
                except Exception:
                    logger.warning(
                        "%s (attempt %d/%d)",
                        failure_message,
                        attempt,
                        self.cleanup_max_attempts,
                    )
            return False
        finally:
            self._cleanup_cancellation_pending |= cancellation_pending

    async def release(self) -> RunMountReleaseOutcome | None:
        """Join release and preserve the provider's typed mount evidence."""

        if self._release_completed:
            return self._release_outcome
        authority = self.authority
        if authority is None:
            self._release_completed = True
            return None
        release = getattr(authority, "release", None)
        if not callable(release):
            raise RuntimeError("Private file authority release is unavailable")

        cancellation_pending = False
        try:
            for attempt in range(1, self.cleanup_max_attempts + 1):
                deferred = await await_despite_cancellation(release())
                cancellation_pending |= deferred.cancellation_pending
                if deferred.task.cancelled():
                    logger.warning(
                        "Private file authority cleanup failed for run %s (attempt %d/%d)",
                        self.run_id,
                        attempt,
                        self.cleanup_max_attempts,
                    )
                    continue
                try:
                    outcome = deferred.result()
                except Exception:
                    logger.warning(
                        "Private file authority cleanup failed for run %s (attempt %d/%d)",
                        self.run_id,
                        attempt,
                        self.cleanup_max_attempts,
                        exc_info=True,
                    )
                    continue
                if outcome is not None and type(outcome) not in {
                    NotAcquired,
                    Released,
                    Orphaned,
                }:
                    logger.warning(
                        "Private file authority returned invalid release evidence for run %s (attempt %d/%d)",
                        self.run_id,
                        attempt,
                        self.cleanup_max_attempts,
                    )
                    continue
                self._release_outcome = outcome
                self._release_completed = True
                return outcome
            raise RuntimeError(
                f"Private file authority cleanup failed for run {self.run_id}",
            )
        finally:
            self._cleanup_cancellation_pending |= cancellation_pending
