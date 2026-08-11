"""Provider protocol and non-cancellable cleanup barrier for Workflow Code."""

from __future__ import annotations

import asyncio
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable

from deerflow.workflows.code_execution.contracts import (
    CodeCleanupReceipt,
    CodeExecutionCompletion,
    CodeExecutionInterruption,
    CodeProvisioningHandle,
    IsolatedCodeExecutionLease,
    IsolatedCodeExecutionRequest,
    IsolatedCodeExecutionResult,
    IsolatedCodeProfileAttestation,
)


class IsolatedCodeCleanupPending(RuntimeError):
    """Raised when a durable labeled resource still needs reconciliation."""

    def __init__(self, lease: IsolatedCodeExecutionLease, receipt: CodeCleanupReceipt):
        super().__init__("isolated code cleanup is pending")
        self.lease = lease
        self.receipt = receipt


class CodeExecutionControl:
    """Thread-safe cooperative cancellation and lease-fence probe."""

    def __init__(self, *, lease_is_current: Callable[[], bool] | None = None) -> None:
        self._cancelled = threading.Event()
        self._lease_is_current = lease_is_current

    def cancel(self) -> None:
        self._cancelled.set()

    def interruption(self) -> CodeExecutionInterruption | None:
        if self._cancelled.is_set():
            return "cancelled"
        if self._lease_is_current is not None:
            lease_is_current = self._lease_is_current()
            if type(lease_is_current) is not bool:
                raise TypeError("lease_is_current must return bool")
            if not lease_is_current:
                return "lease_lost"
        return None


def _cleanup_reason(result: IsolatedCodeExecutionResult) -> str:
    if result.outcome == "succeeded":
        return "completed"
    if result.outcome == "timeout":
        return "timeout"
    if result.outcome == "cancelled":
        return result.interruption or "cancelled"
    return "failed"


async def _join_blocking(
    task: asyncio.Task,
    *,
    on_cancellation: Callable[[], None] | None = None,
):
    """Join a to-thread call even if its awaiting task is cancelled."""

    cancellation_pending = False
    while True:
        try:
            result = await asyncio.shield(task)
            return result, cancellation_pending
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancellation_pending = True
            if on_cancellation is not None:
                on_cancellation()


class IsolatedCodeExecutionProvider(ABC):
    """Strong isolated-compute port; unrelated to the Agent SandboxProvider."""

    @abstractmethod
    def attest(self) -> IsolatedCodeProfileAttestation:
        """Return side-effect-free, secret-free profile proof material."""

    @abstractmethod
    def acquire(self, request: IsolatedCodeExecutionRequest) -> IsolatedCodeExecutionLease:
        """Allocate exactly one fresh resource for this activation attempt."""

    def acquire_reserved(
        self,
        request: IsolatedCodeExecutionRequest,
        handle: CodeProvisioningHandle,
    ) -> IsolatedCodeExecutionLease:
        """Allocate from a handle durably journaled before this call.

        Providers used by the Workflow Worker must override this method.  The
        default is fail-closed so an unjournaled compatibility ``acquire`` can
        never accidentally enter the durable execution path.
        """

        raise NotImplementedError("provider does not support durable provisioning handles")

    def reconcile_provisioning(
        self,
        *,
        lease_id: str,
        reconciliation_key_hash: str,
    ) -> CodeCleanupReceipt:
        """Destroy/confirm absence for one exact pre-running journal row."""

        raise NotImplementedError("provider does not support exact provisioning reconciliation")

    def release_provisioning_handle(
        self,
        *,
        lease_id: str,
        reconciliation_key_hash: str,
    ) -> None:
        """Remove provider-local operation-fence state after DB destruction."""

        raise NotImplementedError("provider does not support provisioning handle release")

    @abstractmethod
    def execute(
        self,
        lease: IsolatedCodeExecutionLease,
        request: IsolatedCodeExecutionRequest,
        control: CodeExecutionControl,
    ) -> IsolatedCodeExecutionResult:
        """Run the fixed runner contract and return an uncommitted candidate."""

    @abstractmethod
    def cleanup(self, lease: IsolatedCodeExecutionLease, *, reason: str) -> CodeCleanupReceipt:
        """Kill all descendants, destroy the resource, and confirm absence."""

    @abstractmethod
    def reconcile_orphans(self) -> tuple[CodeCleanupReceipt, ...]:
        """Destroy durable labeled resources abandoned by a dead Worker."""

    def run(
        self,
        request: IsolatedCodeExecutionRequest,
        *,
        control: CodeExecutionControl | None = None,
    ) -> CodeExecutionCompletion:
        """Conformance-only compatibility runner with a cleanup barrier.

        Application Workers must use the durable provisioning coordinator and
        ``acquire_reserved``.  This helper intentionally has no journal port.
        """

        effective_control = control or CodeExecutionControl()
        if request.profile_digest != self.attest().profile_digest:
            raise ValueError("isolated Code request profile does not match this provider")
        lease = self.acquire(request)
        result: IsolatedCodeExecutionResult | None = None
        execution_error: BaseException | None = None
        execution_traceback = None
        try:
            result = self.execute(lease, request, effective_control)
        except BaseException as exc:
            execution_error = exc
            execution_traceback = exc.__traceback__
        reason = _cleanup_reason(result) if result is not None else "failed"
        receipt = self.cleanup(lease, reason=reason)
        if receipt.state != "destroyed_confirmed":
            pending = IsolatedCodeCleanupPending(lease, receipt)
            if execution_error is not None:
                raise pending from execution_error
            raise pending
        if execution_error is not None:
            raise execution_error.with_traceback(execution_traceback)
        if result is None:  # pragma: no cover - execute exception is re-raised
            raise RuntimeError("isolated code execution returned no result")
        # The candidate is still uncommitted.  Revalidate the execution fence
        # after destroy confirmation so a lease/cancel change during cleanup
        # cannot escape as a successful result for an obsolete attempt.
        final_interruption = effective_control.interruption()
        if final_interruption is not None:
            result = IsolatedCodeExecutionResult(
                outcome="cancelled",
                exit_code=result.exit_code,
                result=None,
                stdout_tail="",
                stderr_tail="",
                truncated=result.truncated,
                duration_ms=result.duration_ms,
                interruption=final_interruption,
            )
            receipt = CodeCleanupReceipt(
                lease_id=receipt.lease_id,
                state="destroyed_confirmed",
                reason=final_interruption,
            )
        return CodeExecutionCompletion(lease=lease, result=result, cleanup=receipt)

    async def run_async(
        self,
        request: IsolatedCodeExecutionRequest,
        *,
        lease_is_current: Callable[[], bool] | None = None,
    ) -> CodeExecutionCompletion:
        """Conformance-only async wrapper; cancellation still joins cleanup."""

        control = CodeExecutionControl(lease_is_current=lease_is_current)
        task = asyncio.create_task(asyncio.to_thread(self.run, request, control=control))
        completion, cancellation_pending = await _join_blocking(
            task,
            on_cancellation=control.cancel,
        )
        if cancellation_pending:
            raise asyncio.CancelledError
        return completion
