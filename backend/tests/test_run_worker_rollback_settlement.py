import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import deerflow.runtime.runs.checkpoint_rollback as checkpoint_rollback
import deerflow.runtime.runs.worker as run_worker
from deerflow.runtime.runs.manager import RunManager
from deerflow.runtime.runs.schemas import RunStatus
from deerflow.runtime.runs.worker import RunContext, run_agent


def _bridge() -> SimpleNamespace:
    return SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("cancelled_stream", [False, True])
@pytest.mark.parametrize("rollback_raises", [False, True])
async def test_failed_rollback_lands_one_explicit_failure_terminal(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cancelled_stream: bool,
    rollback_raises: bool,
) -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    record.abort_action = "rollback"
    status_spy = AsyncMock(wraps=run_manager.set_status)
    run_manager.set_status = status_spy  # type: ignore[method-assign]
    rollback = AsyncMock(return_value=False)
    if rollback_raises:
        rollback.side_effect = RuntimeError("restore failed")
    monkeypatch.setattr(run_worker, "_rollback_to_pre_run_checkpoint", rollback)

    class AbortingAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            del graph_input, config, stream_mode, subgraphs
            if cancelled_stream:
                raise asyncio.CancelledError
            record.abort_event.set()
            if False:
                yield  # pragma: no cover

    await run_agent(
        _bridge(),
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda **_kwargs: AbortingAgent(),
        graph_input={},
        config={},
    )

    terminal_calls = [item for item in status_spy.await_args_list if item.args[1] not in (RunStatus.pending, RunStatus.running)]
    assert len(terminal_calls) == 1
    assert terminal_calls[0].args[1] is RunStatus.error
    assert terminal_calls[0].kwargs == {"error": "ROLLBACK_FAILED"}
    assert record.status is RunStatus.error
    assert record.error == "ROLLBACK_FAILED"
    rollback.assert_awaited_once()


@pytest.mark.anyio
async def test_successful_rollback_lands_successful_rollback_terminal_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    record.abort_action = "rollback"
    status_spy = AsyncMock(wraps=run_manager.set_status)
    run_manager.set_status = status_spy  # type: ignore[method-assign]
    rollback = AsyncMock(return_value=True)
    monkeypatch.setattr(run_worker, "_rollback_to_pre_run_checkpoint", rollback)

    class AbortingAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            del graph_input, config, stream_mode, subgraphs
            record.abort_event.set()
            if False:
                yield  # pragma: no cover

    await run_agent(
        _bridge(),
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda **_kwargs: AbortingAgent(),
        graph_input={},
        config={},
    )

    terminal_calls = [item for item in status_spy.await_args_list if item.args[1] not in (RunStatus.pending, RunStatus.running)]
    assert len(terminal_calls) == 1
    assert terminal_calls[0].args[1] is RunStatus.error
    assert terminal_calls[0].kwargs == {"error": "Rolled back by user"}
    assert record.error == "Rolled back by user"
    rollback.assert_awaited_once()


@pytest.mark.anyio
async def test_rollback_settlement_defers_cancellation_until_terminal_is_recorded() -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    rollback_started = asyncio.Event()
    release_rollback = asyncio.Event()

    async def rollback() -> bool:
        rollback_started.set()
        await release_rollback.wait()
        return False

    settlement = asyncio.create_task(
        checkpoint_rollback._settle_rollback(
            run_manager=run_manager,
            run_id=record.run_id,
            rollback=rollback,
        ),
    )
    await rollback_started.wait()
    settlement.cancel()
    await asyncio.sleep(0)

    assert not settlement.done()
    release_rollback.set()
    assert await settlement is True
    assert record.status is RunStatus.error
    assert record.error == "ROLLBACK_FAILED"


@pytest.mark.anyio
async def test_run_terminal_does_not_schedule_durable_stream_cleanup() -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    bridge = _bridge()

    class SuccessfulAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            del graph_input, config, stream_mode, subgraphs
            yield {"messages": []}

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda **_kwargs: SuccessfulAgent(),
        graph_input={},
        config={},
    )
    await asyncio.sleep(0)

    bridge.publish_end.assert_awaited_once_with(record.run_id)
    bridge.cleanup.assert_not_awaited()
