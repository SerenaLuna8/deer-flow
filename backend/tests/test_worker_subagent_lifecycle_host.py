"""Worker composition-root ownership of the Sub-Agent Task lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from app.worker.app import _run_service_until_subagents_close


@pytest.mark.asyncio
@pytest.mark.parametrize("service_fails", [False, True])
async def test_worker_closes_subagents_after_service_and_before_return(
    service_fails: bool,
) -> None:
    events: list[str] = []

    class Service:
        async def run(self, _stop_event: asyncio.Event) -> None:
            events.append("service-started")
            events.append("service-stopped")
            if service_fails:
                raise RuntimeError("worker failure")

        async def join_detached(self) -> None:
            events.append("detached-joined")

    class Lifecycle:
        async def aclose(self) -> None:
            events.append("subagents-closed")

    if service_fails:
        with pytest.raises(RuntimeError, match="worker failure"):
            await _run_service_until_subagents_close(
                Service(),  # type: ignore[arg-type]
                asyncio.Event(),
                Lifecycle(),  # type: ignore[arg-type]
            )
    else:
        await _run_service_until_subagents_close(
            Service(),  # type: ignore[arg-type]
            asyncio.Event(),
            Lifecycle(),  # type: ignore[arg-type]
        )

    assert events == [
        "service-started",
        "service-stopped",
        "subagents-closed",
        "detached-joined",
    ]


@pytest.mark.asyncio
async def test_worker_joins_parent_handler_after_subagent_close_releases_it() -> None:
    events: list[str] = []
    child_closed = asyncio.Event()
    parent_cleaned = asyncio.Event()

    async def detached_parent_handler() -> None:
        try:
            await child_closed.wait()
        finally:
            events.append("parent-handler-cleaned")
            parent_cleaned.set()

    class Service:
        detached: asyncio.Task[None] | None = None

        async def run(self, _stop_event: asyncio.Event) -> None:
            events.append("service-stopped")
            self.detached = asyncio.create_task(detached_parent_handler())

        async def join_detached(self) -> None:
            assert self.detached is not None
            await self.detached
            assert parent_cleaned.is_set()
            events.append("detached-joined")

    class Lifecycle:
        async def aclose(self) -> None:
            events.append("subagents-closed")
            child_closed.set()

    await _run_service_until_subagents_close(
        Service(),  # type: ignore[arg-type]
        asyncio.Event(),
        Lifecycle(),  # type: ignore[arg-type]
    )

    assert events == [
        "service-stopped",
        "subagents-closed",
        "parent-handler-cleaned",
        "detached-joined",
    ]
