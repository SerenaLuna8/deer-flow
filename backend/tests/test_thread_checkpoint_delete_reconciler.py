from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.private_work.checkpoint_delete_recovery import (
    CheckpointDeleteReconciler,
    CheckpointDeleteRecoveryReport,
    checkpoint_delete_reconciler_runtime,
)


@pytest.mark.asyncio
async def test_reconciler_starts_immediately_and_shutdown_cancels_periodic_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def run_once(self):
        del self
        entered.set()
        try:
            await release.wait()
            return CheckpointDeleteRecoveryReport(0, 0, 0)
        finally:
            cancelled.set()

    monkeypatch.setattr(CheckpointDeleteReconciler, "run_once", run_once)

    class Raw:
        closed = False

    raw = Raw()

    @asynccontextmanager
    async def raw_runtime():
        try:
            yield raw
        finally:
            raw.closed = True

    async with raw_runtime() as open_raw:
        async with checkpoint_delete_reconciler_runtime(
            open_raw,
            object(),
        ) as reconciler:
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert not reconciler.closed
        assert reconciler.closed
        assert cancelled.is_set()
        # Gateway enters this runtime after the raw saver; LIFO shutdown must
        # stop recovery while that raw context is still usable.
        assert not raw.closed
    assert raw.closed
