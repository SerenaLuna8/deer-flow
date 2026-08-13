import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

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
