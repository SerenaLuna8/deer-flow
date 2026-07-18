"""Regression tests for the process-local ``RunManager`` drain primitive.

Gateway no longer creates a ``RunManager`` or owns agent tasks. The independent
Worker still uses a process-local manager while executing a durable job, so its
bounded shutdown and final checkpoint behavior remain valid harness contracts.
"""

from __future__ import annotations

import asyncio
import operator
from contextlib import suppress
from typing import Annotated, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from deerflow.runtime import RunManager, RunStatus


# Module-level so langgraph's get_type_hints (which resolves annotations against
# module globals under `from __future__ import annotations`) can see Annotated.
class _CountState(TypedDict):
    count: Annotated[int, operator.add]


class _CloseableSaver(InMemorySaver):
    """InMemorySaver that fails writes once closed, like a closed pool."""

    def __init__(self) -> None:
        super().__init__()
        self._closed = False
        self.writes_after_close: list[str] = []

    def close(self) -> None:
        self._closed = True

    async def aput(self, *args, **kwargs):
        if self._closed:
            self.writes_after_close.append("aput")
            raise RuntimeError("checkpointer is closed")
        return await super().aput(*args, **kwargs)

    async def aput_writes(self, *args, **kwargs):
        if self._closed:
            self.writes_after_close.append("aput_writes")
            raise RuntimeError("checkpointer is closed")
        return await super().aput_writes(*args, **kwargs)


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_inflight_run():
    """shutdown() cancels the in-flight task, waits for it, marks it interrupted."""
    rm = RunManager()
    record = await rm.create("t-drain")
    await rm.set_status(record.run_id, RunStatus.running)

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def worker() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    record.task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)

        await rm.shutdown(timeout=5.0)

        assert record.task.done()
        assert cancelled.is_set()
        assert record.status == RunStatus.interrupted
    finally:
        if not record.task.done():
            record.task.cancel()
            with suppress(asyncio.CancelledError):
                await record.task


@pytest.mark.asyncio
async def test_shutdown_is_bounded_when_run_ignores_cancellation():
    """A run that swallows cancellation must not make shutdown() hang."""
    rm = RunManager()
    record = await rm.create("t-stubborn")
    await rm.set_status(record.run_id, RunStatus.running)

    started = asyncio.Event()
    stop = asyncio.Event()

    async def stubborn() -> None:
        started.set()
        while not stop.is_set():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if stop.is_set():
                    raise
                # else: swallow — simulates a run stuck in slow cleanup

    record.task = asyncio.create_task(stubborn())
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await rm.shutdown(timeout=0.3)
        elapsed = loop.time() - t0

        assert elapsed < 2.0, f"shutdown took {elapsed:.2f}s; drain is not bounded"
    finally:
        # cleanup the deliberately-stubborn task
        stop.set()
        record.task.cancel()
        with suppress(asyncio.CancelledError):
            await record.task


@pytest.mark.asyncio
async def test_shutdown_is_noop_without_inflight_runs():
    """shutdown() on an idle manager completes cleanly and is idempotent."""
    rm = RunManager()
    await rm.shutdown(timeout=1.0)
    # already-finished runs must not be re-cancelled or error out
    record = await rm.create("t-done")
    await rm.set_status(record.run_id, RunStatus.success)
    await rm.shutdown(timeout=1.0)


@pytest.mark.asyncio
async def test_drain_flushes_real_graph_checkpoint_before_close():
    """End-to-end #3373 guard with a REAL langgraph graph + checkpointer.

    A real run is driven through ``graph.astream`` in a background task, then
    ``RunManager.shutdown()`` drains it. The checkpointer raises once closed
    (mirroring ``psycopg_pool.PoolClosed``). Closing only happens AFTER the
    drain — as the gateway's AsyncExitStack does. The drain must let langgraph
    flush its final checkpoint while the checkpointer is still open, so no write
    lands against a closed checkpointer.

    Unlike the unit/spy tests above, this exercises the real langgraph
    checkpoint-put machinery, so a future langgraph change that cancels (rather
    than awaits) its checkpoint-put task on executor exit would fail this test
    instead of silently regressing #3373.
    """
    from langgraph.graph import END, START, StateGraph

    async def slow(_state: _CountState) -> dict:
        await asyncio.sleep(0.1)
        return {"count": 1}

    saver = _CloseableSaver()
    builder = StateGraph(_CountState)
    for name in ("a", "b", "c"):
        builder.add_node(name, slow)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", "c")
    builder.add_edge("c", END)
    graph = builder.compile(checkpointer=saver)

    rm = RunManager()
    record = await rm.create("t-e2e")
    await rm.set_status(record.run_id, RunStatus.running)
    thread_cfg = {"configurable": {"thread_id": "t-e2e"}}

    started = asyncio.Event()

    async def run() -> None:
        started.set()
        async for _ in graph.astream({"count": 0}, config=thread_cfg):
            pass

    record.task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)

        # Deterministically wait until the run is genuinely in-flight — poll for
        # the first persisted checkpoint instead of a fixed sleep (avoids CI
        # flakiness on slow runners / under event-loop contention).
        async def _await_first_checkpoint() -> None:
            while (await saver.aget_tuple(thread_cfg)) is None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_await_first_checkpoint(), timeout=5.0)

        # The fix: drain while the checkpointer is still open ...
        await rm.shutdown(timeout=5.0)
        # ... and only then close the Worker-owned checkpointer resource.
        saver.close()

        assert saver.writes_after_close == [], f"a checkpoint write raced a closed checkpointer: {saver.writes_after_close}"
        # The final checkpoint landed before close.
        snapshot = await saver.aget_tuple(thread_cfg)
        assert snapshot is not None
    finally:
        if not record.task.done():
            record.task.cancel()
            with suppress(asyncio.CancelledError):
                await record.task


@pytest.mark.asyncio
async def test_shutdown_preserves_status_of_run_completed_during_drain():
    """A run that finishes (e.g. success) during the drain window must keep its
    real terminal status — shutdown must not blanket-overwrite it to
    ``interrupted`` in memory or in the store (Copilot review on PR #3381)."""
    from deerflow.runtime.runs.store.memory import MemoryRunStore

    store = MemoryRunStore()
    rm = RunManager(store=store)
    record = await rm.create("t-complete")
    await rm.set_status(record.run_id, RunStatus.running)

    async def worker() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # The run had effectively finished; swallow the cancellation and
            # record success, like a run that completed in the same tick the
            # shutdown cancelled it.
            pass
        await rm.set_status(record.run_id, RunStatus.success)

    record.task = asyncio.create_task(worker())
    try:
        await asyncio.sleep(0)  # let the task reach its await point

        await rm.shutdown(timeout=5.0)

        assert record.status == RunStatus.success, f"shutdown overwrote in-memory status: {record.status}"
        persisted = await store.get(record.run_id)
        assert persisted is not None and persisted["status"] == "success", f"shutdown overwrote persisted status: {persisted}"
    finally:
        if not record.task.done():
            record.task.cancel()
            with suppress(asyncio.CancelledError):
                await record.task


@pytest.mark.asyncio
async def test_shutdown_surfaces_failed_interrupted_persist(caplog):
    """A failed interrupted-status persist during the drain must be surfaced (with
    the run_id), not silently swallowed by the gather (maintainer review on
    PR #3381)."""
    import logging

    from deerflow.runtime.runs.store.memory import MemoryRunStore

    class _FailingStore(MemoryRunStore):
        async def update_status(self, *args, **kwargs):
            raise RuntimeError("store unavailable")

    rm = RunManager(store=_FailingStore())
    record = await rm.create("t-failpersist")
    record.status = RunStatus.running  # set in memory; the failing store is exercised by the drain

    started = asyncio.Event()

    async def worker() -> None:
        started.set()
        await asyncio.Event().wait()  # blocks until cancelled by the drain

    record.task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)
        with caplog.at_level(logging.WARNING, logger="deerflow.runtime.runs.manager"):
            await rm.shutdown(timeout=5.0)
        assert "Failed to persist status update for run" in caplog.text, caplog.text
        assert record.run_id in caplog.text
    finally:
        if not record.task.done():
            record.task.cancel()
            with suppress(asyncio.CancelledError):
                await record.task
