"""Clean-process probes for the production subagent scheduler."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
import time
from contextvars import ContextVar
from types import MethodType

from langchain_core.runnables.config import var_child_runnable_config

from deerflow.subagents import executor as executor_module
from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.executor import (
    SubagentExecutor,
    SubagentResult,
    SubagentStatus,
    _shutdown_isolated_subagent_loop,
    cleanup_background_task,
    get_background_task_result,
    request_cancel_background_task,
)

_PROBE_CONTEXT: ContextVar[str | None] = ContextVar("subagent_scheduler_probe", default=None)


def _executor(*, trace_id: str, timeout: float = 2.0) -> SubagentExecutor:
    return SubagentExecutor(
        config=SubagentConfig(
            name="probe",
            description="scheduler probe",
            model="probe-model",
            timeout_seconds=timeout,  # type: ignore[arg-type]
        ),
        tools=[],
        trace_id=trace_id,
    )


def _wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("scheduler probe timed out")


def _status(task_id: str) -> SubagentStatus | None:
    result = get_background_task_result(task_id)
    return result.status if result is not None else None


def _complete(result: SubagentResult, task: str) -> SubagentResult:
    result.try_set_terminal(SubagentStatus.COMPLETED, result=task)
    return result


def _single_run_four() -> dict[str, object]:
    executor = _executor(trace_id="single-run")
    release = threading.Event()
    all_started = threading.Event()
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    async def fake_aexecute(
        _self: SubagentExecutor,
        task: str,
        result: SubagentResult | None = None,
    ) -> SubagentResult:
        nonlocal active, max_active
        assert result is not None
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            if active == 4:
                all_started.set()
        try:
            while not release.is_set():
                await asyncio.sleep(0.005)
            return _complete(result, task)
        finally:
            with state_lock:
                active -= 1

    executor._aexecute = MethodType(fake_aexecute, executor)  # type: ignore[method-assign]
    task_ids = [executor.execute_async(f"task-{index}") for index in range(4)]
    try:
        assert all_started.wait(timeout=1.0), "fourth subagent never started"
        release.set()
        _wait_until(lambda: all(_status(task_id) is SubagentStatus.COMPLETED for task_id in task_ids))
        thread_names = [thread.name for thread in threading.enumerate()]
        return {
            "max_active": max_active,
            "scheduler_pool_threads": sum(name.startswith("subagent-scheduler-") for name in thread_names),
            "isolated_loop_threads": thread_names.count("subagent-persistent-loop"),
        }
    finally:
        release.set()
        _shutdown_isolated_subagent_loop()


def _multi_run() -> dict[str, object]:
    release = threading.Event()
    all_started = threading.Event()
    state_lock = threading.Lock()
    observed: dict[str, tuple[str | None, object]] = {}

    async def fake_aexecute(
        _self: SubagentExecutor,
        task: str,
        result: SubagentResult | None = None,
    ) -> SubagentResult:
        assert result is not None
        with state_lock:
            observed[task] = (
                _PROBE_CONTEXT.get(),
                var_child_runnable_config.get(),
            )
            if len(observed) == 8:
                all_started.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return _complete(result, task)

    run_a = _executor(trace_id="run-a")
    run_b = _executor(trace_id="run-b")
    run_a._aexecute = MethodType(fake_aexecute, run_a)  # type: ignore[method-assign]
    run_b._aexecute = MethodType(fake_aexecute, run_b)  # type: ignore[method-assign]
    task_ids: list[str] = []
    token = var_child_runnable_config.set({"callbacks": ["lead-writer"]})
    try:
        for index in range(4):
            context_token = _PROBE_CONTEXT.set("run-a")
            try:
                task_ids.append(run_a.execute_async(f"run-a-{index}"))
            finally:
                _PROBE_CONTEXT.reset(context_token)
            context_token = _PROBE_CONTEXT.set("run-b")
            try:
                task_ids.append(run_b.execute_async(f"run-b-{index}"))
            finally:
                _PROBE_CONTEXT.reset(context_token)

        assert all_started.wait(timeout=1.5), "multi-run submissions were starved"
        release.set()
        _wait_until(lambda: all(_status(task_id) is SubagentStatus.COMPLETED for task_id in task_ids))
        return {
            "started": len(observed),
            "context_matches": all(context == task.rsplit("-", 1)[0] for task, (context, _child) in observed.items()),
            "child_configs_detached": all(child is None for _context, child in observed.values()),
        }
    finally:
        var_child_runnable_config.reset(token)
        release.set()
        _shutdown_isolated_subagent_loop()


def _queued_timeout() -> dict[str, object]:
    executor_module.MAX_CONCURRENT_ISOLATED_SUBAGENTS = 1
    blocker = _executor(trace_id="blocker", timeout=2.0)
    queued = _executor(trace_id="queued", timeout=0.15)
    blocker_started = threading.Event()
    release_blocker = threading.Event()

    async def fake_aexecute(
        _self: SubagentExecutor,
        task: str,
        result: SubagentResult | None = None,
    ) -> SubagentResult:
        assert result is not None
        if task == "blocker":
            blocker_started.set()
            while not release_blocker.is_set():
                await asyncio.sleep(0.005)
        else:
            await asyncio.sleep(0.05)
        return _complete(result, task)

    blocker._aexecute = MethodType(fake_aexecute, blocker)  # type: ignore[method-assign]
    queued._aexecute = MethodType(fake_aexecute, queued)  # type: ignore[method-assign]
    blocker_id = blocker.execute_async("blocker")
    try:
        assert blocker_started.wait(timeout=1.0)
        queued_id = queued.execute_async("queued")
        time.sleep(0.25)
        status_while_queued = _status(queued_id)
        release_blocker.set()
        _wait_until(lambda: _status(blocker_id) is SubagentStatus.COMPLETED)
        _wait_until(lambda: _status(queued_id) is SubagentStatus.COMPLETED)
        return {
            "status_while_queued": status_while_queued.value if status_while_queued else None,
            "final_status": _status(queued_id).value,  # type: ignore[union-attr]
        }
    finally:
        release_blocker.set()
        _shutdown_isolated_subagent_loop()


def _cancel() -> dict[str, object]:
    executor = _executor(trace_id="cancel", timeout=1.0)
    started = threading.Event()
    finalized = threading.Event()

    async def fake_aexecute(
        _self: SubagentExecutor,
        _task: str,
        result: SubagentResult | None = None,
    ) -> SubagentResult:
        assert result is not None
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            finalized.set()
        return result

    executor._aexecute = MethodType(fake_aexecute, executor)  # type: ignore[method-assign]
    task_id = executor.execute_async("cancel-me")
    try:
        assert started.wait(timeout=1.0)
        request_cancel_background_task(task_id)
        _wait_until(lambda: _status(task_id) is SubagentStatus.CANCELLED, timeout=1.0)
        return {
            "status": _status(task_id).value,  # type: ignore[union-attr]
            "coroutine_finalized": finalized.wait(timeout=1.0),
        }
    finally:
        _shutdown_isolated_subagent_loop()


def _cancel_during_submit() -> dict[str, object]:
    executor = _executor(trace_id="cancel-submit", timeout=2.0)
    execution_started = threading.Event()
    submit_scheduled = threading.Event()
    release_submit = threading.Event()
    original_submit = executor_module._submit_to_isolated_loop_in_context

    async def fake_aexecute(
        _self: SubagentExecutor,
        _task: str,
        result: SubagentResult | None = None,
    ) -> SubagentResult:
        assert result is not None
        execution_started.set()
        await asyncio.sleep(30)
        return result

    def delayed_submit(context, coro_factory):
        future = original_submit(context, coro_factory)
        submit_scheduled.set()
        assert release_submit.wait(timeout=1.0)
        return future

    executor._aexecute = MethodType(fake_aexecute, executor)  # type: ignore[method-assign]
    executor_module._submit_to_isolated_loop_in_context = delayed_submit
    submit_thread = threading.Thread(
        target=lambda: executor.execute_async("cancel-during-submit", task_id="cancel-submit-race"),
        daemon=True,
    )
    submit_thread.start()
    try:
        assert submit_scheduled.wait(timeout=1.0)
        assert execution_started.wait(timeout=1.0)
        request_cancel_background_task("cancel-submit-race")
        release_submit.set()
        submit_thread.join(timeout=1.0)
        assert not submit_thread.is_alive()
        _wait_until(lambda: _status("cancel-submit-race") is SubagentStatus.CANCELLED, timeout=1.0)
        return {"status": _status("cancel-submit-race").value}  # type: ignore[union-attr]
    finally:
        release_submit.set()
        executor_module._submit_to_isolated_loop_in_context = original_submit
        _shutdown_isolated_subagent_loop()


def _timeout() -> dict[str, object]:
    executor = _executor(trace_id="timeout", timeout=0.1)
    started = threading.Event()
    finalized = threading.Event()

    async def fake_aexecute(
        _self: SubagentExecutor,
        _task: str,
        result: SubagentResult | None = None,
    ) -> SubagentResult:
        assert result is not None
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            finalized.set()
        return result

    executor._aexecute = MethodType(fake_aexecute, executor)  # type: ignore[method-assign]
    started_at = time.monotonic()
    task_id = executor.execute_async("time-me-out")
    try:
        assert started.wait(timeout=1.0)
        _wait_until(lambda: _status(task_id) is SubagentStatus.TIMED_OUT, timeout=1.0)
        result = get_background_task_result(task_id)
        assert result is not None
        return {
            "status": result.status.value,
            "error": result.error,
            "elapsed": time.monotonic() - started_at,
            "cancel_event": result.cancel_event.is_set(),
            "coroutine_finalized": finalized.wait(timeout=1.0),
        }
    finally:
        _shutdown_isolated_subagent_loop()


def _shutdown() -> dict[str, object]:
    executor = _executor(trace_id="shutdown", timeout=1.0)
    started = threading.Event()

    async def fake_aexecute(
        _self: SubagentExecutor,
        _task: str,
        result: SubagentResult | None = None,
    ) -> SubagentResult:
        assert result is not None
        started.set()
        await asyncio.sleep(30)
        return result

    executor._aexecute = MethodType(fake_aexecute, executor)  # type: ignore[method-assign]
    task_id = executor.execute_async("shutdown-me")
    assert started.wait(timeout=1.0)
    loop = executor_module._isolated_subagent_loop
    thread = executor_module._isolated_subagent_loop_thread
    assert loop is not None
    assert thread is not None
    _shutdown_isolated_subagent_loop()
    _wait_until(lambda: _status(task_id) is SubagentStatus.CANCELLED, timeout=1.0)
    futures = getattr(executor_module, "_background_task_futures", {})
    return {
        "status": _status(task_id).value,  # type: ignore[union-attr]
        "thread_stopped": not thread.is_alive(),
        "loop_closed": loop.is_closed(),
        "tracked_futures": len(futures),
    }


def _shutdown_during_submit() -> dict[str, object]:
    executor = _executor(trace_id="shutdown-submit", timeout=2.0)
    old_loop = executor_module._get_isolated_subagent_loop()
    entered_submit = threading.Event()
    release_submit = threading.Event()
    original_submit = executor_module._submit_to_isolated_loop_in_context

    def delayed_submit(context, coro_factory):
        entered_submit.set()
        assert release_submit.wait(timeout=2.0)
        return original_submit(context, coro_factory)

    executor_module._submit_to_isolated_loop_in_context = delayed_submit
    submit_thread = threading.Thread(
        target=lambda: executor.execute_async(
            "shutdown-during-submit",
            task_id="shutdown-during-submit",
        ),
        daemon=True,
    )
    shutdown_thread = threading.Thread(
        target=executor_module._shutdown_isolated_subagent_loop,
        daemon=True,
    )
    submit_thread.start()
    try:
        assert entered_submit.wait(timeout=1.0)
        shutdown_thread.start()
        _wait_until(
            lambda: (
                getattr(
                    getattr(
                        executor_module,
                        "_isolated_subagent_scheduler_state",
                        None,
                    ),
                    "value",
                    None,
                )
                == "shutting_down"
            ),
            timeout=1.0,
        )
        release_submit.set()
        submit_thread.join(timeout=1.0)
        shutdown_thread.join(timeout=1.0)
        result = get_background_task_result("shutdown-during-submit")
        assert result is not None
        rejected_id = executor.execute_async(
            "after-shutdown",
            task_id="after-shutdown",
        )
        rejected = get_background_task_result(rejected_id)
        assert rejected is not None
        thread_names = [thread.name for thread in threading.enumerate()]
        return {
            "status": result.status.value,
            "error": result.error,
            "new_submission_status": rejected.status.value,
            "new_submission_error": rejected.error,
            "submit_thread_stopped": not submit_thread.is_alive(),
            "shutdown_thread_stopped": not shutdown_thread.is_alive(),
            "old_loop_closed": old_loop.is_closed(),
            "new_loop_created": executor_module._isolated_subagent_loop is not None,
            "isolated_loop_threads": thread_names.count("subagent-persistent-loop"),
        }
    finally:
        release_submit.set()
        submit_thread.join(timeout=1.0)
        shutdown_thread.join(timeout=1.0)
        executor_module._submit_to_isolated_loop_in_context = original_submit
        executor_module._shutdown_isolated_subagent_loop()
        cleanup_background_task("shutdown-during-submit")
        cleanup_background_task("after-shutdown")


def _submission_failure() -> dict[str, object]:
    executor = _executor(trace_id="submission-failure", timeout=2.0)
    captured: dict[str, object] = {}
    original_run_coroutine_threadsafe = executor_module.asyncio.run_coroutine_threadsafe

    def fail_submission(coroutine, _loop):
        captured["coroutine"] = coroutine
        raise RuntimeError("forced submission failure")

    executor_module.asyncio.run_coroutine_threadsafe = fail_submission
    try:
        task_id = executor.execute_async(
            "submission-failure",
            task_id="submission-failure",
        )
        result = get_background_task_result(task_id)
        assert result is not None
        coroutine = captured.get("coroutine")
        return {
            "status": result.status.value,
            "error": result.error,
            "coroutine_closed": getattr(coroutine, "cr_frame", object()) is None,
        }
    finally:
        executor_module.asyncio.run_coroutine_threadsafe = original_run_coroutine_threadsafe
        executor_module._shutdown_isolated_subagent_loop()
        cleanup_background_task("submission-failure")


def _shutdown_reload() -> dict[str, object]:
    old_loop = executor_module._get_isolated_subagent_loop()
    executor_module._shutdown_isolated_subagent_loop()

    reloaded = importlib.reload(executor_module)
    new_loop = reloaded._get_isolated_subagent_loop()
    try:
        return {
            "old_loop_closed": old_loop.is_closed(),
            "new_loop_running": new_loop.is_running(),
            "new_loop_is_distinct": new_loop is not old_loop,
        }
    finally:
        reloaded._shutdown_isolated_subagent_loop()


_SCENARIOS = {
    "single-run-four": _single_run_four,
    "multi-run": _multi_run,
    "queued-timeout": _queued_timeout,
    "cancel": _cancel,
    "cancel-during-submit": _cancel_during_submit,
    "timeout": _timeout,
    "shutdown": _shutdown,
    "shutdown-during-submit": _shutdown_during_submit,
    "submission-failure": _submission_failure,
    "shutdown-reload": _shutdown_reload,
}


def main() -> int:
    scenario = sys.argv[1] if len(sys.argv) > 1 else ""
    if scenario not in _SCENARIOS:
        raise SystemExit(f"unknown scenario: {scenario}")
    print(json.dumps(_SCENARIOS[scenario](), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
