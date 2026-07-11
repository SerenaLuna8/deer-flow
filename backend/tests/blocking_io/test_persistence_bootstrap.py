"""PostgreSQL bootstrap keeps synchronous Alembic calls off the event loop."""

import asyncio

import pytest

from deerflow.persistence import bootstrap


@pytest.mark.asyncio
async def test_bootstrap_offloads_alembic_commands(monkeypatch) -> None:
    calls: list[tuple[object, tuple[object, ...]]] = []
    original_to_thread = asyncio.to_thread

    async def spy_to_thread(function, *args):
        calls.append((function, args))
        return await original_to_thread(function, *args)

    monkeypatch.setattr(bootstrap.asyncio, "to_thread", spy_to_thread)
    marker: list[str] = []
    await bootstrap._run_alembic_offload(marker.append, "ran")
    assert marker == ["ran"]
    assert calls == [(marker.append, ("ran",))]
