"""Reconnect replay compaction for durable private Run streams.

A root ``values`` stream frame carries the complete Run state at one
super-step, so a later root ``values`` frame strictly supersedes every earlier
one. A reconnect catch-up therefore only needs the newest root ``values``
frame below the connection's horizon; dropping the superseded ones keeps the
replay O(state) instead of O(state * super-steps) without changing meaning.

These tests pin three facts:

- the store drops only superseded root ``values`` frames below an explicit
  ``full_state_horizon`` while keeping namespaced subgraph frames, ``custom``
  frames, ``messages`` deltas, the horizon frame itself, and the terminal;
- the bridge reports the newest root ``values`` sequence for one exact Run;
- the reconnect route computes that horizon once per connection and applies
  it to the initial replay page and the durable SSE consumer's later pages.

Event ids remain the original monotonic sequence values; consumers already
accept gaps because they compare cursors by decimal order, not adjacency.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from support.private_thread_seed import seed_private_thread_database

from app.gateway.routers import private_work as private_work_router
from app.private_work.run_repository import (
    PrivateRunCreate,
    PrivateRunRepository,
)
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from deerflow.runtime.events.models import StoredStreamFrame, StreamFrame
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.events.stream import PostgresStreamBridge


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_replay_horizon_drops_only_superseded_root_values_frames(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        thread_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        event_store = DbRunEventStore(seed.factory, run_event_notify_enabled=False)
        bridge = PostgresStreamBridge(seed.factory, run_event_notify_enabled=False)
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(
                    asset_id=seed.project_agent_id,
                    scope="project",
                ),
            )
            await PrivateRunRepository(session).create_terminal_empty_shell(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                request=PrivateRunCreate(
                    run_id=run_id,
                    status="success",
                ),
            )

        assert (
            await bridge.latest_full_state_seq(
                seed.owner_a_scope,
                thread_id,
                run_id=run_id,
            )
            == 0
        )

        appended: list[StoredStreamFrame] = []
        async with seed.factory() as session, session.begin():
            for frame in (
                StreamFrame(event="values", data={"messages": ["turn-1"]}),
                StreamFrame(event="custom", data={"type": "task_running"}),
                StreamFrame(event="values|subgraph-1", data={"messages": ["sub"]}),
                StreamFrame(event="values", data={"messages": ["turn-1", "turn-2"]}),
                StreamFrame(event="messages", data=[{"type": "ai"}, {}]),
                StreamFrame(
                    event="values",
                    data={"messages": ["turn-1", "turn-2", "turn-3"]},
                ),
                StreamFrame(event="messages", data=[{"type": "ai"}, {}]),
            ):
                appended.append(
                    await event_store.append_stream_frame(
                        session,
                        scope=seed.owner_a_scope,
                        thread_id=thread_id,
                        run_id=run_id,
                        frame=frame,
                    )
                )
        async with seed.factory() as session, session.begin():
            appended.append(
                await event_store.append_stream_frame(
                    session,
                    scope=seed.owner_a_scope,
                    thread_id=thread_id,
                    run_id=run_id,
                    frame=StreamFrame.end(status="success"),
                )
            )

        horizon = await bridge.latest_full_state_seq(
            seed.owner_a_scope,
            thread_id,
            run_id=run_id,
        )
        # The newest *root* values frame, not the namespaced subgraph frame.
        assert horizon == int(appended[5].id)

        compacted = await bridge.read_after(
            seed.owner_a_scope,
            thread_id,
            cursor=0,
            limit=100,
            run_id=run_id,
            full_state_horizon=horizon,
        )
        assert [frame.id for frame in compacted] == [
            appended[1].id,  # custom survives
            appended[2].id,  # namespaced subgraph values survives
            appended[4].id,  # messages delta survives
            appended[5].id,  # the horizon frame itself survives
            appended[6].id,  # frames after the horizon survive
            appended[7].id,  # terminal survives
        ]
        assert [frame.event for frame in compacted] == [
            "custom",
            "values|subgraph-1",
            "messages",
            "values",
            "messages",
            "end",
        ]

        uncompacted = await bridge.read_after(
            seed.owner_a_scope,
            thread_id,
            cursor=0,
            limit=100,
            run_id=run_id,
        )
        assert [frame.id for frame in uncompacted] == [frame.id for frame in appended]
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_durable_consumer_forwards_full_state_horizon_to_follow_up_pages() -> None:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    initial = tuple(
        StoredStreamFrame(
            id=str(index),
            thread_id=thread_id,
            run_id=run_id,
            event="updates",
            data={"index": index},
        )
        for index in range(1, 101)
    )
    terminal = StoredStreamFrame(
        id="101",
        thread_id=thread_id,
        run_id=run_id,
        event="end",
        data={"status": "completed"},
        terminal=True,
    )
    bridge = SimpleNamespace(
        read_after=AsyncMock(return_value=(terminal,)),
        ensure_settled_terminal=AsyncMock(return_value=terminal),
    )
    service = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(status="success", error=None),
        ),
    )
    context = SimpleNamespace(request_id="horizon-page", resource_scope=object())
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

    chunks = [
        chunk
        async for chunk in private_work_router._durable_private_sse_consumer(
            bridge=bridge,
            service=service,
            context=context,
            thread_id=thread_id,
            run_id=run_id,
            request=request,
            cursor=0,
            initial_frames=initial,
            cancel_on_disconnect=False,
            full_state_horizon=7,
        )
    ]

    assert len(chunks) == 101
    bridge.read_after.assert_awaited_once_with(
        context.resource_scope,
        thread_id,
        cursor=100,
        limit=100,
        run_id=run_id,
        full_state_horizon=7,
    )


@pytest.mark.asyncio
async def test_reconnect_route_computes_horizon_and_applies_it_to_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = uuid.uuid4()
    run_id = uuid.uuid4()
    bridge = SimpleNamespace(
        read_after=AsyncMock(return_value=()),
        latest_full_state_seq=AsyncMock(return_value=42),
    )
    service = SimpleNamespace(
        require_browser_chat_thread=AsyncMock(),
        get=AsyncMock(
            return_value=SimpleNamespace(status="running", error=None),
        ),
    )
    context = SimpleNamespace(
        request_id="reconnect-horizon",
        project_id=uuid.uuid4(),
        resource_scope=object(),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    captured: dict[str, object] = {}

    def consumer(**kwargs: object):
        captured.update(kwargs)

        async def _body():
            yield "event: end\ndata: null\n\n"

        return _body()

    monkeypatch.setattr(
        private_work_router,
        "_private_stream_bridge",
        lambda _request, _request_id: bridge,
    )
    monkeypatch.setattr(
        private_work_router,
        "_run_service",
        lambda _request, _request_id: service,
    )
    monkeypatch.setattr(
        private_work_router,
        "_private_stream_cursor",
        lambda _request, _request_id: 0,
    )
    monkeypatch.setattr(
        private_work_router,
        "_durable_private_sse_consumer",
        consumer,
    )

    response = await private_work_router.reconnect_private_run_stream(
        thread_id,
        run_id,
        request,
        context,
    )
    await response.body_iterator.aclose()

    bridge.latest_full_state_seq.assert_awaited_once_with(
        context.resource_scope,
        str(thread_id),
        run_id=str(run_id),
    )
    bridge.read_after.assert_awaited_once_with(
        context.resource_scope,
        str(thread_id),
        cursor=0,
        limit=100,
        run_id=str(run_id),
        full_state_horizon=42,
    )
    assert captured["full_state_horizon"] == 42
    assert captured["cursor"] == 0
    assert captured["initial_frames"] == ()
    assert captured["cancel_on_disconnect"] is False
