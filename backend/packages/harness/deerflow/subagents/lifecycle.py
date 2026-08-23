"""Process-wide lifecycle ownership for detached Sub-Agent Tasks.

The public seam is deliberately small: callers submit one immutable
``SubagentTaskCall`` plus one immutable ``SubagentExecutionBinding`` and await
one immutable ``SubagentTaskOutcome``.  Queueing, graph-task ownership,
cancellation, terminal arbitration, quiescence, and reaping stay behind this
module boundary.

``task_id`` is caller correlation only.  The registry and scheduler always use
the internally generated ``execution_id`` UUID, so two parent Runs may safely
reuse the same tool-call identifier.
"""

from __future__ import annotations

import asyncio
import atexit
import inspect
import logging
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast

from langchain_core.runnables.config import var_child_runnable_config

from deerflow.error_codes import (
    LOOP_FINALIZATION_FAILED_ERROR_CODE,
    SUBAGENT_COMMAND_EXECUTION_UNAVAILABLE_ERROR_CODE,
    SUBAGENT_EXECUTION_FAILED_ERROR_CODE,
    TOOL_CALL_CONTROL_STATE_INVALID_ERROR_CODE,
)
from deerflow.runtime.host_execution_approval import HostExecutionApprovalArtifact
from deerflow.subagents.change_signal import SubagentChangeSignal, wait_for_change
from deerflow.subagents.status_contract import SubagentStopReasonValue

logger = logging.getLogger(__name__)

MAX_CONCURRENT_SUBAGENT_EXECUTIONS = 16
_PROGRESS_HEARTBEAT_SECONDS = 1.0
_BARRIER_RETRY_SECONDS = 0.1
_OWNER_LOOP_RECEIPT_POLL_SECONDS = 0.01


class SubagentTaskStatus(StrEnum):
    """Lifecycle status published to Sub-Agent Task consumers."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self not in {type(self).PENDING, type(self).RUNNING}


class SubagentUsageCompleteness(StrEnum):
    """How much confidence consumers may place in the usage snapshot."""

    # Final means final for records observed by the graph's token collector.
    # It does not claim knowledge of provider-side billing absent callbacks.
    FINAL_OBSERVED = "final_observed"
    LATEST_OBSERVED = "latest_observed"


class SubagentQuiescencePolicy(StrEnum):
    """Whether ``run`` may return before every inherited operation is quiet."""

    REQUIRED_BEFORE_RETURN = "required_before_return"
    BOUNDED_WITH_REAPER = "bounded_with_reaper"


class SubagentTimeoutPhase(StrEnum):
    """Budget that expired for a timed-out execution."""

    QUEUE = "queue"
    EXECUTION = "execution"


class SubagentFailureCode(StrEnum):
    """Closed failure vocabulary independent from adapter presentation."""

    EXECUTION_FAILED = SUBAGENT_EXECUTION_FAILED_ERROR_CODE
    COMMAND_EXECUTION_UNAVAILABLE = SUBAGENT_COMMAND_EXECUTION_UNAVAILABLE_ERROR_CODE
    TOOL_CALL_CONTROL_STATE_INVALID = TOOL_CALL_CONTROL_STATE_INVALID_ERROR_CODE
    LOOP_FINALIZATION_FAILED = LOOP_FINALIZATION_FAILED_ERROR_CODE
    TURN_BUDGET_EXHAUSTED = "SUBAGENT_TURN_BUDGET_EXHAUSTED"
    LLM_QUOTA_EXCEEDED = "LLM_QUOTA_EXCEEDED"
    LLM_AUTHENTICATION_FAILED = "LLM_AUTHENTICATION_FAILED"
    LLM_PROVIDER_BUSY = "LLM_PROVIDER_BUSY"
    LLM_PROVIDER_UNAVAILABLE = "LLM_PROVIDER_UNAVAILABLE"
    LLM_REQUEST_FAILED = "LLM_REQUEST_FAILED"
    CURRENT_UPLOAD_UNAVAILABLE = "CURRENT_UPLOAD_UNAVAILABLE"
    LLM_CIRCUIT_OPEN = "LLM_CIRCUIT_OPEN"


class SubagentCancellationCode(StrEnum):
    """Stable reasons for lifecycle cancellation."""

    GRAPH_CANCELLED = "graph_cancelled"
    PARENT_CANCELLED = "parent_cancelled"
    LIFECYCLE_SHUTDOWN = "lifecycle_shutdown"
    PROCESS_SHUTDOWN = "process_shutdown"


@dataclass(frozen=True, slots=True)
class SubagentTaskCall:
    """One Sub-Agent Task request, separate from its inherited authority."""

    task_id: str
    prompt: str
    queue_timeout_seconds: float
    execution_timeout_seconds: float
    quiescence_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id must not be empty")
        if not self.prompt:
            raise ValueError("prompt must not be empty")
        if self.queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be greater than zero")
        if self.execution_timeout_seconds <= 0:
            raise ValueError("execution_timeout_seconds must be greater than zero")
        if self.quiescence_timeout_seconds < 0:
            raise ValueError("quiescence_timeout_seconds must not be negative")


@dataclass(frozen=True, slots=True)
class _SubagentGraphExecutionSnapshot:
    """Thread-safe snapshot returned by the graph runner's mutable holder.

    This type is an internal adapter seam shared with ``executor.py``.  The
    lifecycle never reaches into the holder's locks or mutable collections.
    """

    trace_id: str | None
    status: str
    status_is_terminal: bool
    result: str | None
    error: str | None
    stop_reason: str | None
    ai_messages: tuple[Mapping[str, Any], ...]
    token_usage_records: tuple[Mapping[str, int | str | None], ...]
    host_execution_approval_artifact: Mapping[str, object] | None


class _SubagentGraphResultHolder(Protocol):
    """Mutable graph-side state hidden behind the lifecycle adapter."""

    cancel_event: threading.Event
    changes: SubagentChangeSignal

    def mark_running(self, *, started_at: datetime | None = None) -> None: ...

    def _snapshot_for_lifecycle(self) -> _SubagentGraphExecutionSnapshot: ...


class _SubagentGraphRunner(Protocol):
    """Narrow internal seam: graph execution, not task lifecycle."""

    trace_id: str

    def _create_lifecycle_result_holder(
        self,
        *,
        execution_id: uuid.UUID,
        changes: SubagentChangeSignal,
    ) -> _SubagentGraphResultHolder: ...

    async def _run_lifecycle_graph(
        self,
        prompt: str,
        result_holder: _SubagentGraphResultHolder,
    ) -> _SubagentGraphResultHolder: ...


type SubagentRunnerFactoryResult = _SubagentGraphRunner | Awaitable[_SubagentGraphRunner]
type SubagentRunnerFactory = Callable[[], SubagentRunnerFactoryResult]


class SubagentInheritedOperationsBarrier(Protocol):
    """Receipt barrier for owner-loop operations inherited from the parent Run.

    ``seal`` prevents new receipts. ``wait_quiescent`` returns only after every
    receipt has been acknowledged by the target operation's real ``finally``.
    A cancelled ``concurrent.futures.Future`` is not a valid acknowledgement.
    """

    def seal(self) -> None: ...

    async def wait_quiescent(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _NoInheritedOperationsBarrier:
    """Explicit barrier for bindings that inherit no asynchronous operations."""

    def seal(self) -> None:
        return None

    async def wait_quiescent(self) -> None:
        return None


NO_INHERITED_OPERATIONS: SubagentInheritedOperationsBarrier = _NoInheritedOperationsBarrier()


@dataclass(frozen=True, slots=True)
class SubagentUsageSettlement:
    """Internal detailed-usage transfer with a single-winner invocation.

    Detailed model-call records are intentionally absent from public progress
    and outcomes.  Only the parent Run's owner-loop settlement adapter receives
    one lifecycle attempt after graph and inherited-operation quiescence. The
    receipt correlates that attempt; durable RunJournal deduplication remains
    based on its detailed record source identities.
    """

    receipt_id: uuid.UUID
    task_id: str
    records: tuple[Mapping[str, int | str | None], ...]


type SubagentUsageSettlementHook = Callable[[SubagentUsageSettlement], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SubagentExecutionBinding:
    """Opaque, lazy execution binding consumed only after scheduler admission.

    The factory must close over the exact immutable parent Run profile,
    authority, runtime snapshot, and resolved subagent inputs.  It is invoked
    only after the process-wide execution gate is acquired; ``task_tool`` must
    not construct a graph runner before calling this module.

    Private Run bindings use ``REQUIRED_BEFORE_RETURN`` and must provide their
    receipt barrier.  Bindings with no inherited asynchronous operations must
    say so explicitly with ``NO_INHERITED_OPERATIONS``. A bounded binding with
    a real barrier must also provide ``owner_loop_quiescent``: returning at its
    coordination cutoff is safe only after new receipts are sealed and active
    owner-loop targets have unwound.
    """

    runner_factory: SubagentRunnerFactory
    quiescence_policy: SubagentQuiescencePolicy
    inherited_operations_barrier: SubagentInheritedOperationsBarrier | None
    owner_loop_quiescent: Callable[[], bool] | None = None
    settle_usage: SubagentUsageSettlementHook | None = None

    def __post_init__(self) -> None:
        if not callable(self.runner_factory):
            raise TypeError("runner_factory must be callable")
        if self.quiescence_policy is SubagentQuiescencePolicy.REQUIRED_BEFORE_RETURN and self.inherited_operations_barrier is None:
            raise ValueError(
                "REQUIRED_BEFORE_RETURN needs an explicit inherited-operations barrier",
            )
        if self.owner_loop_quiescent is not None and not callable(self.owner_loop_quiescent):
            raise TypeError("owner_loop_quiescent must be callable")
        if self.quiescence_policy is SubagentQuiescencePolicy.BOUNDED_WITH_REAPER and self.inherited_operations_barrier is not None and self.inherited_operations_barrier is not NO_INHERITED_OPERATIONS and self.owner_loop_quiescent is None:
            raise ValueError(
                "BOUNDED_WITH_REAPER needs an owner-loop quiescence probe for a real inherited-operations barrier",
            )


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class SubagentTokenUsage:
    """Frozen cumulative usage safe for progress and terminal consumers."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int = 0


@dataclass(frozen=True, slots=True)
class SubagentTaskSnapshot:
    """Immutable non-terminal progress view delivered to observers."""

    execution_id: uuid.UUID
    task_id: str
    status: SubagentTaskStatus
    trace_id: str | None
    queued_at: datetime
    started_at: datetime | None
    ai_messages: tuple[Mapping[str, Any], ...]
    usage: SubagentTokenUsage | None
    usage_completeness: SubagentUsageCompleteness

    @property
    def usage_is_final(self) -> bool:
        return self.usage_completeness is SubagentUsageCompleteness.FINAL_OBSERVED


@dataclass(frozen=True, slots=True)
class _SubagentOutcomeBase:
    execution_id: uuid.UUID
    task_id: str
    trace_id: str | None
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime
    ai_messages: tuple[Mapping[str, Any], ...]
    usage: SubagentTokenUsage | None
    usage_completeness: SubagentUsageCompleteness
    quiescent: bool

    @property
    def usage_is_final(self) -> bool:
        return self.usage_completeness is SubagentUsageCompleteness.FINAL_OBSERVED


@dataclass(frozen=True, slots=True)
class SubagentCompleted(_SubagentOutcomeBase):
    result: str
    stop_reason: SubagentStopReasonValue | None
    status: Literal[SubagentTaskStatus.COMPLETED] = field(
        default=SubagentTaskStatus.COMPLETED,
        init=False,
    )


@dataclass(frozen=True, slots=True)
class SubagentFailed(_SubagentOutcomeBase):
    failure_code: SubagentFailureCode
    detail: str | None
    stop_reason: SubagentStopReasonValue | None
    status: Literal[SubagentTaskStatus.FAILED] = field(
        default=SubagentTaskStatus.FAILED,
        init=False,
    )

    def __post_init__(self) -> None:
        if type(self.failure_code) is not SubagentFailureCode:
            raise TypeError("failure_code must be SubagentFailureCode")
        if self.detail is not None and (not isinstance(self.detail, str) or not self.detail.strip()):
            raise ValueError("detail must be a non-blank string when provided")


@dataclass(frozen=True, slots=True)
class SubagentCancelled(_SubagentOutcomeBase):
    cancellation_code: SubagentCancellationCode
    status: Literal[SubagentTaskStatus.CANCELLED] = field(
        default=SubagentTaskStatus.CANCELLED,
        init=False,
    )

    def __post_init__(self) -> None:
        if type(self.cancellation_code) is not SubagentCancellationCode:
            raise TypeError("cancellation_code must be SubagentCancellationCode")


@dataclass(frozen=True, slots=True)
class SubagentTimedOut(_SubagentOutcomeBase):
    timeout_phase: SubagentTimeoutPhase
    status: Literal[SubagentTaskStatus.TIMED_OUT] = field(
        default=SubagentTaskStatus.TIMED_OUT,
        init=False,
    )

    def __post_init__(self) -> None:
        if type(self.timeout_phase) is not SubagentTimeoutPhase:
            raise TypeError("timeout_phase must be SubagentTimeoutPhase")


@dataclass(frozen=True, slots=True)
class SubagentApprovalRequired(_SubagentOutcomeBase):
    artifact: HostExecutionApprovalArtifact
    status: Literal["approval_required"] = field(
        default="approval_required",
        init=False,
    )

    def __post_init__(self) -> None:
        if type(self.artifact) is not HostExecutionApprovalArtifact:
            raise TypeError(
                "approval_required must carry HostExecutionApprovalArtifact",
            )


type SubagentTaskOutcome = SubagentCompleted | SubagentFailed | SubagentCancelled | SubagentTimedOut | SubagentApprovalRequired

type SubagentTaskEvent = SubagentTaskSnapshot | SubagentTaskOutcome


type SubagentTaskObserver = Callable[[SubagentTaskEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _TerminationRequest:
    status: SubagentTaskStatus
    failure_code: SubagentFailureCode | None
    cancellation_code: SubagentCancellationCode | None
    timeout_phase: SubagentTimeoutPhase | None
    requested_at_monotonic: float


class _ThreadOperationReceipts:
    """Join receipts for ``asyncio.to_thread`` work started by one graph.

    Cancelling the asyncio wrapper does not stop an already-running worker
    thread. The lifecycle therefore waits for the underlying concurrent Future
    before it calls the graph quiescent or releases parent resources.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: set[Future[Any]] = set()
        self._sealed = False
        self._quiescent = threading.Event()

    def track(self, future: Future[Any]) -> None:
        with self._lock:
            self._pending.add(future)
            self._quiescent.clear()
        future.add_done_callback(self._acknowledge)

    def _acknowledge(self, future: Future[Any]) -> None:
        with self._lock:
            self._pending.discard(future)
            if self._sealed and not self._pending:
                self._quiescent.set()

    def seal(self) -> None:
        with self._lock:
            self._sealed = True
            if not self._pending:
                self._quiescent.set()

    async def wait_quiescent(self) -> None:
        while not self._quiescent.is_set():
            await asyncio.sleep(0.002)


_CURRENT_THREAD_OPERATION_RECEIPTS: ContextVar[_ThreadOperationReceipts | None] = ContextVar(
    "subagent_thread_operation_receipts",
    default=None,
)


class _LifecycleThreadPoolExecutor(ThreadPoolExecutor):
    """Dedicated default executor that records real worker completion."""

    def submit[ResultT](
        self,
        fn: Callable[..., ResultT],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[ResultT]:
        receipt_owner = _CURRENT_THREAD_OPERATION_RECEIPTS.get()
        future = super().submit(fn, *args, **kwargs)
        if receipt_owner is not None:
            receipt_owner.track(future)
        return future


@dataclass(slots=True)
class _ExecutionRecord:
    execution_id: uuid.UUID
    call: SubagentTaskCall
    binding: SubagentExecutionBinding
    owner_loop: asyncio.AbstractEventLoop
    queued_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    queued_at_monotonic: float = field(default_factory=time.monotonic)
    changes: SubagentChangeSignal = field(
        default_factory=lambda: SubagentChangeSignal(debounce_seconds=1.0),
    )
    thread_operations: _ThreadOperationReceipts = field(
        default_factory=lambda: _ThreadOperationReceipts(),
    )
    graph_quiesced: threading.Event = field(default_factory=threading.Event)
    overall_quiesced: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    source_task: asyncio.Task[None] | None = None
    result_holder: _SubagentGraphResultHolder | None = None
    trace_id: str | None = None
    started_at: datetime | None = None
    started_at_monotonic: float | None = None
    graph_quiesced_at_monotonic: float | None = None
    completed_at: datetime | None = None
    termination: _TerminationRequest | None = None
    outcome: SubagentTaskOutcome | None = None
    usage_settlement_started: bool = False
    finalizer_started: bool = False
    admission_lease_active: bool = False
    delivery_fault_count: int = 0
    returned: bool = False

    def request_termination(
        self,
        status: SubagentTaskStatus,
        *,
        failure_code: SubagentFailureCode | None = None,
        cancellation_code: SubagentCancellationCode | None = None,
        timeout_phase: SubagentTimeoutPhase | None = None,
    ) -> bool:
        """Record the first lifecycle terminal decision."""

        if status is SubagentTaskStatus.FAILED:
            if type(failure_code) is not SubagentFailureCode:
                raise ValueError("failed termination requires failure_code")
            if cancellation_code is not None or timeout_phase is not None:
                raise ValueError("failed termination cannot carry cancellation or timeout")
        elif status is SubagentTaskStatus.CANCELLED:
            if type(cancellation_code) is not SubagentCancellationCode:
                raise ValueError("cancelled termination requires cancellation_code")
            if failure_code is not None or timeout_phase is not None:
                raise ValueError("cancelled termination cannot carry failure or timeout")
        elif status is SubagentTaskStatus.TIMED_OUT:
            if type(timeout_phase) is not SubagentTimeoutPhase:
                raise ValueError("timed-out termination requires timeout_phase")
            if failure_code is not None or cancellation_code is not None:
                raise ValueError("timed-out termination cannot carry failure or cancellation")
        else:
            raise ValueError("termination status must be failed, cancelled, or timed_out")
        with self.lock:
            if self.termination is not None:
                return False
            self.termination = _TerminationRequest(
                status=status,
                failure_code=failure_code,
                cancellation_code=cancellation_code,
                timeout_phase=timeout_phase,
                requested_at_monotonic=time.monotonic(),
            )
            holder = self.result_holder
        barrier = self.binding.inherited_operations_barrier
        if barrier is not None and self.binding.quiescence_policy is SubagentQuiescencePolicy.BOUNDED_WITH_REAPER:
            try:
                barrier.seal()
            except Exception as exc:
                logger.error(
                    "Failed to seal inherited operations at termination: execution_id=%s exception_type=%s",
                    self.execution_id,
                    type(exc).__name__,
                )
        if holder is not None:
            holder.cancel_event.set()
        self.changes.notify()
        return True

    def install_source_task(self, task: asyncio.Task[None]) -> bool:
        with self.lock:
            self.source_task = task
            return self.termination is not None

    def install_runner(
        self,
        runner: _SubagentGraphRunner,
        holder: _SubagentGraphResultHolder,
    ) -> bool:
        with self.lock:
            self.trace_id = getattr(runner, "trace_id", None)
            self.result_holder = holder
            started_at = self.started_at or datetime.now(UTC)
            self.started_at = started_at
            if self.started_at_monotonic is None:
                self.started_at_monotonic = time.monotonic()
            should_cancel = self.termination is not None
        holder.mark_running(started_at=started_at)
        if should_cancel:
            holder.cancel_event.set()
        self.changes.notify()
        return should_cancel

    def mark_execution_started(self) -> bool:
        """Start the execution budget immediately after scheduler admission."""

        with self.lock:
            if self.started_at_monotonic is None:
                self.started_at = datetime.now(UTC)
                self.started_at_monotonic = time.monotonic()
            should_cancel = self.termination is not None
        self.changes.notify()
        return should_cancel

    def mark_admission_acquired(self) -> None:
        with self.lock:
            if self.admission_lease_active:
                raise RuntimeError("Sub-Agent Task admission lease was acquired twice")
            self.admission_lease_active = True

    def claim_admission_release(self) -> bool:
        with self.lock:
            if not self.admission_lease_active:
                return False
            self.admission_lease_active = False
            return True

    def mark_graph_quiescent(self) -> None:
        with self.lock:
            if self.graph_quiesced_at_monotonic is None:
                self.graph_quiesced_at_monotonic = time.monotonic()
        self.graph_quiesced.set()
        # Graph unwind is a lifecycle deadline boundary, not debounced
        # progress. Wake owner-loop coordination immediately.
        self.changes.notify(terminal=True)

    def mark_overall_quiescent(self) -> None:
        with self.lock:
            self.completed_at = datetime.now(UTC)
        self.overall_quiesced.set()
        self.changes.notify(terminal=True)

    def claim_finalizer(self) -> bool:
        with self.lock:
            if self.finalizer_started:
                return False
            self.finalizer_started = True
            return True

    def mark_returned(self) -> bool:
        with self.lock:
            self.returned = True
            return self.overall_quiesced.is_set()


class _SchedulerState(StrEnum):
    ACCEPTING = "accepting"
    CLOSING = "closing"
    CLOSED = "closed"


class _ProcessSubagentScheduler:
    """One process-wide isolated loop, gate, UUID registry, and graph reaper."""

    def __init__(self, *, max_concurrency: int = MAX_CONCURRENT_SUBAGENT_EXECUTIONS) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be greater than zero")
        self._max_concurrency = max_concurrency
        self._lock = threading.Lock()
        self._state = _SchedulerState.ACCEPTING
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started: threading.Event | None = None
        self._gate: asyncio.Semaphore | None = None
        self._records: dict[uuid.UUID, _ExecutionRecord] = {}
        self._thread_executor = _LifecycleThreadPoolExecutor(
            thread_name_prefix="subagent-lifecycle-worker",
        )

    def _ensure_loop_locked(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        thread = self._thread
        if loop is not None and not loop.is_closed() and loop.is_running() and thread is not None and thread.is_alive():
            return loop

        loop = asyncio.new_event_loop()
        loop.set_default_executor(self._thread_executor)
        started = threading.Event()

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(started.set)
            loop.run_forever()

        thread = threading.Thread(
            target=run_loop,
            name="subagent-lifecycle-loop",
            daemon=True,
        )
        thread.start()
        if not started.wait(timeout=5.0):
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=1.0)
            if not loop.is_closed():
                loop.close()
            raise RuntimeError("Timed out starting Sub-Agent Task scheduler")
        self._loop = loop
        self._thread = thread
        self._started = started
        return loop

    def submit(self, record: _ExecutionRecord, context: Context) -> None:
        with self._lock:
            if self._state is not _SchedulerState.ACCEPTING:
                raise RuntimeError("Sub-Agent Task lifecycle is closing")
            loop = self._ensure_loop_locked()
            self._records[record.execution_id] = record

        def schedule() -> None:
            coroutine = self._run_record(record)
            try:
                task = loop.create_task(coroutine, context=context)
            except BaseException:
                coroutine.close()
                record.request_termination(
                    SubagentTaskStatus.FAILED,
                    failure_code=SubagentFailureCode.EXECUTION_FAILED,
                )
                self._start_finalizer(record)
                return
            cancel_before_start = record.install_source_task(task)
            task.add_done_callback(lambda completed: self._source_task_done(record, completed))
            if cancel_before_start:
                task.cancel()

        try:
            loop.call_soon_threadsafe(schedule, context=context)
        except RuntimeError:
            record.request_termination(
                SubagentTaskStatus.FAILED,
                failure_code=SubagentFailureCode.EXECUTION_FAILED,
            )
            record.mark_graph_quiescent()
            record.mark_overall_quiescent()

    async def _run_record(self, record: _ExecutionRecord) -> None:
        gate = self._gate
        if gate is None:
            gate = asyncio.Semaphore(self._max_concurrency)
            self._gate = gate

        deadline_watchdog: asyncio.Task[None] | None = None
        receipt_token = _CURRENT_THREAD_OPERATION_RECEIPTS.set(
            record.thread_operations,
        )
        try:
            queue_remaining = record.queued_at_monotonic + record.call.queue_timeout_seconds - time.monotonic()
            if queue_remaining <= 0:
                record.request_termination(
                    SubagentTaskStatus.TIMED_OUT,
                    timeout_phase=SubagentTimeoutPhase.QUEUE,
                )
                return
            try:
                await asyncio.wait_for(
                    gate.acquire(),
                    timeout=queue_remaining,
                )
            except TimeoutError:
                record.request_termination(
                    SubagentTaskStatus.TIMED_OUT,
                    timeout_phase=SubagentTimeoutPhase.QUEUE,
                )
                return
            record.mark_admission_acquired()
            if record.mark_execution_started():
                return
            source_task = asyncio.current_task()
            if source_task is None:
                raise RuntimeError("Sub-Agent Task source task is unavailable")
            deadline_watchdog = asyncio.create_task(
                self._enforce_execution_deadline(record, source_task),
                name=f"subagent-deadline-{record.execution_id}",
            )

            runner_or_awaitable = record.binding.runner_factory()
            runner = await runner_or_awaitable if inspect.isawaitable(runner_or_awaitable) else runner_or_awaitable
            holder = runner._create_lifecycle_result_holder(
                execution_id=record.execution_id,
                changes=record.changes,
            )
            if record.install_runner(runner, holder):
                raise asyncio.CancelledError
            await runner._run_lifecycle_graph(record.call.prompt, holder)
        except asyncio.CancelledError:
            with record.lock:
                has_terminal_request = record.termination is not None
            if not has_terminal_request:
                record.request_termination(
                    SubagentTaskStatus.CANCELLED,
                    cancellation_code=SubagentCancellationCode.GRAPH_CANCELLED,
                )
        except Exception as exc:
            logger.error(
                "Sub-Agent Task graph failure: execution_id=%s exception_type=%s",
                record.execution_id,
                type(exc).__name__,
            )
            record.request_termination(
                SubagentTaskStatus.FAILED,
                failure_code=SubagentFailureCode.EXECUTION_FAILED,
            )
        finally:
            if deadline_watchdog is not None:
                deadline_watchdog.cancel()
                try:
                    await deadline_watchdog
                except asyncio.CancelledError:
                    pass
            _CURRENT_THREAD_OPERATION_RECEIPTS.reset(receipt_token)

    @staticmethod
    async def _enforce_execution_deadline(
        record: _ExecutionRecord,
        source_task: asyncio.Task[None],
    ) -> None:
        with record.lock:
            started_at_monotonic = record.started_at_monotonic
        if started_at_monotonic is None:
            return
        remaining = started_at_monotonic + record.call.execution_timeout_seconds - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
        if record.request_termination(
            SubagentTaskStatus.TIMED_OUT,
            timeout_phase=SubagentTimeoutPhase.EXECUTION,
        ):
            source_task.cancel()

    def _source_task_done(
        self,
        record: _ExecutionRecord,
        task: asyncio.Task[None],
    ) -> None:
        if task.cancelled():
            record.request_termination(
                SubagentTaskStatus.CANCELLED,
                cancellation_code=SubagentCancellationCode.GRAPH_CANCELLED,
            )
        else:
            try:
                exception = task.exception()
            except asyncio.CancelledError:
                exception = None
            if exception is not None:
                logger.error(
                    "Sub-Agent Task source future failed: execution_id=%s exception_type=%s",
                    record.execution_id,
                    type(exception).__name__,
                )
                record.request_termination(
                    SubagentTaskStatus.FAILED,
                    failure_code=SubagentFailureCode.EXECUTION_FAILED,
                )
        # This callback runs only after the actual asyncio.Task has completed;
        # unlike concurrent Future.cancelled(), it is a graph-unwind receipt.
        self._start_finalizer(record)

    def _start_finalizer(self, record: _ExecutionRecord) -> None:
        """Start the process-loop reaper after the real graph Task is done."""

        if not record.claim_finalizer():
            return
        barrier = record.binding.inherited_operations_barrier
        if barrier is not None:
            try:
                barrier.seal()
            except Exception as exc:
                logger.error(
                    "Failed to seal inherited operations at graph unwind: execution_id=%s exception_type=%s",
                    record.execution_id,
                    type(exc).__name__,
                )
        record.mark_graph_quiescent()
        task = asyncio.create_task(
            _finish_record_quiescence(record, self),
            name=f"subagent-quiescence-{record.execution_id}",
        )
        task.add_done_callback(
            lambda completed: self._finalizer_done(record, completed),
        )

    @staticmethod
    def _finalizer_done(
        record: _ExecutionRecord,
        task: asyncio.Task[None],
    ) -> None:
        if task.cancelled():
            logger.error(
                "Sub-Agent Task process reaper was cancelled: execution_id=%s",
                record.execution_id,
            )
            return
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            exception = None
        if exception is not None:
            logger.error(
                "Sub-Agent Task process reaper failed: execution_id=%s exception_type=%s",
                record.execution_id,
                type(exception).__name__,
            )

    def cancel(self, record: _ExecutionRecord) -> None:
        with record.lock:
            holder = record.result_holder
            task = record.source_task
            loop = self._loop
        if holder is not None:
            holder.cancel_event.set()
        if task is not None and loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                return

    def release(self, record: _ExecutionRecord) -> None:
        with self._lock:
            if self._records.get(record.execution_id) is record:
                self._records.pop(record.execution_id, None)

    def release_admission(self, record: _ExecutionRecord) -> None:
        """Release the process gate only after all child resources are quiet."""

        if not record.claim_admission_release():
            return
        gate = self._gate
        if gate is None:
            raise RuntimeError("Sub-Agent Task scheduler gate is unavailable")
        gate.release()

    def records(self) -> tuple[_ExecutionRecord, ...]:
        with self._lock:
            return tuple(self._records.values())

    def active_count(self) -> int:
        with self._lock:
            return len(self._records)

    def begin_close(self) -> tuple[_ExecutionRecord, ...]:
        with self._lock:
            if self._state is _SchedulerState.CLOSED:
                return ()
            self._state = _SchedulerState.CLOSING
            return tuple(self._records.values())

    def close_sync_fallback(self, *, timeout_seconds: float = 5.0) -> None:
        """Best-effort process-exit fallback; normal shutdown uses ``aclose``."""

        records = self.begin_close()
        for record in records:
            record.request_termination(
                SubagentTaskStatus.CANCELLED,
                cancellation_code=SubagentCancellationCode.PROCESS_SHUTDOWN,
            )
            self.cancel(record)
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        incomplete = 0
        for record in records:
            remaining = max(0.0, deadline - time.monotonic())
            if not record.overall_quiesced.wait(timeout=remaining):
                incomplete += 1
        if incomplete:
            logger.error(
                "Process exit reached the Sub-Agent Task fallback deadline before full quiescence: incomplete=%s",
                incomplete,
            )
        self.finish_close(wait_for_thread_operations=incomplete == 0)

    def finish_close(self, *, wait_for_thread_operations: bool = True) -> None:
        with self._lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
            self._gate = None
            self._records.clear()
            self._state = _SchedulerState.CLOSED
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        if loop is not None and not loop.is_running() and not loop.is_closed():
            loop.close()
        self._thread_executor.shutdown(
            wait=wait_for_thread_operations,
            cancel_futures=True,
        )


def _copy_detached_context() -> Context:
    """Keep authority/tracing context but detach the parent Agent Graph writer."""

    context = copy_context()
    context.run(var_child_runnable_config.set, None)
    return context


def _empty_graph_snapshot(trace_id: str | None = None) -> _SubagentGraphExecutionSnapshot:
    return _SubagentGraphExecutionSnapshot(
        trace_id=trace_id,
        status=SubagentTaskStatus.PENDING,
        status_is_terminal=False,
        result=None,
        error=None,
        stop_reason=None,
        ai_messages=(),
        token_usage_records=(),
        host_execution_approval_artifact=None,
    )


def _graph_snapshot(record: _ExecutionRecord) -> _SubagentGraphExecutionSnapshot:
    with record.lock:
        holder = record.result_holder
        trace_id = record.trace_id
    if holder is None:
        return _empty_graph_snapshot(trace_id)
    return holder._snapshot_for_lifecycle()


def _normalize_graph_terminal_status(status: str) -> SubagentTaskStatus:
    try:
        normalized = SubagentTaskStatus(status)
    except ValueError:
        return SubagentTaskStatus.FAILED
    return normalized if normalized.is_terminal else SubagentTaskStatus.FAILED


def _aggregate_usage(
    records: Sequence[Mapping[str, int | str | None]],
) -> SubagentTokenUsage | None:
    if not records:
        return None

    def total(key: str) -> int:
        amount = 0
        for record in records:
            value = record.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                amount += value
        return amount

    return SubagentTokenUsage(
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        total_tokens=total("total_tokens"),
        cache_read_tokens=total("cache_read_tokens"),
    )


def _normalize_stop_reason(value: str | None) -> SubagentStopReasonValue | None:
    if value not in {
        "token_capped",
        "turn_capped",
        "loop_capped",
        "tool_budget_capped",
    }:
        return None
    return cast(SubagentStopReasonValue, value)


def _normalize_graph_failure(
    error: str | None,
    stop_reason: SubagentStopReasonValue | None,
) -> tuple[SubagentFailureCode, str | None]:
    if isinstance(error, str):
        try:
            return SubagentFailureCode(error), None
        except ValueError:
            if stop_reason == "turn_capped" and error.startswith("Reached max_turns="):
                return SubagentFailureCode.TURN_BUDGET_EXHAUSTED, error
    return SubagentFailureCode.EXECUTION_FAILED, None


def _approval_artifact(
    payload: Mapping[str, object] | None,
) -> HostExecutionApprovalArtifact | None:
    if payload is None:
        return None
    try:
        if payload.get("schema_version") != 1 or payload.get("kind") != "local_shell":
            return None
        return HostExecutionApprovalArtifact(
            approval_id=cast(str, payload.get("approval_id")),
            source_run_id=cast(str, payload.get("source_run_id")),
            source_tool_call_id=cast(str, payload.get("source_tool_call_id")),
        )
    except (TypeError, ValueError):
        return None


def _build_progress_snapshot(record: _ExecutionRecord) -> SubagentTaskSnapshot:
    graph = _graph_snapshot(record)
    with record.lock:
        started_at = record.started_at
    status = SubagentTaskStatus.RUNNING if started_at is not None else SubagentTaskStatus.PENDING
    frozen_messages = tuple(_freeze_value(message) for message in graph.ai_messages)
    return SubagentTaskSnapshot(
        execution_id=record.execution_id,
        task_id=record.call.task_id,
        status=status,
        trace_id=graph.trace_id or record.trace_id,
        queued_at=record.queued_at,
        started_at=started_at,
        ai_messages=frozen_messages,
        usage=_aggregate_usage(graph.token_usage_records),
        usage_completeness=SubagentUsageCompleteness.LATEST_OBSERVED,
    )


def _build_outcome(
    record: _ExecutionRecord,
    *,
    quiescent: bool,
) -> SubagentTaskOutcome:
    graph = _graph_snapshot(record)
    with record.lock:
        termination = record.termination
        started_at = record.started_at
        completed_at = record.completed_at or datetime.now(UTC)

    status = termination.status if termination is not None else (_normalize_graph_terminal_status(graph.status) if graph.status_is_terminal else SubagentTaskStatus.FAILED)
    stop_reason = _normalize_stop_reason(graph.stop_reason)

    common: dict[str, Any] = {
        "execution_id": record.execution_id,
        "task_id": record.call.task_id,
        "trace_id": graph.trace_id or record.trace_id,
        "queued_at": record.queued_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "ai_messages": tuple(_freeze_value(message) for message in graph.ai_messages),
        "usage": _aggregate_usage(graph.token_usage_records),
        "usage_completeness": (SubagentUsageCompleteness.FINAL_OBSERVED if quiescent else SubagentUsageCompleteness.LATEST_OBSERVED),
        "quiescent": quiescent,
    }
    if status is SubagentTaskStatus.COMPLETED:
        approval = _approval_artifact(graph.host_execution_approval_artifact)
        if approval is not None:
            return SubagentApprovalRequired(**common, artifact=approval)
        return SubagentCompleted(
            **common,
            result=graph.result or "No response generated",
            stop_reason=stop_reason,
        )
    if status is SubagentTaskStatus.CANCELLED:
        cancellation_code = termination.cancellation_code if termination is not None and termination.cancellation_code is not None else SubagentCancellationCode.GRAPH_CANCELLED
        return SubagentCancelled(
            **common,
            cancellation_code=cancellation_code,
        )
    if status is SubagentTaskStatus.TIMED_OUT:
        phase = termination.timeout_phase if termination is not None and termination.timeout_phase is not None else SubagentTimeoutPhase.EXECUTION
        return SubagentTimedOut(**common, timeout_phase=phase)
    if termination is not None and termination.failure_code is not None:
        failure_code, detail = termination.failure_code, None
    else:
        failure_code, detail = _normalize_graph_failure(
            graph.error,
            stop_reason,
        )
    return SubagentFailed(
        **common,
        failure_code=failure_code,
        detail=detail,
        stop_reason=stop_reason,
    )


async def _invoke_while_owner_loop_is_running(
    owner_loop: asyncio.AbstractEventLoop,
    operation: Callable[[], Awaitable[None]],
) -> bool:
    """Run one receipt on its owner loop without waiting on a stopped loop."""

    if owner_loop.is_closed() or not owner_loop.is_running():
        return False

    completion: Future[None] = Future()
    state_lock = threading.Lock()
    owner_task: asyncio.Task[None] | None = None
    abandoned = False

    def complete(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            if not completion.done():
                completion.cancel()
            return
        exception = task.exception()
        if completion.done():
            return
        if exception is None:
            completion.set_result(None)
        else:
            completion.set_exception(exception)

    def start() -> None:
        nonlocal owner_task
        with state_lock:
            if abandoned:
                return
            awaitable: Awaitable[None] | None = None
            try:
                awaitable = operation()
                owner_task = asyncio.ensure_future(awaitable, loop=owner_loop)
            except BaseException as exc:
                close = getattr(awaitable, "close", None)
                if callable(close):
                    close()
                if not completion.done():
                    completion.set_exception(exc)
                return
            task = owner_task
        task.add_done_callback(complete)

    try:
        owner_loop.call_soon_threadsafe(start)
    except RuntimeError:
        return False

    while not completion.done():
        if owner_loop.is_closed() or not owner_loop.is_running():
            with state_lock:
                abandoned = True
                task = owner_task
            completion.cancel()
            if task is not None and not task.done() and not owner_loop.is_closed():
                try:
                    owner_loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass
            return False
        await asyncio.sleep(_OWNER_LOOP_RECEIPT_POLL_SECONDS)

    await asyncio.wrap_future(completion)
    return True


async def _settle_usage_once_on_owner_loop(record: _ExecutionRecord) -> None:
    """Transfer detailed records once, on the parent execution's owner loop."""

    hook = record.binding.settle_usage
    if hook is None:
        return
    with record.lock:
        if record.usage_settlement_started:
            return
        record.usage_settlement_started = True
    graph = _graph_snapshot(record)
    settlement = SubagentUsageSettlement(
        receipt_id=record.execution_id,
        task_id=record.call.task_id,
        records=tuple(_freeze_value(item) for item in graph.token_usage_records),
    )

    async def invoke() -> None:
        await hook(settlement)

    try:
        if asyncio.get_running_loop() is record.owner_loop:
            await invoke()
            return
        delivered = await _invoke_while_owner_loop_is_running(
            record.owner_loop,
            invoke,
        )
        if not delivered:
            logger.error(
                "Sub-Agent Task usage settlement owner loop is unavailable: execution_id=%s",
                record.execution_id,
            )
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        logger.error(
            "Sub-Agent Task usage settlement was cancelled after receipt claim: execution_id=%s",
            record.execution_id,
        )
    except Exception as exc:
        logger.error(
            "Sub-Agent Task usage settlement failed after receipt claim: execution_id=%s exception_type=%s",
            record.execution_id,
            type(exc).__name__,
        )


async def _finish_record_quiescence(
    record: _ExecutionRecord,
    scheduler: _ProcessSubagentScheduler,
) -> None:
    """Process-loop reaper for graph, inherited operations, and settlement."""

    try:
        record.thread_operations.seal()
        await record.thread_operations.wait_quiescent()
        barrier = record.binding.inherited_operations_barrier
        if barrier is not None:
            while True:
                try:
                    barrier.seal()
                    await barrier.wait_quiescent()
                    break
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
                except Exception as exc:
                    record.request_termination(
                        SubagentTaskStatus.FAILED,
                        failure_code=SubagentFailureCode.EXECUTION_FAILED,
                    )
                    logger.error(
                        "Inherited-operation barrier failed closed; retrying: execution_id=%s exception_type=%s",
                        record.execution_id,
                        type(exc).__name__,
                    )
                    await asyncio.sleep(_BARRIER_RETRY_SECONDS)
        await _settle_usage_once_on_owner_loop(record)
    finally:
        try:
            scheduler.release_admission(record)
        finally:
            record.mark_overall_quiescent()
            with record.lock:
                returned = record.returned
            if returned:
                scheduler.release(record)


class SubagentTaskLifecycle:
    """Deep lifecycle Module for process-wide Sub-Agent Task execution."""

    def __init__(self, *, _scheduler: _ProcessSubagentScheduler | None = None) -> None:
        self._scheduler = _scheduler or _PROCESS_SCHEDULER
        self._close_lock = threading.Lock()
        self._close_complete = threading.Event()
        self._closed = False

    async def run(
        self,
        call: SubagentTaskCall,
        binding: SubagentExecutionBinding,
        *,
        observers: Iterable[SubagentTaskObserver] = (),
    ) -> SubagentTaskOutcome:
        """Run one Sub-Agent Task and return one immutable terminal outcome.

        For ``REQUIRED_BEFORE_RETURN`` this method is a hard child-resource
        barrier: caller cancellation is remembered but cannot interrupt the
        graph receipt or inherited-operation receipt.  The cancellation is
        re-raised only after both are quiet.
        """

        with self._close_lock:
            closed = self._closed
        if closed:
            raise RuntimeError("Sub-Agent Task lifecycle is closed")
        observer_tuple = tuple(observers)
        if any(not callable(observer) for observer in observer_tuple):
            raise TypeError("all observers must be callable")

        owner_loop = asyncio.get_running_loop()
        record = _ExecutionRecord(
            execution_id=uuid.uuid4(),
            call=call,
            binding=binding,
            owner_loop=owner_loop,
        )
        change_event = record.changes.subscribe()

        caller_cancellation: asyncio.CancelledError | None = None
        observers_healthy = True
        try:
            try:
                self._scheduler.submit(record, _copy_detached_context())
            except Exception as exc:
                logger.error(
                    "Sub-Agent Task submission failed: execution_id=%s exception_type=%s",
                    record.execution_id,
                    type(exc).__name__,
                )
                record.request_termination(
                    SubagentTaskStatus.FAILED,
                    failure_code=SubagentFailureCode.EXECUTION_FAILED,
                )
                record.mark_graph_quiescent()
                record.mark_overall_quiescent()
            try:
                quiescent, observers_healthy = await self._drive(
                    record,
                    change_event=change_event,
                    observers=observer_tuple,
                )
            except asyncio.CancelledError as exc:
                caller_cancellation = exc
                self._request_stop(
                    record,
                    status=SubagentTaskStatus.CANCELLED,
                    cancellation_code=SubagentCancellationCode.PARENT_CANCELLED,
                )
                quiescent, later_cancellation = await self._wait_after_caller_cancel(
                    record,
                )
                caller_cancellation = caller_cancellation or later_cancellation

            candidate = _build_outcome(record, quiescent=quiescent)
            # The semantic terminal transition is committed exactly once
            # before any fallible delivery observer sees it.
            with record.lock:
                if record.outcome is None:
                    record.outcome = candidate
                outcome = record.outcome
            if caller_cancellation is None and observers_healthy:
                try:
                    await self._notify_observers(observer_tuple, outcome)
                except asyncio.CancelledError as exc:
                    caller_cancellation = exc
                    self._request_stop(
                        record,
                        status=SubagentTaskStatus.CANCELLED,
                        cancellation_code=SubagentCancellationCode.PARENT_CANCELLED,
                    )
                    quiescent, later_cancellation = await self._wait_after_caller_cancel(
                        record,
                    )
                    caller_cancellation = caller_cancellation or later_cancellation
                except Exception as exc:
                    with record.lock:
                        record.delivery_fault_count += 1
                    logger.error(
                        "Sub-Agent Task terminal delivery fault: execution_id=%s exception_type=%s",
                        record.execution_id,
                        type(exc).__name__,
                    )
            if caller_cancellation is not None:
                raise caller_cancellation
            return outcome
        finally:
            record.changes.unsubscribe(change_event)
            if record.mark_returned():
                self._scheduler.release(record)

    async def _drive(
        self,
        record: _ExecutionRecord,
        *,
        change_event: asyncio.Event,
        observers: Sequence[SubagentTaskObserver],
    ) -> tuple[bool, bool]:
        last_snapshot: SubagentTaskSnapshot | None = None
        observers_healthy = True
        while True:
            if record.overall_quiesced.is_set():
                return True, observers_healthy
            # Clear before reading lifecycle state. A scheduler-thread change
            # racing with any read below re-sets the event, so the subsequent
            # wait cannot erase a terminal or deadline wake-up.
            change_event.clear()

            now = time.monotonic()
            with record.lock:
                started_at_monotonic = record.started_at_monotonic
                termination = record.termination
                graph_quiesced_at_monotonic = record.graph_quiesced_at_monotonic

            if termination is None and not record.graph_quiesced.is_set():
                if started_at_monotonic is None:
                    deadline = record.queued_at_monotonic + record.call.queue_timeout_seconds
                    if now >= deadline:
                        self._request_stop(
                            record,
                            status=SubagentTaskStatus.TIMED_OUT,
                            timeout_phase=SubagentTimeoutPhase.QUEUE,
                        )
                else:
                    deadline = started_at_monotonic + record.call.execution_timeout_seconds
                    if now >= deadline:
                        self._request_stop(
                            record,
                            status=SubagentTaskStatus.TIMED_OUT,
                            timeout_phase=SubagentTimeoutPhase.EXECUTION,
                        )

            if observers_healthy:
                snapshot = _build_progress_snapshot(record)
                if snapshot != last_snapshot:
                    try:
                        await self._notify_observers(observers, snapshot)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        observers_healthy = False
                        self._request_stop(
                            record,
                            status=SubagentTaskStatus.FAILED,
                            failure_code=SubagentFailureCode.EXECUTION_FAILED,
                        )
                    last_snapshot = snapshot

            with record.lock:
                termination = record.termination
                started_at_monotonic = record.started_at_monotonic
                graph_quiesced_at_monotonic = record.graph_quiesced_at_monotonic
            if termination is not None:
                if record.binding.quiescence_policy is SubagentQuiescencePolicy.BOUNDED_WITH_REAPER:
                    next_deadline = termination.requested_at_monotonic + record.call.quiescence_timeout_seconds
                    if now >= next_deadline:
                        if self._owner_loop_resources_quiescent(record):
                            return record.overall_quiesced.is_set(), observers_healthy
                        next_deadline = None
                else:
                    next_deadline = None
            elif graph_quiesced_at_monotonic is not None:
                if record.binding.quiescence_policy is SubagentQuiescencePolicy.BOUNDED_WITH_REAPER:
                    next_deadline = graph_quiesced_at_monotonic + record.call.quiescence_timeout_seconds
                    if now >= next_deadline:
                        if self._owner_loop_resources_quiescent(record):
                            return record.overall_quiesced.is_set(), observers_healthy
                        next_deadline = None
                else:
                    next_deadline = None
            elif started_at_monotonic is None:
                next_deadline = record.queued_at_monotonic + record.call.queue_timeout_seconds
            else:
                next_deadline = started_at_monotonic + record.call.execution_timeout_seconds

            heartbeat = _PROGRESS_HEARTBEAT_SECONDS
            if next_deadline is not None:
                heartbeat = min(heartbeat, max(0.0, next_deadline - time.monotonic()))
            await wait_for_change(change_event, heartbeat_seconds=heartbeat)

    @staticmethod
    def _owner_loop_resources_quiescent(record: _ExecutionRecord) -> bool:
        probe = record.binding.owner_loop_quiescent
        if probe is None:
            return True
        try:
            return probe() is True
        except Exception as exc:
            logger.error(
                "Parent owner-loop quiescence probe failed closed: execution_id=%s exception_type=%s",
                record.execution_id,
                type(exc).__name__,
            )
            return False

    def _request_stop(
        self,
        record: _ExecutionRecord,
        *,
        status: SubagentTaskStatus,
        failure_code: SubagentFailureCode | None = None,
        cancellation_code: SubagentCancellationCode | None = None,
        timeout_phase: SubagentTimeoutPhase | None = None,
    ) -> None:
        record.request_termination(
            status,
            failure_code=failure_code,
            cancellation_code=cancellation_code,
            timeout_phase=timeout_phase,
        )
        self._scheduler.cancel(record)

    async def _wait_after_caller_cancel(
        self,
        record: _ExecutionRecord,
    ) -> tuple[bool, asyncio.CancelledError | None]:
        timeout: float | None = None
        if record.binding.quiescence_policy is SubagentQuiescencePolicy.BOUNDED_WITH_REAPER:
            timeout = record.call.quiescence_timeout_seconds
        return await self._wait_ignoring_caller_cancellation(
            record.overall_quiesced,
            timeout=timeout,
            timeout_guard=((lambda: self._owner_loop_resources_quiescent(record)) if record.binding.owner_loop_quiescent is not None else None),
        )

    @staticmethod
    async def _wait_ignoring_caller_cancellation(
        event: threading.Event,
        *,
        timeout: float | None,
        timeout_guard: Callable[[], bool] | None = None,
    ) -> tuple[bool, asyncio.CancelledError | None]:
        deadline = None if timeout is None else time.monotonic() + timeout
        cancellation: asyncio.CancelledError | None = None
        while not event.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                if timeout_guard is None or timeout_guard():
                    break
                remaining = None
            delay = 0.05 if remaining is None else min(0.05, remaining)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
        return event.is_set(), cancellation

    @staticmethod
    async def _notify_observers(
        observers: Sequence[SubagentTaskObserver],
        snapshot: SubagentTaskEvent,
    ) -> None:
        for observer in observers:
            await observer(snapshot)

    async def aclose(self) -> None:
        """Cancel admission, then join every graph and inherited operation."""

        with self._close_lock:
            close_owner = not self._closed
            self._closed = True
        if not close_owner:
            _, cancellation = await self._wait_ignoring_caller_cancellation(
                self._close_complete,
                timeout=None,
            )
            if cancellation is not None:
                raise cancellation
            return

        cancellation: asyncio.CancelledError | None = None
        try:
            records = self._scheduler.begin_close()
            for record in records:
                self._request_stop(
                    record,
                    status=SubagentTaskStatus.CANCELLED,
                    cancellation_code=SubagentCancellationCode.LIFECYCLE_SHUTDOWN,
                )

            for record in records:
                _, interrupted = await self._wait_ignoring_caller_cancellation(
                    record.overall_quiesced,
                    timeout=None,
                )
                cancellation = cancellation or interrupted
                self._scheduler.release(record)
            self._scheduler.finish_close()
        finally:
            self._close_complete.set()
        if cancellation is not None:
            raise cancellation

    def _active_execution_count_for_tests(self) -> int:
        return self._scheduler.active_count()


_PROCESS_SCHEDULER = _ProcessSubagentScheduler()
subagent_task_lifecycle = SubagentTaskLifecycle(_scheduler=_PROCESS_SCHEDULER)


def close_subagent_lifecycle_sync_fallback() -> None:
    """Idempotent atexit seam; service owners must still await ``aclose``."""

    _PROCESS_SCHEDULER.close_sync_fallback()


atexit.register(close_subagent_lifecycle_sync_fallback)


__all__ = [
    "MAX_CONCURRENT_SUBAGENT_EXECUTIONS",
    "NO_INHERITED_OPERATIONS",
    "SubagentApprovalRequired",
    "SubagentCancellationCode",
    "SubagentCancelled",
    "SubagentCompleted",
    "SubagentExecutionBinding",
    "SubagentFailed",
    "SubagentFailureCode",
    "SubagentInheritedOperationsBarrier",
    "SubagentQuiescencePolicy",
    "SubagentTaskCall",
    "SubagentTaskEvent",
    "SubagentTaskLifecycle",
    "SubagentTaskObserver",
    "SubagentTaskOutcome",
    "SubagentTaskSnapshot",
    "SubagentTaskStatus",
    "SubagentTimedOut",
    "SubagentTimeoutPhase",
    "SubagentTokenUsage",
    "SubagentUsageCompleteness",
    "SubagentUsageSettlement",
    "SubagentUsageSettlementHook",
    "close_subagent_lifecycle_sync_fallback",
    "subagent_task_lifecycle",
]
