"""Focused contracts for the deep Sub-Agent Task lifecycle Module."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables.config import var_child_runnable_config

from deerflow.subagents.binding import (
    ParentExecutionBarrier,
    ParentExecutionBinding,
    invoke_parent_operation_on_owner_loop,
)
from deerflow.subagents.change_signal import SubagentChangeSignal
from deerflow.subagents.lifecycle import (
    NO_INHERITED_OPERATIONS,
    SubagentApprovalRequired,
    SubagentCancellationCode,
    SubagentCancelled,
    SubagentCompleted,
    SubagentExecutionBinding,
    SubagentFailed,
    SubagentFailureCode,
    SubagentQuiescencePolicy,
    SubagentTaskCall,
    SubagentTaskEvent,
    SubagentTaskLifecycle,
    SubagentTaskOutcome,
    SubagentTaskSnapshot,
    SubagentTaskStatus,
    SubagentTimedOut,
    SubagentTimeoutPhase,
    SubagentUsageCompleteness,
    SubagentUsageSettlement,
    _ExecutionRecord,
    _ProcessSubagentScheduler,
    _SubagentGraphExecutionSnapshot,
)

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


class _FakeHolder:
    def __init__(self, trace_id: str, changes: SubagentChangeSignal) -> None:
        self.trace_id = trace_id
        self.changes = changes
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._status = "pending"
        self._result: str | None = None
        self._error: str | None = None
        self._stop_reason: str | None = None
        self._messages: list[dict[str, Any]] = []
        self._records: list[dict[str, int | str | None]] = []
        self._approval: dict[str, object] | None = None

    def mark_running(self, *, started_at=None) -> None:
        del started_at
        with self._lock:
            self._status = "running"
        self.changes.notify()

    def publish(
        self,
        *,
        messages: list[dict[str, Any]] | None = None,
        records: list[dict[str, int | str | None]] | None = None,
    ) -> None:
        with self._lock:
            if messages is not None:
                self._messages = list(messages)
            if records is not None:
                self._records = list(records)
        self.changes.notify()

    def complete(
        self,
        result: str,
        *,
        stop_reason: str | None = None,
        approval: dict[str, object] | None = None,
    ) -> None:
        with self._lock:
            self._status = "completed"
            self._result = result
            self._stop_reason = stop_reason
            self._approval = approval
        # This intentionally publishes the graph terminal before the runner
        # returns. Lifecycle observers must still not see a terminal event
        # until the source Task and inherited-operation barrier are quiet.
        self.changes.notify(terminal=True)

    def fail(
        self,
        error: str,
        *,
        stop_reason: str | None = None,
    ) -> None:
        with self._lock:
            self._status = "failed"
            self._error = error
            self._stop_reason = stop_reason
        self.changes.notify(terminal=True)

    def _snapshot_for_lifecycle(self) -> _SubagentGraphExecutionSnapshot:
        with self._lock:
            return _SubagentGraphExecutionSnapshot(
                trace_id=self.trace_id,
                status=self._status,
                status_is_terminal=self._status in {"completed", "failed", "cancelled", "timed_out"},
                result=self._result,
                error=self._error,
                stop_reason=self._stop_reason,
                ai_messages=tuple(dict(message) for message in self._messages),
                token_usage_records=tuple(dict(record) for record in self._records),
                host_execution_approval_artifact=(dict(self._approval) if self._approval is not None else None),
            )


class _FakeRunner:
    def __init__(
        self,
        behavior: Callable[[_FakeHolder], Awaitable[None]],
        *,
        trace_id: str = "trace-subagent",
    ) -> None:
        self.trace_id = trace_id
        self._behavior = behavior

    def _create_lifecycle_result_holder(
        self,
        *,
        execution_id: uuid.UUID,
        changes: SubagentChangeSignal,
    ) -> _FakeHolder:
        assert isinstance(execution_id, uuid.UUID)
        return _FakeHolder(self.trace_id, changes)

    async def _run_lifecycle_graph(
        self,
        prompt: str,
        result_holder: _FakeHolder,
    ) -> _FakeHolder:
        assert prompt
        await self._behavior(result_holder)
        return result_holder


class _BlockingBarrier:
    def __init__(self) -> None:
        self.sealed = threading.Event()
        self.waiting = threading.Event()
        self.release = threading.Event()

    def seal(self) -> None:
        self.sealed.set()

    async def wait_quiescent(self) -> None:
        self.waiting.set()
        while not self.release.is_set():
            await asyncio.sleep(0.002)


class _CountingBlockingBarrier:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.first_waiter = threading.Event()
        self.replacement_waiter = threading.Event()
        self._lock = threading.Lock()
        self._waiters = 0

    def seal(self) -> None:
        return

    async def wait_quiescent(self) -> None:
        with self._lock:
            self._waiters += 1
            if self._waiters == 1:
                self.first_waiter.set()
            else:
                self.replacement_waiter.set()
        while not self.release.is_set():
            await asyncio.sleep(0.002)


def _call(
    task_id: str = "same-correlation-id",
    *,
    queue: float = 1.0,
    execution: float = 1.0,
    quiescence: float = 0.05,
) -> SubagentTaskCall:
    return SubagentTaskCall(
        task_id=task_id,
        prompt="Do the delegated work",
        queue_timeout_seconds=queue,
        execution_timeout_seconds=execution,
        quiescence_timeout_seconds=quiescence,
    )


def _binding(
    runner_factory,
    *,
    policy: SubagentQuiescencePolicy = SubagentQuiescencePolicy.REQUIRED_BEFORE_RETURN,
    barrier=NO_INHERITED_OPERATIONS,
    settle_usage=None,
    owner_loop_quiescent=None,
    scheduling_key: str | None = None,
) -> SubagentExecutionBinding:
    return SubagentExecutionBinding(
        runner_factory=runner_factory,
        quiescence_policy=policy,
        inherited_operations_barrier=barrier,
        settle_usage=settle_usage,
        owner_loop_quiescent=owner_loop_quiescent,
        scheduling_key=scheduling_key,
    )


def _lifecycle(*, max_concurrency: int = 2) -> SubagentTaskLifecycle:
    return SubagentTaskLifecycle(
        _scheduler=_ProcessSubagentScheduler(max_concurrency=max_concurrency),
    )


async def _wait_thread_event(event: threading.Event, *, timeout: float = 1.0) -> None:
    assert await asyncio.to_thread(event.wait, timeout)


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.005)


def _wait_until_sync(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.005)


def test_bounded_real_barrier_requires_owner_loop_quiescence_probe() -> None:
    with pytest.raises(ValueError, match="owner-loop quiescence probe"):
        SubagentExecutionBinding(
            runner_factory=lambda: object(),
            quiescence_policy=SubagentQuiescencePolicy.BOUNDED_WITH_REAPER,
            inherited_operations_barrier=_BlockingBarrier(),
        )


@pytest.mark.asyncio
async def test_completed_outcome_is_typed_frozen_and_usage_settles_once() -> None:
    lifecycle = _lifecycle()
    events_a: list[SubagentTaskEvent] = []
    events_b: list[SubagentTaskEvent] = []
    settlements: list[SubagentUsageSettlement] = []
    order: list[str] = []

    async def behavior(holder: _FakeHolder) -> None:
        holder.publish(
            messages=[{"content": {"nested": ["immutable"]}}],
            records=[
                {
                    "source_run_id": "llm-1",
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "total_tokens": 10,
                    "cache_read_tokens": 2,
                }
            ],
        )
        holder.complete("finished", stop_reason="token_capped")

    async def settle(receipt: SubagentUsageSettlement) -> None:
        order.append("settled")
        settlements.append(receipt)

    async def observe_a(event: SubagentTaskEvent) -> None:
        if not isinstance(event, SubagentTaskSnapshot):
            order.append("terminal-observed")
        events_a.append(event)

    async def observe_b(event: SubagentTaskEvent) -> None:
        events_b.append(event)

    try:
        outcome = await lifecycle.run(
            _call(),
            _binding(lambda: _FakeRunner(behavior), settle_usage=settle),
            observers=(observe_a, observe_b),
        )

        assert isinstance(outcome, SubagentCompleted)
        assert outcome.result == "finished"
        assert outcome.stop_reason == "token_capped"
        assert outcome.quiescent is True
        assert outcome.usage_is_final is True
        assert outcome.usage is not None
        assert outcome.usage.total_tokens == 10
        assert outcome.usage.cache_read_tokens == 2
        assert not hasattr(outcome, "token_usage_records")
        assert events_a[-1] is outcome
        assert events_b[-1] is outcome
        assert order[-2:] == ["settled", "terminal-observed"]
        assert len(settlements) == 1
        assert settlements[0].receipt_id == outcome.execution_id
        assert settlements[0].task_id == outcome.task_id
        with pytest.raises(TypeError):
            settlements[0].records[0]["total_tokens"] = 999  # type: ignore[index]
        with pytest.raises(FrozenInstanceError):
            outcome.result = "changed"  # type: ignore[misc]
        with pytest.raises(TypeError):
            outcome.ai_messages[0]["content"]["nested"] = ()  # type: ignore[index]
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_failed_outcome_separates_stable_code_from_graph_detail() -> None:
    lifecycle = _lifecycle()

    async def behavior(holder: _FakeHolder) -> None:
        holder.fail("Reached max_turns=7", stop_reason="turn_capped")

    try:
        outcome = await lifecycle.run(
            _call(),
            _binding(lambda: _FakeRunner(behavior)),
        )
        assert isinstance(outcome, SubagentFailed)
        assert outcome.failure_code is SubagentFailureCode.TURN_BUDGET_EXHAUSTED
        assert outcome.detail == "Reached max_turns=7"
        assert not hasattr(outcome, "error")
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_tool_control_state_invalid_is_a_stable_failed_outcome() -> None:
    lifecycle = _lifecycle()

    async def behavior(holder: _FakeHolder) -> None:
        holder.fail("TOOL_CALL_CONTROL_STATE_INVALID")

    try:
        outcome = await lifecycle.run(
            _call(),
            _binding(lambda: _FakeRunner(behavior)),
        )

        assert isinstance(outcome, SubagentFailed)
        assert outcome.status is SubagentTaskStatus.FAILED
        assert outcome.failure_code.value == "TOOL_CALL_CONTROL_STATE_INVALID"
        assert outcome.detail is None
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_loop_finalization_failure_is_a_stable_failed_outcome() -> None:
    lifecycle = _lifecycle()

    async def behavior(holder: _FakeHolder) -> None:
        holder.fail("LOOP_FINALIZATION_FAILED")

    try:
        outcome = await lifecycle.run(
            _call(),
            _binding(lambda: _FakeRunner(behavior)),
        )

        assert isinstance(outcome, SubagentFailed)
        assert outcome.status is SubagentTaskStatus.FAILED
        assert outcome.failure_code.value == "LOOP_FINALIZATION_FAILED"
        assert outcome.detail is None
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_tool_budget_cap_survives_as_a_completed_contributing_reason() -> None:
    lifecycle = _lifecycle()

    async def behavior(holder: _FakeHolder) -> None:
        holder.complete(
            "completed from admitted evidence",
            stop_reason="tool_budget_capped",
        )

    try:
        outcome = await lifecycle.run(
            _call(),
            _binding(lambda: _FakeRunner(behavior)),
        )

        assert isinstance(outcome, SubagentCompleted)
        assert outcome.result == "completed from admitted evidence"
        assert outcome.stop_reason == "tool_budget_capped"
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_provider_output_truncation_survives_lifecycle_normalization() -> None:
    lifecycle = _lifecycle()

    async def behavior(holder: _FakeHolder) -> None:
        holder.complete(
            "usable partial result",
            stop_reason="output_truncated",
        )

    try:
        outcome = await lifecycle.run(
            _call(),
            _binding(lambda: _FakeRunner(behavior)),
        )

        assert isinstance(outcome, SubagentCompleted)
        assert outcome.result == "usable partial result"
        assert outcome.stop_reason == "output_truncated"
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_provider_failure_remains_direct_when_tool_budget_was_capped() -> None:
    lifecycle = _lifecycle()

    async def behavior(holder: _FakeHolder) -> None:
        holder.fail(
            "LLM_PROVIDER_UNAVAILABLE",
            stop_reason="tool_budget_capped",
        )

    try:
        outcome = await lifecycle.run(
            _call(),
            _binding(lambda: _FakeRunner(behavior)),
        )

        assert isinstance(outcome, SubagentFailed)
        assert outcome.failure_code is SubagentFailureCode.LLM_PROVIDER_UNAVAILABLE
        assert outcome.stop_reason == "tool_budget_capped"
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_approval_is_a_discriminated_typed_outcome() -> None:
    lifecycle = _lifecycle()

    async def behavior(holder: _FakeHolder) -> None:
        holder.complete(
            "approval",
            approval={
                "schema_version": 1,
                "kind": "local_shell",
                "approval_id": "approval-1",
                "source_run_id": "run-1",
                "source_tool_call_id": "tool-1",
            },
        )

    try:
        outcome = await lifecycle.run(_call(), _binding(lambda: _FakeRunner(behavior)))
        assert isinstance(outcome, SubagentApprovalRequired)
        assert outcome.status == "approval_required"
        assert outcome.artifact.approval_id == "approval-1"
        assert outcome.artifact.source_run_id == "run-1"
        with pytest.raises(TypeError, match="HostExecutionApprovalArtifact"):
            SubagentApprovalRequired(
                execution_id=uuid.uuid4(),
                task_id="invalid-approval",
                trace_id=None,
                queued_at=outcome.queued_at,
                started_at=outcome.started_at,
                completed_at=outcome.completed_at,
                ai_messages=(),
                usage=None,
                usage_completeness=SubagentUsageCompleteness.FINAL_OBSERVED,
                quiescent=True,
                artifact={"approval_id": "not-typed"},  # type: ignore[arg-type]
            )
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_external_task_id_is_only_correlation() -> None:
    lifecycle = _lifecycle()

    async def behavior(holder: _FakeHolder) -> None:
        holder.complete("ok")

    try:
        first, second = await asyncio.gather(
            lifecycle.run(_call("duplicate"), _binding(lambda: _FakeRunner(behavior))),
            lifecycle.run(_call("duplicate"), _binding(lambda: _FakeRunner(behavior))),
        )
        assert first.task_id == second.task_id == "duplicate"
        assert first.execution_id != second.execution_id
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_async_runner_factory_is_awaited_exactly_once_after_admission() -> None:
    lifecycle = _lifecycle()
    factory_calls = 0

    async def behavior(holder: _FakeHolder) -> None:
        holder.complete("ok")

    async def factory() -> _FakeRunner:
        nonlocal factory_calls
        factory_calls += 1
        return _FakeRunner(behavior)

    try:
        outcome = await lifecycle.run(_call(), _binding(factory))
        assert isinstance(outcome, SubagentCompleted)
        assert factory_calls == 1
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_runner_materialization_counts_against_execution_budget() -> None:
    lifecycle = _lifecycle()
    materialization_started = threading.Event()
    materialization_unwound = threading.Event()

    async def factory() -> _FakeRunner:
        materialization_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            materialization_unwound.set()
        raise AssertionError("unreachable")

    try:
        outcome = await asyncio.wait_for(
            lifecycle.run(
                _call(queue=1.0, execution=0.03),
                _binding(factory),
            ),
            timeout=0.5,
        )
        assert materialization_started.is_set()
        assert materialization_unwound.is_set()
        assert isinstance(outcome, SubagentTimedOut)
        assert outcome.timeout_phase is SubagentTimeoutPhase.EXECUTION
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_scheduler_enforces_execution_deadline_while_owner_observer_is_blocked() -> None:
    lifecycle = _lifecycle()
    graph_started = threading.Event()
    graph_unwound = threading.Event()

    async def behavior(holder: _FakeHolder) -> None:
        del holder
        graph_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            graph_unwound.set()

    async def slow_observer(event: SubagentTaskEvent) -> None:
        del event
        await asyncio.sleep(0.2)

    try:
        task = asyncio.create_task(
            lifecycle.run(
                _call(execution=0.03),
                _binding(lambda: _FakeRunner(behavior)),
                observers=(slow_observer,),
            )
        )
        await _wait_thread_event(graph_started)
        await asyncio.sleep(0.08)
        assert graph_unwound.is_set()
        outcome = await asyncio.wait_for(task, timeout=1.0)
        assert isinstance(outcome, SubagentTimedOut)
        assert outcome.timeout_phase is SubagentTimeoutPhase.EXECUTION
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_private_quiescence_joins_cancelled_to_thread_work() -> None:
    lifecycle = _lifecycle()
    thread_started = threading.Event()
    release_thread = threading.Event()
    thread_done = threading.Event()

    def blocking_operation() -> None:
        thread_started.set()
        release_thread.wait()
        thread_done.set()

    async def behavior(holder: _FakeHolder) -> None:
        del holder
        await asyncio.to_thread(blocking_operation)

    task = asyncio.create_task(
        lifecycle.run(
            _call(execution=0.03),
            _binding(lambda: _FakeRunner(behavior)),
        )
    )
    try:
        await _wait_thread_event(thread_started)
        await asyncio.sleep(0.08)
        assert not task.done()
        assert not thread_done.is_set()
        release_thread.set()
        outcome = await asyncio.wait_for(task, timeout=1.0)
        assert isinstance(outcome, SubagentTimedOut)
        assert outcome.quiescent is True
        assert thread_done.is_set()
    finally:
        release_thread.set()
        if not task.done():
            await task
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_queue_budget_expires_without_materializing_queued_runner() -> None:
    lifecycle = _lifecycle(max_concurrency=1)
    first_started = threading.Event()
    release_first = threading.Event()
    second_factory_called = threading.Event()

    async def first_behavior(holder: _FakeHolder) -> None:
        first_started.set()
        while not release_first.is_set():
            await asyncio.sleep(0.002)
        holder.complete("first")

    def second_factory() -> _FakeRunner:
        second_factory_called.set()

        async def behavior(holder: _FakeHolder) -> None:
            holder.complete("second")

        return _FakeRunner(behavior)

    first_task = asyncio.create_task(
        lifecycle.run(
            _call("first", execution=2.0),
            _binding(lambda: _FakeRunner(first_behavior)),
        )
    )
    try:
        await _wait_thread_event(first_started)
        second = await lifecycle.run(
            _call("second", queue=0.03),
            _binding(second_factory),
        )
        assert isinstance(second, SubagentTimedOut)
        assert second.timeout_phase is SubagentTimeoutPhase.QUEUE
        assert second.quiescent is True
        assert not second_factory_called.is_set()
    finally:
        release_first.set()
        await first_task
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_queue_wait_does_not_consume_the_execution_budget() -> None:
    lifecycle = _lifecycle(max_concurrency=1)
    first_started = threading.Event()
    release_first = threading.Event()

    async def first_behavior(holder: _FakeHolder) -> None:
        first_started.set()
        while not release_first.is_set():
            await asyncio.sleep(0.002)
        holder.complete("first")

    async def second_behavior(holder: _FakeHolder) -> None:
        await asyncio.sleep(0.03)
        holder.complete("second")

    first_task = asyncio.create_task(
        lifecycle.run(
            _call("first", execution=1.0),
            _binding(lambda: _FakeRunner(first_behavior)),
        )
    )
    try:
        await _wait_thread_event(first_started)
        second_task = asyncio.create_task(
            lifecycle.run(
                _call("second", queue=0.5, execution=0.05),
                _binding(lambda: _FakeRunner(second_behavior)),
            )
        )
        # Queue longer than the second execution budget, then admit it.  It
        # must still receive its complete post-admission budget.
        await asyncio.sleep(0.08)
        release_first.set()
        first, second = await asyncio.gather(first_task, second_task)
        assert isinstance(first, SubagentCompleted)
        assert isinstance(second, SubagentCompleted)
        assert second.result == "second"
    finally:
        release_first.set()
        if not first_task.done():
            await first_task
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_scheduler_gate_is_held_until_cancelled_thread_work_is_quiescent() -> None:
    lifecycle = _lifecycle(max_concurrency=1)
    first_thread_started = threading.Event()
    release_first_thread = threading.Event()
    first_thread_done = threading.Event()
    second_admitted = threading.Event()

    def blocking_work() -> None:
        first_thread_started.set()
        release_first_thread.wait()
        first_thread_done.set()

    async def first_behavior(holder: _FakeHolder) -> None:
        await asyncio.to_thread(blocking_work)
        holder.complete("first")

    def second_factory() -> _FakeRunner:
        second_admitted.set()

        async def second_behavior(holder: _FakeHolder) -> None:
            holder.complete("second")

        return _FakeRunner(second_behavior)

    try:
        first_outcome = await lifecycle.run(
            _call("first", execution=0.03, quiescence=0.01),
            _binding(
                lambda: _FakeRunner(first_behavior),
                policy=SubagentQuiescencePolicy.BOUNDED_WITH_REAPER,
            ),
        )
        assert isinstance(first_outcome, SubagentTimedOut)
        assert first_outcome.quiescent is False
        assert first_thread_started.is_set()

        second = asyncio.create_task(
            lifecycle.run(
                _call("second", queue=1.0),
                _binding(second_factory),
            )
        )
        await asyncio.sleep(0.05)

        assert first_thread_done.is_set() is False
        assert second_admitted.is_set() is False

        release_first_thread.set()
        second_outcome = await asyncio.wait_for(second, timeout=1.0)
        assert isinstance(second_outcome, SubagentCompleted)
        assert first_thread_done.is_set()
        assert second_admitted.is_set()
    finally:
        release_first_thread.set()
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_scheduler_reserves_admission_for_another_parent_run() -> None:
    lifecycle = _lifecycle(max_concurrency=2)
    first_a_started = threading.Event()
    second_a_started = threading.Event()
    b_started = threading.Event()
    release_first_a = threading.Event()

    async def first_a(holder: _FakeHolder) -> None:
        holder.mark_running()
        first_a_started.set()
        while not release_first_a.is_set():
            await asyncio.sleep(0.002)
        holder.complete("first A complete")

    async def second_a(holder: _FakeHolder) -> None:
        holder.mark_running()
        second_a_started.set()
        holder.complete("second A complete")

    async def run_b(holder: _FakeHolder) -> None:
        holder.mark_running()
        b_started.set()
        holder.complete("B complete")

    task_a1 = asyncio.create_task(
        lifecycle.run(
            _call("a-1"),
            _binding(lambda: _FakeRunner(first_a), scheduling_key="run-a"),
        )
    )
    await _wait_thread_event(first_a_started)
    task_a2 = asyncio.create_task(
        lifecycle.run(
            _call("a-2"),
            _binding(lambda: _FakeRunner(second_a), scheduling_key="run-a"),
        )
    )
    await asyncio.sleep(0.03)
    assert second_a_started.is_set() is False

    task_b = asyncio.create_task(
        lifecycle.run(
            _call("b-1"),
            _binding(lambda: _FakeRunner(run_b), scheduling_key="run-b"),
        )
    )
    try:
        await _wait_thread_event(b_started)
        outcome_b = await asyncio.wait_for(task_b, timeout=1.0)
        assert isinstance(outcome_b, SubagentCompleted)
        assert second_a_started.is_set() is False

        release_first_a.set()
        await _wait_thread_event(second_a_started)
        outcomes_a = await asyncio.wait_for(
            asyncio.gather(task_a1, task_a2),
            timeout=1.0,
        )
        assert all(isinstance(outcome, SubagentCompleted) for outcome in outcomes_a)
    finally:
        release_first_a.set()
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_scheduler_emits_structured_warning_after_five_second_queue_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = _ProcessSubagentScheduler(max_concurrency=1)
    lifecycle = SubagentTaskLifecycle(_scheduler=scheduler)
    original_submit = scheduler.submit

    def submit_with_aged_queue(record, context) -> None:
        record.queued_at_monotonic -= 5.1
        original_submit(record, context)

    scheduler.submit = submit_with_aged_queue  # type: ignore[method-assign]

    async def behavior(holder: _FakeHolder) -> None:
        holder.complete("queued work complete")

    try:
        with caplog.at_level(
            logging.WARNING,
            logger="deerflow.subagents.lifecycle",
        ):
            outcome = await lifecycle.run(
                _call("queue-warning", queue=10.0),
                _binding(
                    lambda: _FakeRunner(behavior),
                    scheduling_key="run-warning",
                ),
            )
        assert isinstance(outcome, SubagentCompleted)
        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and record.getMessage().startswith(
                "Sub-Agent Task scheduler admitted",
            )
        ]
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning.event == "subagent_scheduler_queue_wait"
        assert warning.execution_id == str(outcome.execution_id)
        assert warning.scheduling_key == "run-warning"
        assert warning.queue_wait_seconds >= 5.0
        assert warning.queue_wait_warning_threshold_seconds == 5.0
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_scheduler_emits_structured_warning_when_queue_timeout_expires(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "deerflow.subagents.lifecycle._QUEUE_WAIT_WARNING_SECONDS",
        0.005,
    )
    lifecycle = _lifecycle(max_concurrency=1)
    blocker_started = threading.Event()
    timed_out_runner_started = False

    async def blocker_behavior(holder: _FakeHolder) -> None:
        holder.mark_running()
        blocker_started.set()
        # Deliberately stall the isolated scheduler loop so the owner-loop
        # deadline wins the queue-timeout race deterministically.
        time.sleep(0.1)
        holder.complete("blocker complete")

    async def timed_out_behavior(holder: _FakeHolder) -> None:
        nonlocal timed_out_runner_started
        timed_out_runner_started = True
        holder.complete("must not start")

    blocker_task = asyncio.create_task(
        lifecycle.run(
            _call("queue-blocker", queue=1.0, execution=1.0),
            _binding(
                lambda: _FakeRunner(blocker_behavior),
                scheduling_key="run-blocker",
            ),
        ),
    )
    try:
        await _wait_thread_event(blocker_started)
        with caplog.at_level(
            logging.WARNING,
            logger="deerflow.subagents.lifecycle",
        ):
            outcome = await lifecycle.run(
                _call("queue-timeout", queue=0.03),
                _binding(
                    lambda: _FakeRunner(timed_out_behavior),
                    scheduling_key="run-timeout",
                ),
            )

        assert isinstance(outcome, SubagentTimedOut)
        assert outcome.timeout_phase is SubagentTimeoutPhase.QUEUE
        assert timed_out_runner_started is False
        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and record.getMessage().startswith(
                "Sub-Agent Task scheduler queue timed out",
            )
        ]
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning.event == "subagent_scheduler_queue_wait"
        assert warning.execution_id == str(outcome.execution_id)
        assert warning.scheduling_key == "run-timeout"
        assert warning.queue_wait_seconds >= 0.005
        assert warning.queue_wait_warning_threshold_seconds == 0.005
    finally:
        await asyncio.wait_for(blocker_task, timeout=1.0)
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_queue_timeout_winner_rejects_late_gate_admission_once(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "deerflow.subagents.lifecycle._QUEUE_WAIT_WARNING_SECONDS",
        0.005,
    )
    admission_entered = threading.Event()
    release_admission = threading.Event()
    original_mark_admission_acquired = _ExecutionRecord.mark_admission_acquired

    def delayed_mark_admission_acquired(
        record: _ExecutionRecord,
        scheduling_key: str,
    ) -> bool:
        admission_entered.set()
        assert release_admission.wait(timeout=1.0)
        return original_mark_admission_acquired(record, scheduling_key)

    monkeypatch.setattr(
        _ExecutionRecord,
        "mark_admission_acquired",
        delayed_mark_admission_acquired,
    )
    lifecycle = _lifecycle(max_concurrency=1)
    timed_out_runner_started = False

    async def timed_out_behavior(holder: _FakeHolder) -> None:
        nonlocal timed_out_runner_started
        timed_out_runner_started = True
        holder.complete("must not start")

    async def follower_behavior(holder: _FakeHolder) -> None:
        holder.complete("follower complete")

    task: asyncio.Task[SubagentTaskOutcome] | None = None
    try:
        with caplog.at_level(
            logging.WARNING,
            logger="deerflow.subagents.lifecycle",
        ):
            task = asyncio.create_task(
                lifecycle.run(
                    _call("late-admission-timeout", queue=0.03),
                    _binding(
                        lambda: _FakeRunner(timed_out_behavior),
                        scheduling_key="run-late-admission",
                    ),
                ),
            )
            await _wait_thread_event(admission_entered)
            await asyncio.sleep(0.06)
            release_admission.set()
            outcome = await asyncio.wait_for(task, timeout=1.0)

        assert isinstance(outcome, SubagentTimedOut)
        assert outcome.timeout_phase is SubagentTimeoutPhase.QUEUE
        assert timed_out_runner_started is False
        queue_records = [record for record in caplog.records if record.event == "subagent_scheduler_queue_wait" and record.execution_id == str(outcome.execution_id)]
        assert len(queue_records) == 1
        assert (
            queue_records[0]
            .getMessage()
            .startswith(
                "Sub-Agent Task scheduler queue timed out",
            )
        )

        follower = await asyncio.wait_for(
            lifecycle.run(
                _call("late-admission-follower", queue=0.2),
                _binding(
                    lambda: _FakeRunner(follower_behavior),
                    scheduling_key="run-late-admission",
                ),
            ),
            timeout=1.0,
        )
        assert isinstance(follower, SubagentCompleted)
    finally:
        release_admission.set()
        if task is not None and not task.done():
            await asyncio.wait_for(task, timeout=1.0)
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_scheduler_copies_parent_context_but_detaches_child_writer() -> None:
    lifecycle = _lifecycle()
    marker: ContextVar[str] = ContextVar("subagent_lifecycle_marker")
    marker_token = marker.set("parent-context")
    child_token = var_child_runnable_config.set({"callbacks": "parent-writer"})
    observed: list[tuple[str, object]] = []

    async def behavior(holder: _FakeHolder) -> None:
        holder.complete("ok")

    def factory() -> _FakeRunner:
        observed.append((marker.get(), var_child_runnable_config.get()))
        return _FakeRunner(behavior)

    try:
        outcome = await lifecycle.run(_call(), _binding(factory))
        assert isinstance(outcome, SubagentCompleted)
        assert observed == [("parent-context", None)]
    finally:
        var_child_runnable_config.reset(child_token)
        marker.reset(marker_token)
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_private_timeout_waits_for_graph_finally_and_inherited_barrier() -> None:
    lifecycle = _lifecycle()
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    cleanup_done = threading.Event()
    barrier = _BlockingBarrier()

    async def behavior(holder: _FakeHolder) -> None:
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            cleanup_entered.set()
            while not release_cleanup.is_set():
                await asyncio.sleep(0.002)
            holder.publish(records=[{"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}])
            cleanup_done.set()

    task = asyncio.create_task(
        lifecycle.run(
            _call(execution=0.03),
            _binding(lambda: _FakeRunner(behavior), barrier=barrier),
        )
    )
    try:
        await _wait_thread_event(cleanup_entered)
        await asyncio.sleep(0.02)
        assert not task.done()
        release_cleanup.set()
        await _wait_thread_event(cleanup_done)
        await _wait_thread_event(barrier.waiting)
        assert barrier.sealed.is_set()
        assert not task.done()
        barrier.release.set()
        outcome = await asyncio.wait_for(task, timeout=1.0)
        assert isinstance(outcome, SubagentTimedOut)
        assert outcome.timeout_phase is SubagentTimeoutPhase.EXECUTION
        assert outcome.quiescent is True
        assert outcome.usage_completeness is SubagentUsageCompleteness.FINAL_OBSERVED
        assert outcome.usage is not None and outcome.usage.total_tokens == 2
    finally:
        release_cleanup.set()
        barrier.release.set()
        if not task.done():
            await task
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_private_caller_cancellation_ignores_repeated_cancel_until_quiescent() -> None:
    lifecycle = _lifecycle()
    started = threading.Event()
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    cleanup_done = threading.Event()
    barrier = _BlockingBarrier()

    async def behavior(holder: _FakeHolder) -> None:
        started.set()
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            cleanup_entered.set()
            while not release_cleanup.is_set():
                await asyncio.sleep(0.002)
            cleanup_done.set()

    task = asyncio.create_task(
        lifecycle.run(
            _call(),
            _binding(lambda: _FakeRunner(behavior), barrier=barrier),
        )
    )
    try:
        await _wait_thread_event(started)
        task.cancel("first parent cancellation")
        await _wait_thread_event(cleanup_entered)
        task.cancel("repeated parent cancellation")
        await asyncio.sleep(0.02)
        assert not task.done()
        release_cleanup.set()
        await _wait_thread_event(cleanup_done)
        await _wait_thread_event(barrier.waiting)
        assert not task.done()
        barrier.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
    finally:
        release_cleanup.set()
        barrier.release.set()
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_aclose_cancels_and_joins_an_active_graph_before_returning() -> None:
    lifecycle = _lifecycle()
    started = threading.Event()
    graph_finally = threading.Event()

    async def behavior(holder: _FakeHolder) -> None:
        del holder
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            graph_finally.set()

    run_task = asyncio.create_task(lifecycle.run(_call(), _binding(lambda: _FakeRunner(behavior))))
    await _wait_thread_event(started)

    await asyncio.wait_for(lifecycle.aclose(), timeout=1.0)
    outcome = await asyncio.wait_for(run_task, timeout=1.0)

    assert graph_finally.is_set()
    assert isinstance(outcome, SubagentCancelled)
    assert outcome.cancellation_code is SubagentCancellationCode.LIFECYCLE_SHUTDOWN
    assert lifecycle._active_execution_count_for_tests() == 0


@pytest.mark.asyncio
async def test_non_private_bounded_policy_returns_latest_then_reaper_joins() -> None:
    lifecycle = _lifecycle()
    cancellation_seen = threading.Event()
    release_runner = threading.Event()

    async def behavior(holder: _FakeHolder) -> None:
        while not release_runner.is_set():
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                cancellation_seen.set()
        holder.publish(records=[{"input_tokens": 4, "output_tokens": 1, "total_tokens": 5}])
        holder.complete("late completion must not replace timeout")

    try:
        outcome = await lifecycle.run(
            _call(execution=0.02, quiescence=0.02),
            _binding(
                lambda: _FakeRunner(behavior),
                policy=SubagentQuiescencePolicy.BOUNDED_WITH_REAPER,
                barrier=None,
            ),
        )
        assert isinstance(outcome, SubagentTimedOut)
        assert outcome.quiescent is False
        assert outcome.usage_completeness is SubagentUsageCompleteness.LATEST_OBSERVED
        assert cancellation_seen.is_set()
        assert lifecycle._active_execution_count_for_tests() == 1

        release_runner.set()
        await _wait_until(lambda: lifecycle._active_execution_count_for_tests() == 0)
        assert isinstance(outcome, SubagentTimedOut)
    finally:
        release_runner.set()
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_bounded_timeout_seals_parent_barrier_before_returning() -> None:
    lifecycle = _lifecycle()
    owner_loop = asyncio.get_running_loop()
    barrier = ParentExecutionBarrier()
    late_attempt_done = threading.Event()
    target_called = threading.Event()
    late_error: list[str] = []
    parent_binding = ParentExecutionBinding(
        profile=object(),  # type: ignore[arg-type]
        state={},
        context={},
        config={},
        owner_loop=owner_loop,
        store=None,
        barrier=barrier,
    )

    async def target() -> None:
        target_called.set()

    async def behavior(holder: _FakeHolder) -> None:
        del holder
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            try:
                await invoke_parent_operation_on_owner_loop(
                    parent_binding,
                    target,
                )
            except RuntimeError as exc:
                late_error.append(str(exc))
            finally:
                late_attempt_done.set()

    try:
        outcome = await lifecycle.run(
            _call(execution=0.02, quiescence=0.02),
            _binding(
                lambda: _FakeRunner(behavior),
                policy=SubagentQuiescencePolicy.BOUNDED_WITH_REAPER,
                barrier=barrier,
                owner_loop_quiescent=barrier.is_quiescent,
            ),
        )
        assert isinstance(outcome, SubagentTimedOut)
        assert outcome.quiescent is False
        await _wait_thread_event(late_attempt_done)
        await _wait_until(
            lambda: lifecycle._active_execution_count_for_tests() == 0,
        )

        assert target_called.is_set() is False
        assert late_error == ["parent execution barrier is sealed"]
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_non_private_bounded_policy_limits_post_graph_barrier_wait() -> None:
    lifecycle = _lifecycle()
    barrier = _BlockingBarrier()

    async def behavior(holder: _FakeHolder) -> None:
        holder.complete("completed before inherited cleanup")

    try:
        outcome = await asyncio.wait_for(
            lifecycle.run(
                _call(quiescence=0.03),
                _binding(
                    lambda: _FakeRunner(behavior),
                    policy=SubagentQuiescencePolicy.BOUNDED_WITH_REAPER,
                    barrier=barrier,
                    owner_loop_quiescent=lambda: True,
                ),
            ),
            timeout=0.2,
        )
        assert isinstance(outcome, SubagentCompleted)
        assert outcome.quiescent is False
        assert outcome.usage_completeness is SubagentUsageCompleteness.LATEST_OBSERVED
        assert lifecycle._active_execution_count_for_tests() == 1

        barrier.release.set()
        await _wait_until(lambda: lifecycle._active_execution_count_for_tests() == 0)
    finally:
        barrier.release.set()
        await lifecycle.aclose()


def test_bounded_reaper_survives_the_callers_event_loop_closing() -> None:
    """Process lifecycle ownership must not depend on a one-shot SDK loop."""

    lifecycle = _lifecycle()
    cancellation_seen = threading.Event()
    release_runner = threading.Event()

    async def behavior(holder: _FakeHolder) -> None:
        while not release_runner.is_set():
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                cancellation_seen.set()
        holder.complete("late completion must not replace timeout")

    async def invoke_once() -> SubagentTimedOut:
        outcome = await lifecycle.run(
            _call(execution=0.02, quiescence=0.02),
            _binding(
                lambda: _FakeRunner(behavior),
                policy=SubagentQuiescencePolicy.BOUNDED_WITH_REAPER,
                barrier=None,
            ),
        )
        assert isinstance(outcome, SubagentTimedOut)
        assert outcome.quiescent is False
        return outcome

    outcome = asyncio.run(invoke_once())
    assert cancellation_seen.is_set()
    assert lifecycle._active_execution_count_for_tests() == 1

    # ``asyncio.run`` has closed the call's owner loop.  The process-owned
    # reaper must still observe the real graph unwind and release its UUID
    # registry entry without relying on that dead loop.
    release_runner.set()
    _wait_until_sync(lambda: lifecycle._active_execution_count_for_tests() == 0)
    assert outcome.quiescent is False

    # Explicit close may run on a different composition-root loop.
    asyncio.run(lifecycle.aclose())


def test_bounded_reaper_does_not_wait_forever_on_a_stopped_owner_loop() -> None:
    lifecycle = _lifecycle(max_concurrency=1)
    owner_loop = asyncio.new_event_loop()
    barrier = _BlockingBarrier()
    settlement_called = threading.Event()

    async def behavior(holder: _FakeHolder) -> None:
        holder.complete("graph finished before owner loop stopped")

    async def settle_usage(_settlement: SubagentUsageSettlement) -> None:
        settlement_called.set()

    async def invoke_once() -> SubagentCompleted:
        outcome = await lifecycle.run(
            _call(quiescence=0.02),
            _binding(
                lambda: _FakeRunner(behavior),
                policy=SubagentQuiescencePolicy.BOUNDED_WITH_REAPER,
                barrier=barrier,
                settle_usage=settle_usage,
                owner_loop_quiescent=lambda: True,
            ),
        )
        assert isinstance(outcome, SubagentCompleted)
        assert outcome.quiescent is False
        return outcome

    try:
        owner_loop.run_until_complete(invoke_once())
        assert owner_loop.is_running() is False
        assert owner_loop.is_closed() is False
        assert lifecycle._active_execution_count_for_tests() == 1

        barrier.release.set()
        _wait_until_sync(
            lambda: lifecycle._active_execution_count_for_tests() == 0,
            timeout=0.3,
        )

        assert settlement_called.is_set() is False
        asyncio.run(asyncio.wait_for(lifecycle.aclose(), timeout=1.0))
    finally:
        barrier.release.set()
        if lifecycle._active_execution_count_for_tests() != 0:
            owner_loop.run_until_complete(asyncio.sleep(0.05))
        asyncio.run(lifecycle.aclose())
        owner_loop.close()


def test_bounded_run_keeps_owner_loop_alive_until_active_parent_receipt_unwinds() -> None:
    lifecycle = _lifecycle(max_concurrency=1)
    owner_loop = asyncio.new_event_loop()
    barrier = ParentExecutionBarrier()
    release_cleanup = threading.Event()
    cleanup_done = threading.Event()
    timer = threading.Timer(0.08, release_cleanup.set)
    parent_binding = ParentExecutionBinding(
        profile=object(),  # type: ignore[arg-type]
        state={},
        context={},
        config={},
        owner_loop=owner_loop,
        store=None,
        barrier=barrier,
    )

    async def owner_target() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            while not release_cleanup.is_set():
                await asyncio.sleep(0.002)
            cleanup_done.set()

    async def behavior(holder: _FakeHolder) -> None:
        await invoke_parent_operation_on_owner_loop(
            parent_binding,
            owner_target,
        )
        holder.complete("must not replace timeout")

    async def invoke_once() -> SubagentTimedOut:
        outcome = await lifecycle.run(
            _call(execution=0.02, quiescence=0.02),
            _binding(
                lambda: _FakeRunner(behavior),
                policy=SubagentQuiescencePolicy.BOUNDED_WITH_REAPER,
                barrier=barrier,
                owner_loop_quiescent=barrier.is_quiescent,
            ),
        )
        assert isinstance(outcome, SubagentTimedOut)
        return outcome

    try:
        timer.start()
        outcome = owner_loop.run_until_complete(invoke_once())

        assert cleanup_done.is_set()
        assert barrier.active_operations == 0
        assert outcome.quiescent is True
        assert lifecycle._active_execution_count_for_tests() == 0
    finally:
        release_cleanup.set()
        if lifecycle._active_execution_count_for_tests() != 0:
            owner_loop.run_until_complete(asyncio.sleep(0.05))
        timer.cancel()
        asyncio.run(lifecycle.aclose())
        owner_loop.close()


@pytest.mark.asyncio
async def test_progress_observer_failure_cancels_then_waits_for_private_cleanup() -> None:
    lifecycle = _lifecycle()
    cleanup_done = threading.Event()

    async def behavior(holder: _FakeHolder) -> None:
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            await asyncio.sleep(0.03)
            cleanup_done.set()

    async def broken_observer(event: SubagentTaskEvent) -> None:
        if isinstance(event, SubagentTaskSnapshot) and event.status is SubagentTaskStatus.RUNNING:
            raise RuntimeError("delivery is unavailable")

    try:
        outcome = await lifecycle.run(
            _call(),
            _binding(lambda: _FakeRunner(behavior)),
            observers=(broken_observer,),
        )
        assert isinstance(outcome, SubagentFailed)
        assert outcome.quiescent is True
        assert cleanup_done.is_set()
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_terminal_observer_failure_does_not_rewrite_committed_success() -> None:
    lifecycle = _lifecycle()

    async def behavior(holder: _FakeHolder) -> None:
        holder.complete("semantic success")

    async def broken_terminal_observer(event: SubagentTaskEvent) -> None:
        if isinstance(event, SubagentCompleted):
            raise RuntimeError("terminal transport failed")

    try:
        outcome = await lifecycle.run(
            _call(),
            _binding(lambda: _FakeRunner(behavior)),
            observers=(broken_terminal_observer,),
        )
        assert isinstance(outcome, SubagentCompleted)
        assert outcome.result == "semantic success"
    finally:
        await lifecycle.aclose()


@pytest.mark.asyncio
async def test_submission_rejected_while_closing_acknowledges_quiescence() -> None:
    scheduler = _ProcessSubagentScheduler(max_concurrency=1)
    scheduler.begin_close()
    lifecycle = SubagentTaskLifecycle(_scheduler=scheduler)
    factory_called = False

    def factory() -> _FakeRunner:
        nonlocal factory_called
        factory_called = True

        async def behavior(holder: _FakeHolder) -> None:
            holder.complete("must not run")

        return _FakeRunner(behavior)

    outcome = await asyncio.wait_for(
        lifecycle.run(_call(), _binding(factory)),
        timeout=1.0,
    )
    assert isinstance(outcome, SubagentFailed)
    assert outcome.quiescent is True
    assert factory_called is False
    await lifecycle.aclose()


@pytest.mark.asyncio
async def test_concurrent_aclose_calls_share_the_same_teardown_barrier() -> None:
    lifecycle = _lifecycle()
    started = threading.Event()
    cancellation_observed = threading.Event()
    release = threading.Event()
    graph_unwound = threading.Event()

    async def behavior(holder: _FakeHolder) -> None:
        holder.mark_running()
        started.set()
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancellation_observed.set()
            while not release.is_set():
                await asyncio.sleep(0.002)
        finally:
            graph_unwound.set()

    run_task = asyncio.create_task(
        lifecycle.run(
            _call(),
            _binding(lambda: _FakeRunner(behavior)),
        )
    )
    await _wait_thread_event(started)

    close_one = asyncio.create_task(lifecycle.aclose())
    await _wait_thread_event(cancellation_observed)
    close_two = asyncio.create_task(lifecycle.aclose())
    await asyncio.sleep(0.02)

    assert close_one.done() is False
    assert close_two.done() is False
    assert graph_unwound.is_set() is False

    release.set()
    await asyncio.wait_for(asyncio.gather(close_one, close_two), timeout=1.0)
    outcome = await asyncio.wait_for(run_task, timeout=1.0)

    assert isinstance(outcome, SubagentCancelled)
    assert graph_unwound.is_set()
    assert lifecycle._active_execution_count_for_tests() == 0


def test_sync_process_exit_fallback_is_idempotent() -> None:
    scheduler = _ProcessSubagentScheduler(max_concurrency=1)
    scheduler.close_sync_fallback(timeout_seconds=0)
    scheduler.close_sync_fallback(timeout_seconds=0)
    assert scheduler.active_count() == 0


@pytest.mark.asyncio
async def test_scheduler_loop_restart_reaps_live_work_without_releasing_the_replacement_gate() -> None:
    scheduler = _ProcessSubagentScheduler(max_concurrency=1)
    lifecycle = SubagentTaskLifecycle(_scheduler=scheduler)
    stale_loop: asyncio.AbstractEventLoop | None = None
    stale_thread: threading.Thread | None = None
    stale_barrier = _CountingBlockingBarrier()
    replacement_started = threading.Event()
    release_replacement = threading.Event()
    replacement_thread_work_finished = threading.Event()
    queued_started = threading.Event()
    old_run: asyncio.Task[object] | None = None
    replacement_run: asyncio.Task[object] | None = None
    queued_run: asyncio.Task[object] | None = None

    async def old_behavior(holder: _FakeHolder) -> None:
        holder.complete("completion from the dead scheduler epoch")

    async def replacement_behavior(holder: _FakeHolder) -> None:
        replacement_started.set()
        while not release_replacement.is_set():
            await asyncio.sleep(0.002)
        await asyncio.to_thread(replacement_thread_work_finished.set)
        holder.complete("replacement complete")

    async def queued_behavior(holder: _FakeHolder) -> None:
        queued_started.set()
        holder.complete("queued complete")

    try:
        old_run = asyncio.create_task(
            lifecycle.run(
                _call("before-loop-death"),
                _binding(
                    lambda: _FakeRunner(old_behavior),
                    barrier=stale_barrier,
                ),
            ),
        )
        await _wait_thread_event(stale_barrier.first_waiter)
        with scheduler._lock:
            stale_loop = scheduler._loop
            stale_thread = scheduler._thread
            stale_gate = scheduler._gate
        assert stale_loop is not None
        assert stale_thread is not None
        assert stale_gate is not None

        stale_loop.call_soon_threadsafe(stale_loop.stop)
        await asyncio.to_thread(stale_thread.join, 1.0)
        assert stale_thread.is_alive() is False

        replacement_run = asyncio.create_task(
            lifecycle.run(
                _call("after-loop-death"),
                _binding(lambda: _FakeRunner(replacement_behavior)),
            )
        )
        await _wait_thread_event(replacement_started)
        with scheduler._lock:
            replacement = scheduler._loop
            replacement_gate = scheduler._gate

        assert replacement is not stale_loop
        assert replacement_gate is not stale_gate

        queued_run = asyncio.create_task(
            lifecycle.run(
                _call("queued-behind-replacement"),
                _binding(lambda: _FakeRunner(queued_behavior)),
            )
        )
        await asyncio.sleep(0.03)
        assert queued_started.is_set() is False

        stale_barrier.release.set()
        old_outcome = await asyncio.wait_for(old_run, timeout=1.0)
        assert isinstance(old_outcome, SubagentFailed)
        assert old_outcome.failure_code is SubagentFailureCode.EXECUTION_FAILED
        assert old_outcome.quiescent is True
        await _wait_until(stale_loop.is_closed)
        assert stale_loop.is_closed()
        await asyncio.sleep(0.03)
        assert queued_started.is_set() is False

        release_replacement.set()
        replacement_outcome = await asyncio.wait_for(replacement_run, timeout=1.0)
        queued_outcome = await asyncio.wait_for(queued_run, timeout=1.0)
        assert isinstance(replacement_outcome, SubagentCompleted)
        assert isinstance(queued_outcome, SubagentCompleted)
        assert replacement_thread_work_finished.is_set()
        assert scheduler.active_count() == 0
        await asyncio.wait_for(lifecycle.aclose(), timeout=1.0)
    finally:
        stale_barrier.release.set()
        release_replacement.set()
        pending_runs = tuple(task for task in (old_run, replacement_run, queued_run) if task is not None and not task.done())
        if pending_runs:
            await asyncio.wait_for(
                asyncio.gather(*pending_runs, return_exceptions=True),
                timeout=1.0,
            )
        if not lifecycle._closed:
            await asyncio.wait_for(lifecycle.aclose(), timeout=1.0)
        if stale_loop is not None and not stale_loop.is_closed():
            assert stale_loop.is_running() is False
            stale_loop.close()


@pytest.mark.asyncio
async def test_aclose_reaps_live_work_after_scheduler_loop_death_without_another_submission() -> None:
    scheduler = _ProcessSubagentScheduler(max_concurrency=1)
    lifecycle = SubagentTaskLifecycle(_scheduler=scheduler)
    stale_barrier = _CountingBlockingBarrier()
    stale_loop: asyncio.AbstractEventLoop | None = None
    stale_thread: threading.Thread | None = None
    run_task: asyncio.Task[SubagentTaskOutcome] | None = None
    close_task: asyncio.Task[None] | None = None

    async def behavior(holder: _FakeHolder) -> None:
        holder.complete("completion awaiting dead-loop cleanup")

    def drain_stale_loop_blocking() -> None:
        assert stale_loop is not None
        failures: list[BaseException] = []

        def drain() -> None:
            try:
                asyncio.set_event_loop(stale_loop)
                pending = tuple(asyncio.all_tasks(stale_loop))
                for task in pending:
                    task.cancel()
                if pending:
                    stale_loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True),
                    )
            except BaseException as exc:
                failures.append(exc)
            finally:
                asyncio.set_event_loop(None)

        cleanup_thread = threading.Thread(target=drain)
        cleanup_thread.start()
        cleanup_thread.join(timeout=1.0)
        assert cleanup_thread.is_alive() is False
        assert failures == []

    try:
        run_task = asyncio.create_task(
            lifecycle.run(
                _call("close-after-loop-death"),
                _binding(
                    lambda: _FakeRunner(behavior),
                    barrier=stale_barrier,
                ),
            )
        )
        await _wait_thread_event(stale_barrier.first_waiter)
        with scheduler._lock:
            stale_loop = scheduler._loop
            stale_thread = scheduler._thread
        assert stale_loop is not None
        assert stale_thread is not None

        stale_loop.call_soon_threadsafe(stale_loop.stop)
        await asyncio.to_thread(stale_thread.join, 1.0)
        assert stale_thread.is_alive() is False
        stale_barrier.release.set()

        close_task = asyncio.create_task(lifecycle.aclose())
        await _wait_until(close_task.done, timeout=0.5)
        await close_task
        outcome = await asyncio.wait_for(run_task, timeout=1.0)

        assert isinstance(outcome, SubagentCancelled)
        assert outcome.cancellation_code is SubagentCancellationCode.LIFECYCLE_SHUTDOWN
        assert outcome.quiescent is True
        assert scheduler.active_count() == 0
    finally:
        stale_barrier.release.set()
        if stale_loop is not None and stale_thread is not None and not stale_thread.is_alive():
            drain_stale_loop_blocking()
        if close_task is not None and not close_task.done():
            await asyncio.wait_for(close_task, timeout=1.0)
        elif not lifecycle._closed:
            await asyncio.wait_for(lifecycle.aclose(), timeout=1.0)
        if run_task is not None and not run_task.done():
            await asyncio.wait_for(run_task, timeout=1.0)
        if stale_loop is not None and not stale_loop.is_closed():
            stale_loop.close()


@pytest.mark.asyncio
async def test_aclose_waits_for_live_graph_unwind_after_scheduler_loop_death() -> None:
    scheduler = _ProcessSubagentScheduler(max_concurrency=1)
    lifecycle = SubagentTaskLifecycle(_scheduler=scheduler)
    graph_started = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    cleanup_finished = threading.Event()
    stale_loop: asyncio.AbstractEventLoop | None = None
    stale_thread: threading.Thread | None = None
    run_task: asyncio.Task[SubagentTaskOutcome] | None = None
    close_task: asyncio.Task[None] | None = None

    async def behavior(holder: _FakeHolder) -> None:
        holder.mark_running()
        graph_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            while not release_cleanup.is_set():
                await asyncio.sleep(0.002)
            cleanup_finished.set()

    def drain_stale_loop_blocking() -> None:
        assert stale_loop is not None

        def drain() -> None:
            asyncio.set_event_loop(stale_loop)
            pending = tuple(asyncio.all_tasks(stale_loop))
            for task in pending:
                task.cancel()
            if pending:
                stale_loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True),
                )
            asyncio.set_event_loop(None)

        cleanup_thread = threading.Thread(target=drain)
        cleanup_thread.start()
        cleanup_thread.join(timeout=1.0)
        assert cleanup_thread.is_alive() is False

    try:
        run_task = asyncio.create_task(
            lifecycle.run(
                _call("live-graph-at-loop-death"),
                _binding(lambda: _FakeRunner(behavior)),
            )
        )
        await _wait_thread_event(graph_started)
        with scheduler._lock:
            stale_loop = scheduler._loop
            stale_thread = scheduler._thread
        assert stale_loop is not None
        assert stale_thread is not None

        stale_loop.call_soon_threadsafe(stale_loop.stop)
        await asyncio.to_thread(stale_thread.join, 1.0)
        assert stale_thread.is_alive() is False

        close_task = asyncio.create_task(lifecycle.aclose())
        await _wait_thread_event(cleanup_started, timeout=0.5)
        await asyncio.sleep(0.02)
        assert close_task.done() is False
        assert cleanup_finished.is_set() is False

        release_cleanup.set()
        await asyncio.wait_for(close_task, timeout=1.0)
        outcome = await asyncio.wait_for(run_task, timeout=1.0)

        assert isinstance(outcome, SubagentCancelled)
        assert outcome.cancellation_code is SubagentCancellationCode.LIFECYCLE_SHUTDOWN
        assert outcome.quiescent is True
        assert cleanup_finished.is_set()
        assert stale_loop.is_closed()
        assert scheduler.active_count() == 0
    finally:
        release_cleanup.set()
        if close_task is not None and not close_task.done():
            await asyncio.wait_for(close_task, timeout=1.0)
        elif not lifecycle._closed:
            await asyncio.wait_for(lifecycle.aclose(), timeout=1.0)
        if run_task is not None and not run_task.done():
            await asyncio.wait_for(run_task, timeout=1.0)
        if stale_loop is not None and stale_thread is not None and not stale_thread.is_alive() and not stale_loop.is_closed():
            drain_stale_loop_blocking()
            stale_loop.close()


def test_production_executor_implements_the_graph_runner_adapter() -> None:
    completed = subprocess.run(
        [sys.executable, "tests/support/subagent_lifecycle_executor_probe.py"],
        cwd=_BACKEND_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "outcome_type": "SubagentCompleted",
        "result": "production graph adapter",
        "quiescent": True,
        "total_tokens": 5,
        "execution_id_is_internal": True,
    }
