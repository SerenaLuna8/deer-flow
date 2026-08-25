"""LISTEN/NOTIFY wakeup for durable private SSE consumers.

NOTIFY is only an alarm clock: the writer queues ``pg_notify('run_events',
run_id)`` inside the appending transaction, one Gateway listener connection
fans the payload out to in-process waiters, and every failure mode leaves the
consumer on its poll-timeout fallback. Cursor semantics never change.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.gateway.deps import _install_run_event_wakeup
from app.gateway.routers import private_work as private_work_router
from app.gateway.run_event_wakeup import RunEventWakeup
from deerflow.config.worker_config import WorkerStreamConfig
from deerflow.runtime.events.models import StoredStreamFrame
from deerflow.runtime.events.store.db import (
    RUN_EVENTS_NOTIFY_CHANNEL,
    DbRunEventStore,
)
from deerflow.runtime.events.stream import PostgresStreamBridge


class _FakeConnection:
    def __init__(self) -> None:
        self.listeners: list[tuple[str, object]] = []
        self.closed = False
        self.terminated = False

    async def add_listener(self, channel: str, callback) -> None:
        self.listeners.append((channel, callback))

    def is_closed(self) -> bool:
        return self.closed

    def terminate(self) -> None:
        self.terminated = True
        self.closed = True


class _FakeConnector:
    def __init__(self, *, failures: int = 0) -> None:
        self.connections: list[_FakeConnection] = []
        self.failures = failures
        self.attempts = 0

    async def __call__(self, dsn: str) -> _FakeConnection:
        self.attempts += 1
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("listener connect refused")
        connection = _FakeConnection()
        self.connections.append(connection)
        return connection

    def fire(self, payload: str, *, channel: str = RUN_EVENTS_NOTIFY_CHANNEL) -> None:
        connection = self.connections[-1]
        for listen_channel, callback in connection.listeners:
            if listen_channel == RUN_EVENTS_NOTIFY_CHANNEL:
                callback(connection, 1, channel, payload)


def _wakeup(connector: _FakeConnector) -> RunEventWakeup:
    return RunEventWakeup(
        "postgresql://unused/db",
        connect=connector,
        reconnect_backoff_seconds=0.01,
        probe_seconds=0.01,
    )


async def _until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition was not reached in time"
        await asyncio.sleep(0.005)


def test_run_event_notify_switch_defaults_on_and_accepts_off() -> None:
    assert WorkerStreamConfig().run_event_notify_enabled is True
    assert WorkerStreamConfig(run_event_notify_enabled=False).run_event_notify_enabled is False


@pytest.mark.asyncio
async def test_disabled_event_store_does_not_queue_pg_notify() -> None:
    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"),
        ),
        execute=AsyncMock(),
    )

    store = DbRunEventStore(
        object(),
        run_event_notify_enabled=False,
    )
    await store._notify_stream_append(session, "run-a")

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_event_store_still_queues_pg_notify() -> None:
    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"),
        ),
        execute=AsyncMock(),
    )

    store = DbRunEventStore(object())
    await store._notify_stream_append(session, "run-a")

    session.execute.assert_awaited_once()
    _statement, parameters = session.execute.await_args.args
    assert parameters == {
        "channel": RUN_EVENTS_NOTIFY_CHANNEL,
        "payload": "run-a",
    }


def test_stream_bridge_forwards_disabled_notify_policy_to_writer() -> None:
    bridge = PostgresStreamBridge(
        object(),
        run_event_notify_enabled=False,
    )

    assert bridge._events._run_event_notify_enabled is False


@pytest.mark.asyncio
async def test_gateway_does_not_start_listener_when_notify_is_disabled() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    stack = SimpleNamespace(push_async_callback=Mock())
    wakeup_factory = Mock(side_effect=AssertionError("listener must stay off"))

    wakeup = await _install_run_event_wakeup(
        app,
        stack,
        dsn="postgresql://unused/db",
        enabled=False,
        wakeup_factory=wakeup_factory,
    )

    assert wakeup is None
    assert app.state.run_event_wakeup is None
    request = SimpleNamespace(app=app)
    assert private_work_router._run_event_wakeup(request) is None
    wakeup_factory.assert_not_called()
    stack.push_async_callback.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_installs_listener_when_notify_is_enabled() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    stack = SimpleNamespace(push_async_callback=Mock())
    installed = SimpleNamespace(
        start=AsyncMock(),
        aclose=AsyncMock(),
    )
    wakeup_factory = Mock(return_value=installed)

    wakeup = await _install_run_event_wakeup(
        app,
        stack,
        dsn="postgresql://unused/db",
        enabled=True,
        wakeup_factory=wakeup_factory,
    )

    assert wakeup is installed
    assert app.state.run_event_wakeup is installed
    wakeup_factory.assert_called_once_with("postgresql://unused/db")
    installed.start.assert_awaited_once_with()
    stack.push_async_callback.assert_called_once_with(installed.aclose)


@pytest.mark.asyncio
async def test_notification_wakes_only_the_matching_run_waiters() -> None:
    connector = _FakeConnector()
    wakeup = _wakeup(connector)
    await wakeup.start()
    try:
        await _until(lambda: wakeup.listening)
        waiter_a = wakeup.subscribe("run-a")
        waiter_b = wakeup.subscribe("run-b")

        connector.fire("run-a")

        assert waiter_a.is_set()
        assert not waiter_b.is_set()
    finally:
        await wakeup.aclose()


@pytest.mark.asyncio
async def test_foreign_channel_and_empty_payload_wake_nobody() -> None:
    connector = _FakeConnector()
    wakeup = _wakeup(connector)
    await wakeup.start()
    try:
        await _until(lambda: wakeup.listening)
        waiter = wakeup.subscribe("run-a")

        connector.fire("run-a", channel="other_channel")
        connector.fire("")

        assert not waiter.is_set()
    finally:
        await wakeup.aclose()


@pytest.mark.asyncio
async def test_connection_loss_wakes_waiters_and_reconnects() -> None:
    connector = _FakeConnector()
    wakeup = _wakeup(connector)
    await wakeup.start()
    try:
        await _until(lambda: wakeup.listening)
        waiter = wakeup.subscribe("run-a")
        assert not waiter.is_set()

        connector.connections[0].closed = True

        # Degradation wakes every parked waiter so it re-reads promptly, and a
        # fresh listener connection resumes event-driven waiting afterwards.
        await _until(lambda: waiter.is_set())
        await _until(lambda: connector.attempts >= 2 and wakeup.listening)
        assert connector.connections[0].terminated

        waiter.clear()
        connector.fire("run-a")
        assert waiter.is_set()
    finally:
        await wakeup.aclose()


@pytest.mark.asyncio
async def test_connect_failures_back_off_and_eventually_listen() -> None:
    connector = _FakeConnector(failures=2)
    wakeup = _wakeup(connector)
    await wakeup.start()
    try:
        await _until(lambda: wakeup.listening)
        assert connector.attempts == 3
    finally:
        await wakeup.aclose()


@pytest.mark.asyncio
async def test_real_postgres_listener_termination_wakes_and_reconnects(
    postgres_database_url: str,
) -> None:
    """Kill the actual LISTEN backend, then prove poll fallback and recovery."""
    import asyncpg

    dsn = str(postgres_database_url).replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )
    listener_connections: list[asyncpg.Connection] = []

    async def connect_listener(url: str) -> asyncpg.Connection:
        connection = await asyncpg.connect(url)
        listener_connections.append(connection)
        return connection

    wakeup = RunEventWakeup(
        dsn,
        connect=connect_listener,
        reconnect_backoff_seconds=0.01,
        probe_seconds=0.01,
    )
    control = await asyncpg.connect(dsn)
    await wakeup.start()
    try:
        await _until(lambda: wakeup.listening and len(listener_connections) == 1)
        waiter = wakeup.subscribe("run-real-reconnect")
        assert not waiter.is_set()

        terminated = await control.fetchval(
            "SELECT pg_terminate_backend($1)",
            listener_connections[0].get_server_pid(),
        )
        assert terminated is True

        # Losing LISTEN wakes every parked consumer so it can immediately
        # re-read by cursor instead of waiting out the healthy-listener delay.
        await _until(waiter.is_set)
        await _until(lambda: wakeup.listening and len(listener_connections) >= 2)

        waiter.clear()
        await control.execute(
            "SELECT pg_notify($1, $2)",
            RUN_EVENTS_NOTIFY_CHANNEL,
            "run-real-reconnect",
        )
        await _until(waiter.is_set)
    finally:
        await wakeup.aclose()
        await control.close()


@pytest.mark.asyncio
async def test_aclose_terminates_the_connection_and_wakes_waiters() -> None:
    connector = _FakeConnector()
    wakeup = _wakeup(connector)
    await wakeup.start()
    await _until(lambda: wakeup.listening)
    waiter = wakeup.subscribe("run-a")

    await wakeup.aclose()

    assert waiter.is_set()
    assert not wakeup.listening
    assert connector.connections[0].terminated
    await wakeup.aclose()
    await wakeup.start()
    assert not wakeup.listening


@pytest.mark.asyncio
async def test_unsubscribe_prunes_the_dispatch_table() -> None:
    wakeup = RunEventWakeup("postgresql://unused/db", connect=_FakeConnector())
    waiter = wakeup.subscribe("run-a")
    other = wakeup.subscribe("run-a")

    wakeup.unsubscribe("run-a", waiter)
    assert wakeup._waiters == {"run-a": {other}}
    wakeup.unsubscribe("run-a", other)
    assert wakeup._waiters == {}
    wakeup.unsubscribe("run-a", other)


def _terminal_frame(thread_id: str, run_id: str) -> StoredStreamFrame:
    return StoredStreamFrame(
        id="7",
        thread_id=thread_id,
        run_id=run_id,
        event="end",
        data={"status": "completed"},
        terminal=True,
        created=False,
    )


async def _collect_consumer(
    *,
    bridge,
    service,
    run_id: str,
    thread_id: str,
    wakeup: RunEventWakeup | None,
) -> list[str]:
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    context = SimpleNamespace(request_id="wakeup", resource_scope=object())
    return [
        chunk
        async for chunk in private_work_router._durable_private_sse_consumer(
            bridge=bridge,
            service=service,
            context=context,
            thread_id=thread_id,
            run_id=run_id,
            request=request,
            cursor=0,
            initial_frames=(),
            cancel_on_disconnect=False,
            wakeup=wakeup,
        )
    ]


@pytest.mark.asyncio
async def test_idle_consumer_wakes_on_notify_instead_of_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    # A lost wakeup would stall the consumer for the full idle wait, so a fast
    # finish proves the NOTIFY path (not the timeout) drove the read.
    monkeypatch.setattr(
        private_work_router,
        "_PRIVATE_STREAM_WAKEUP_WAIT_SECONDS",
        30.0,
    )
    wakeup = RunEventWakeup("postgresql://unused/db", connect=_FakeConnector())
    wakeup._listening = True

    terminal = _terminal_frame(thread_id, run_id)
    first_read_done = asyncio.Event()

    async def read_after(*_args, **_kwargs):
        if not first_read_done.is_set():
            first_read_done.set()
            return ()
        return (terminal,)

    bridge = SimpleNamespace(
        read_after=read_after,
        ensure_settled_terminal=AsyncMock(return_value=terminal),
    )
    service = SimpleNamespace(
        get=AsyncMock(
            side_effect=(
                SimpleNamespace(status="running", error=None),
                SimpleNamespace(status="success", error=None),
            ),
        ),
    )

    started = time.monotonic()
    consumer = asyncio.create_task(
        _collect_consumer(
            bridge=bridge,
            service=service,
            run_id=run_id,
            thread_id=thread_id,
            wakeup=wakeup,
        )
    )
    await first_read_done.wait()
    wakeup._on_notification(None, 1, RUN_EVENTS_NOTIFY_CHANNEL, run_id)

    chunks = await asyncio.wait_for(consumer, timeout=10)
    elapsed = time.monotonic() - started

    assert any('"status":"completed"' in chunk for chunk in chunks)
    assert elapsed < 5, "the notify wakeup must beat the 30s idle timeout"
    assert wakeup._waiters == {}


@pytest.mark.asyncio
async def test_healthy_listener_parks_an_idle_stream_without_repeated_db_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quantify the healthy LISTEN path instead of merely checking latency.

    Over the same interval the legacy 10ms fallback would issue several
    ``read_after``/Run-status queries. A healthy listener performs the initial
    cursor read once and then parks without another database round trip.
    """

    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    monkeypatch.setattr(
        private_work_router,
        "_PRIVATE_STREAM_WAKEUP_WAIT_SECONDS",
        30.0,
    )
    monkeypatch.setattr(private_work_router, "_PRIVATE_STREAM_POLL_SECONDS", 0.01)
    wakeup = RunEventWakeup("postgresql://unused/db", connect=_FakeConnector())
    wakeup._listening = True
    first_read = asyncio.Event()

    async def read_after(*_args, **_kwargs):
        first_read.set()
        return ()

    bridge = SimpleNamespace(
        read_after=AsyncMock(side_effect=read_after),
        ensure_settled_terminal=AsyncMock(),
    )
    service = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status="running", error=None)),
    )
    consumer = asyncio.create_task(
        _collect_consumer(
            bridge=bridge,
            service=service,
            run_id=run_id,
            thread_id=thread_id,
            wakeup=wakeup,
        )
    )
    await asyncio.wait_for(first_read.wait(), timeout=1)
    await asyncio.sleep(0.08)

    assert bridge.read_after.await_count == 1
    assert service.get.await_count == 1

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert wakeup._waiters == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("notify_enabled", (True, False))
async def test_append_stream_frame_notifies_only_on_commit_when_enabled(
    migrated_postgres_database_url: str,
    notify_enabled: bool,
) -> None:
    import asyncpg
    from support.private_thread_seed import seed_private_thread_database
    from support.run_closure import add_sealed_test_run

    from deerflow.persistence.run.model import RunRow
    from deerflow.persistence.thread_meta.model import ThreadMetaRow
    from deerflow.runtime.events.models import StreamFrame
    from deerflow.runtime.events.store.db import DbRunEventStore

    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        scope = seed.owner_a.resource_scope
        thread_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        async with seed.factory() as session, session.begin():
            session.add(
                ThreadMetaRow(
                    thread_id=thread_id,
                    assistant_id=str(seed.project_agent_id),
                    owner_user_id=scope.owner_user_id,
                    display_name="Notify wakeup",
                    status="idle",
                    metadata_json={},
                    project_id=uuid.UUID(scope.project_id),
                    agent_asset_id=seed.project_agent_id,
                    agent_scope="project",
                )
            )
            await session.flush()
            await add_sealed_test_run(
                session,
                RunRow(
                    run_id=run_id,
                    thread_id=thread_id,
                    assistant_id=str(seed.project_agent_id),
                    owner_user_id=scope.owner_user_id,
                    status="running",
                    model_name="test-model",
                    multitask_strategy="reject",
                    metadata_json={},
                    kwargs_json={},
                    origin_trace_id="c" * 32,
                    project_id=uuid.UUID(scope.project_id),
                ),
            )

        store = DbRunEventStore(
            seed.factory,
            run_event_notify_enabled=notify_enabled,
        )
        dsn = str(migrated_postgres_database_url).replace(
            "postgresql+asyncpg://",
            "postgresql://",
            1,
        )
        received: list[str] = []
        delivered = asyncio.Event()

        def _on_notify(_connection, _pid, _channel, payload) -> None:
            received.append(payload)
            delivered.set()

        listener = await asyncpg.connect(dsn)
        try:
            await listener.add_listener(RUN_EVENTS_NOTIFY_CHANNEL, _on_notify)
            async with seed.factory() as session:
                async with session.begin():
                    await store.append_stream_frame(
                        session,
                        scope=scope,
                        thread_id=thread_id,
                        run_id=run_id,
                        frame=StreamFrame(event="updates", data={"index": 1}),
                    )
                    await asyncio.sleep(0.2)
                    assert received == [], "NOTIFY must ride the commit, not the write"
            if notify_enabled:
                await asyncio.wait_for(delivered.wait(), timeout=5)
                assert received == [run_id]
            else:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(delivered.wait(), timeout=0.25)
                assert received == []
        finally:
            await listener.close()
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_degraded_listener_falls_back_to_the_poll_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    monkeypatch.setattr(
        private_work_router,
        "_PRIVATE_STREAM_WAKEUP_WAIT_SECONDS",
        30.0,
    )
    monkeypatch.setattr(private_work_router, "_PRIVATE_STREAM_POLL_SECONDS", 0.01)
    wakeup = RunEventWakeup("postgresql://unused/db", connect=_FakeConnector())
    assert not wakeup.listening

    terminal = _terminal_frame(thread_id, run_id)
    bridge = SimpleNamespace(
        read_after=AsyncMock(side_effect=((), (terminal,))),
        ensure_settled_terminal=AsyncMock(return_value=terminal),
    )
    service = SimpleNamespace(
        get=AsyncMock(
            side_effect=(
                SimpleNamespace(status="running", error=None),
                SimpleNamespace(status="success", error=None),
            ),
        ),
    )

    chunks = await asyncio.wait_for(
        _collect_consumer(
            bridge=bridge,
            service=service,
            run_id=run_id,
            thread_id=thread_id,
            wakeup=wakeup,
        ),
        timeout=10,
    )

    assert any('"status":"completed"' in chunk for chunk in chunks)
    assert wakeup._waiters == {}
