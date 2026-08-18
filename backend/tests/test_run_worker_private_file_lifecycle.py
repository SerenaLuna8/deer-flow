import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from deerflow.error_codes import PublicRunErrorCode
from deerflow.runtime.runs.manager import RunManager
from deerflow.runtime.runs.schemas import RunStatus
from deerflow.runtime.runs.worker import RunContext, run_agent


class _SuccessfulAgent:
    async def astream(
        self,
        graph_input: dict[str, Any],
        *,
        config: dict[str, Any] | None = None,
        stream_mode: list[str] | None = None,
        subgraphs: bool = False,
    ):
        del graph_input, config, stream_mode, subgraphs
        yield {"messages": []}


class _FailingAgent:
    async def astream(
        self,
        graph_input: dict[str, Any],
        *,
        config: dict[str, Any] | None = None,
        stream_mode: list[str] | None = None,
        subgraphs: bool = False,
    ):
        del graph_input, config, stream_mode, subgraphs
        raise RuntimeError("private-agent-failure")
        yield  # pragma: no cover - keeps this method an async generator


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("delivery_status", "expected_status", "expected_error"),
    [
        ("delivered", RunStatus.success, None),
        (
            "assigned",
            RunStatus.error,
            PublicRunErrorCode.OUTPUT_DELIVERY_INCOMPLETE.value,
        ),
        (
            "intent_recorded",
            RunStatus.error,
            PublicRunErrorCode.OUTPUT_DELIVERY_INCOMPLETE.value,
        ),
    ],
)
async def test_continuation_requires_durable_output_delivery_terminal(
    delivery_status: str,
    expected_status: RunStatus,
    expected_error: str | None,
) -> None:
    class Authority:
        async def restore(self) -> object:
            return object()

        async def finalize(self) -> object:
            return SimpleNamespace(workspace_changes=None, artifacts=())

        async def output_delivery_status(self) -> str:
            return delivery_status

        async def mark_failed(self) -> None:
            pass

        async def release(self) -> None:
            pass

    run_manager = RunManager()
    record = await run_manager.create("thread-output-delivery-terminal")

    await run_agent(
        _bridge([]),
        run_manager,
        record,
        ctx=RunContext(checkpointer=None, file_authority=Authority()),
        agent_factory=lambda **_kwargs: _SuccessfulAgent(),
        graph_input={},
        config={},
    )

    assert record.status is expected_status
    assert record.error == expected_error


@pytest.mark.anyio
async def test_delivered_source_candidate_satisfies_any_one_with_new_output() -> None:
    class Authority:
        async def restore(self) -> object:
            return object()

        async def finalize(self) -> object:
            return SimpleNamespace(
                workspace_changes={
                    "created": ["outputs/new-from-command.txt"],
                    "modified": [],
                    "deleted": [],
                },
                artifacts=[
                    SimpleNamespace(
                        metadata={"logical_path": "outputs/source-candidate.txt"},
                    ),
                ],
            )

        async def output_delivery_status(self) -> str:
            return "delivered"

        async def mark_failed(self) -> None:
            pass

        async def release(self) -> None:
            pass

    run_manager = RunManager()
    record = await run_manager.create("thread-output-delivery-any-one")

    await run_agent(
        _bridge([]),
        run_manager,
        record,
        ctx=RunContext(checkpointer=None, file_authority=Authority()),
        agent_factory=lambda **_kwargs: _SuccessfulAgent(),
        graph_input={},
        config={},
    )

    assert record.status is RunStatus.success
    assert record.error is None


def _bridge(events: list[str]) -> SimpleNamespace:
    async def _publish_end(_run_id: str) -> None:
        events.append("publish_end")

    return SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(side_effect=_publish_end),
        cleanup=AsyncMock(),
    )


def _record_manager_events(run_manager: RunManager, events: list[str]) -> None:
    original_set_finalizing = run_manager.set_finalizing
    original_set_status = run_manager.set_status

    async def _set_finalizing(run_id: str, finalizing: bool) -> None:
        events.append(f"finalizing:{str(finalizing).lower()}")
        await original_set_finalizing(run_id, finalizing)

    async def _set_status(
        run_id: str,
        status: RunStatus,
        error: str | None = None,
    ) -> None:
        events.append(f"status:{status.value}")
        await original_set_status(run_id, status, error=error)

    run_manager.set_finalizing = _set_finalizing  # type: ignore[method-assign]
    run_manager.set_status = _set_status  # type: ignore[method-assign]


async def _cancel_twice(task: asyncio.Task[None]) -> None:
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()


@pytest.mark.anyio
async def test_private_finalize_defers_repeated_cancellation_until_terminal_cleanup() -> None:
    events: list[str] = []
    finalize_started = asyncio.Event()
    finish_finalize = asyncio.Event()

    class Authority:
        async def restore(self) -> object:
            events.append("restore")
            return object()

        async def finalize(self) -> object:
            events.append("finalize:start")
            finalize_started.set()
            try:
                await finish_finalize.wait()
            except asyncio.CancelledError:
                events.append("finalize:cancelled")
                raise
            events.append("finalize:done")
            return object()

        async def mark_failed(self) -> None:
            events.append("mark_failed")

        async def release(self) -> None:
            events.append("release:done")

    run_manager = RunManager()
    record = await run_manager.create("thread-private-finalize")
    _record_manager_events(run_manager, events)
    task = asyncio.create_task(
        run_agent(
            _bridge(events),
            run_manager,
            record,
            ctx=RunContext(checkpointer=None, file_authority=Authority()),
            agent_factory=lambda **_kwargs: _SuccessfulAgent(),
            graph_input={},
            config={},
        )
    )

    await asyncio.wait_for(finalize_started.wait(), timeout=1)
    await _cancel_twice(task)
    finish_finalize.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert "finalize:cancelled" not in events
    assert "mark_failed" not in events
    assert record.status is RunStatus.interrupted
    assert record.finalizing is False
    assert events.index("finalize:done") < events.index("status:interrupted")
    assert events.index("status:interrupted") < events.index("release:done")
    assert events.index("release:done") < events.index("finalizing:false")
    assert events.index("finalizing:false") < events.index("publish_end")


@pytest.mark.anyio
async def test_private_mark_failed_defers_repeated_cancellation_until_error_cleanup() -> None:
    events: list[str] = []
    mark_failed_started = asyncio.Event()
    finish_mark_failed = asyncio.Event()

    class Authority:
        async def restore(self) -> object:
            events.append("restore")
            return object()

        async def finalize(self) -> object:
            events.append("finalize")
            return object()

        async def mark_failed(self) -> None:
            events.append("mark_failed:start")
            mark_failed_started.set()
            try:
                await finish_mark_failed.wait()
            except asyncio.CancelledError:
                events.append("mark_failed:cancelled")
                raise
            events.append("mark_failed:done")

        async def release(self) -> None:
            events.append("release:done")

    run_manager = RunManager()
    record = await run_manager.create("thread-private-failure")
    _record_manager_events(run_manager, events)
    task = asyncio.create_task(
        run_agent(
            _bridge(events),
            run_manager,
            record,
            ctx=RunContext(checkpointer=None, file_authority=Authority()),
            agent_factory=lambda **_kwargs: _FailingAgent(),
            graph_input={},
            config={},
        )
    )

    await asyncio.wait_for(mark_failed_started.wait(), timeout=1)
    await _cancel_twice(task)
    finish_mark_failed.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert "mark_failed:cancelled" not in events
    assert "finalize" not in events
    assert record.status is RunStatus.error
    assert record.finalizing is False
    assert events.index("mark_failed:done") < events.index("status:error")
    assert events.index("status:error") < events.index("release:done")
    assert events.index("release:done") < events.index("finalizing:false")
    assert events.index("finalizing:false") < events.index("publish_end")


@pytest.mark.anyio
async def test_private_cleanup_retries_and_defers_repeated_cancellation() -> None:
    events: list[str] = []
    final_release_started = asyncio.Event()
    finish_release = asyncio.Event()

    class Authority:
        def __init__(self) -> None:
            self.release_attempts = 0

        async def restore(self) -> object:
            events.append("restore")
            return object()

        async def finalize(self) -> object:
            events.append("finalize:done")
            return object()

        async def mark_failed(self) -> None:
            events.append("mark_failed")

        async def release(self) -> None:
            self.release_attempts += 1
            events.append(f"release:{self.release_attempts}:start")
            if self.release_attempts < 3:
                raise RuntimeError("retry cleanup")
            final_release_started.set()
            try:
                await finish_release.wait()
            except asyncio.CancelledError:
                events.append("release:cancelled")
                raise
            events.append("release:done")

    authority = Authority()
    run_manager = RunManager()
    record = await run_manager.create("thread-private-cleanup")
    _record_manager_events(run_manager, events)
    task = asyncio.create_task(
        run_agent(
            _bridge(events),
            run_manager,
            record,
            ctx=RunContext(checkpointer=None, file_authority=authority),
            agent_factory=lambda **_kwargs: _SuccessfulAgent(),
            graph_input={},
            config={},
        )
    )

    await asyncio.wait_for(final_release_started.wait(), timeout=1)
    await _cancel_twice(task)
    finish_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert authority.release_attempts == 3
    assert "release:cancelled" not in events
    assert "mark_failed" not in events
    assert record.status is RunStatus.success
    assert record.finalizing is False
    assert events.index("status:success") < events.index("release:1:start")
    assert events.index("release:done") < events.index("finalizing:false")
    assert events.index("finalizing:false") < events.index("publish_end")


@pytest.mark.anyio
@pytest.mark.parametrize("failed_cleanup", ["runtime", "sandbox"])
async def test_completed_private_response_remains_successful_when_cleanup_fails(
    failed_cleanup: str,
) -> None:
    """A post-response teardown failure cannot invalidate a durable reply."""

    events: list[str] = []

    class Authority:
        def __init__(self) -> None:
            self.release_attempts = 0

        async def restore(self) -> object:
            return object()

        async def finalize(self) -> object:
            return SimpleNamespace(workspace_changes=None, artifacts=())

        async def mark_failed(self) -> None:
            pytest.fail("a completed response must not be marked failed")

        async def release(self) -> None:
            self.release_attempts += 1
            if failed_cleanup == "sandbox":
                raise RuntimeError("sandbox cleanup unavailable")

    class PrivateRuntime:
        def __init__(self) -> None:
            self.close_attempts = 0

        async def aclose(self) -> None:
            self.close_attempts += 1
            if failed_cleanup == "runtime":
                raise RuntimeError("runtime cleanup unavailable")

    authority = Authority()
    private_runtime = PrivateRuntime() if failed_cleanup == "runtime" else None
    run_manager = RunManager()
    record = await run_manager.create(f"thread-private-cleanup-{failed_cleanup}")
    _record_manager_events(run_manager, events)

    def agent_factory(**_kwargs: object) -> _SuccessfulAgent:
        return _SuccessfulAgent()

    def private_runtime_agent_factory(
        *,
        config: object,
        private_runtime: object,
    ) -> _SuccessfulAgent:
        del config, private_runtime
        return _SuccessfulAgent()

    await run_agent(
        _bridge(events),
        run_manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            file_authority=authority,
            private_agent_runtime=private_runtime,
        ),
        agent_factory=(private_runtime_agent_factory if private_runtime is not None else agent_factory),
        graph_input={},
        config={},
    )

    assert record.status is RunStatus.success
    assert record.error is None
    assert record.finalizing is True
    assert "status:error" not in events
    assert events.count("status:success") == 1
    assert authority.release_attempts == (3 if failed_cleanup == "sandbox" else 1)
    if failed_cleanup == "runtime":
        assert private_runtime is not None
        assert private_runtime.close_attempts == 3
    else:
        assert private_runtime is None
    assert events[-1] == "publish_end"
