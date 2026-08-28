from __future__ import annotations

import asyncio
import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.gateway.deps import private_work_context, require_project_private_open
from app.gateway.routers import private_work as private_work_router
from app.private_work.errors import PrivateWorkNotFound
from deerflow.runtime import DisconnectMode
from deerflow.runtime.events.models import StoredStreamFrame


class _CancellationProbe:
    def __init__(self, result: object) -> None:
        self.result = result
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.exited = asyncio.Event()
        self.cancelled = False

    async def run(self, *_args: object, **_kwargs: object) -> object:
        self.entered.set()
        try:
            await self.release.wait()
            return self.result
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.exited.set()


async def _assert_cancellation_waits_for_probe(
    pending: asyncio.Task[object],
    probe: _CancellationProbe,
) -> None:
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
        if pending.done() or probe.cancelled:
            break

    assert probe.cancelled is False
    assert pending.done() is False
    probe.release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert probe.exited.is_set()


@pytest.fixture()
def context() -> SimpleNamespace:
    return SimpleNamespace(request_id="stream-request")


@pytest.fixture()
def app(context: SimpleNamespace) -> FastAPI:
    value = FastAPI()
    value.include_router(private_work_router.router)
    value.dependency_overrides[private_work_context] = lambda: context
    value.dependency_overrides[require_project_private_open] = lambda: None
    return value


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    **kwargs: object,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


def _stream_path(project_id: uuid.UUID, thread_id: uuid.UUID) -> str:
    return f"/api/projects/{project_id}/private-work/threads/{thread_id}/runs/stream"


def test_private_work_router_exposes_project_run_stream() -> None:
    routes = {(route.path, method) for route in private_work_router.router.routes for method in route.methods or ()}

    assert (
        "/api/projects/{project_id}/private-work/threads/{thread_id}/runs/stream",
        "POST",
    ) in routes
    assert (
        "/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}/stream",
        "GET",
    ) in routes
    assert (
        "/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}/workspace-changes",
        "GET",
    ) in routes


@pytest.mark.asyncio
async def test_workspace_changes_route_scopes_run_and_event_lookup(
    app: FastAPI,
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    run_id = uuid.uuid4()
    resource_scope = object()
    context.resource_scope = resource_scope
    service = SimpleNamespace(get=AsyncMock(return_value=object()))

    class _EventStore:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        async def list_events(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
            self.calls.append((*args, kwargs))
            return [
                {
                    "metadata": {
                        "workspace_changes": {
                            "version": 1,
                            "summary": {"created": 1},
                            "files": [{"path": "report.md", "diff": "+done"}],
                        }
                    }
                }
            ]

    event_store = _EventStore()
    browser_service = AsyncMock(return_value=service)
    monkeypatch.setattr(
        private_work_router,
        "_browser_chat_run_service",
        browser_service,
    )
    monkeypatch.setattr(
        private_work_router,
        "_run_event_store",
        lambda _request, _request_id: event_store,
    )

    response = await _request(
        app,
        "GET",
        (f"/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}/workspace-changes?include_files=true&include_diff=false"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "version": 1,
        "summary": {"created": 1},
        "files": [{"path": "report.md", "diff": ""}],
    }
    browser_service.assert_awaited_once()
    service.get.assert_awaited_once_with(
        context,
        str(thread_id),
        str(run_id),
    )
    assert event_store.calls == [
        (
            str(thread_id),
            str(run_id),
            {
                "event_types": ["workspace_changes"],
                "limit": 10,
                "scope": resource_scope,
            },
        )
    ]


@pytest.mark.asyncio
async def test_workspace_changes_route_does_not_query_events_for_hidden_run(
    app: FastAPI,
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    run_id = uuid.uuid4()
    context.resource_scope = object()
    service = SimpleNamespace(
        get=AsyncMock(side_effect=PrivateWorkNotFound(context.request_id)),
    )
    event_store = SimpleNamespace(list_events=AsyncMock())
    monkeypatch.setattr(
        private_work_router,
        "_browser_chat_run_service",
        AsyncMock(return_value=service),
    )
    monkeypatch.setattr(
        private_work_router,
        "_run_event_store",
        lambda _request, _request_id: event_store,
    )

    response = await _request(
        app,
        "GET",
        f"/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}/workspace-changes",
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_NOT_FOUND"
    event_store.list_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_run_uses_private_launcher_and_durable_sse_consumer_once(
    app: FastAPI,
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    run_id = str(uuid.uuid4())
    bridge = object()
    service = SimpleNamespace(require_browser_chat_thread=AsyncMock())
    record = SimpleNamespace(
        run_id=run_id,
        on_disconnect=DisconnectMode.cancel,
    )
    context.project_id = project_id
    app.state.project_scoped_checkpointer = object()
    launch_calls: list[tuple[object, str, object, object]] = []
    consumer_calls: list[dict[str, object]] = []

    async def launcher(
        body,
        selected_thread_id,
        request,
        trusted_context,
        *,
        context_rebase_reason,
    ):
        assert context_rebase_reason is None
        launch_calls.append((body, selected_thread_id, request, trusted_context))
        return record

    async def consumer(**kwargs):
        consumer_calls.append(kwargs)
        yield "event: end\ndata: null\n\n"

    monkeypatch.setattr(private_work_router, "start_private_run", launcher)
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
    read_page = AsyncMock(return_value=())
    monkeypatch.setattr(private_work_router, "_read_private_stream_page", read_page)
    monkeypatch.setattr(private_work_router, "_durable_private_sse_consumer", consumer)

    response = await _request(
        app,
        "POST",
        _stream_path(project_id, thread_id),
        json={
            "input": {
                "messages": [{"role": "user", "content": "stream"}],
                "owner_user_id": "forged",
            },
            "command": {
                "resume": {
                    "role": "tool",
                    "project_id": "payload-project",
                }
            },
            "metadata": {"safe": "value", "project_id": "forged"},
            "config": {
                "context": {
                    "membership_id": "forged",
                    "thinking_enabled": True,
                }
            },
            "context": {"user_id": "forged", "thinking_enabled": False},
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["content-location"] == (f"/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}")
    assert response.headers["location"] == (f"/threads/{thread_id}/runs/{run_id}/stream")
    assert response.text == "event: end\ndata: null\n\n"

    assert len(launch_calls) == 1
    body, selected_thread_id, request, trusted_context = launch_calls[0]
    assert selected_thread_id == str(thread_id)
    assert trusted_context is context
    assert body.input == {
        "messages": [{"role": "user", "content": "stream"}],
    }
    assert body.command == {"resume": {"role": "tool", "project_id": "payload-project"}}
    assert body.metadata == {"safe": "value"}
    assert body.config == {"context": {}}
    assert body.context == {}
    assert body.execution_profile.model_dump() == {
        "model_name": None,
        "thinking_enabled": None,
        "reasoning_effort": None,
    }
    service.require_browser_chat_thread.assert_awaited_once_with(
        context,
        str(thread_id),
    )
    read_page.assert_not_awaited()
    assert len(consumer_calls) == 1
    assert consumer_calls[0] == {
        "bridge": bridge,
        "service": service,
        "context": context,
        "thread_id": str(thread_id),
        "run_id": run_id,
        "request": request,
        "cursor": 0,
        "initial_frames": (),
        "cancel_on_disconnect": True,
        "wakeup": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path_suffix", "body"),
    [
        ("not-a-uuid/runs/stream", {"input": {}}),
        (f"{uuid.uuid4()}/runs/stream", {"input": {}, "unexpected": True}),
    ],
)
async def test_stream_run_rejects_invalid_uuid_and_body_with_private_422(
    app: FastAPI,
    path_suffix: str,
    body: dict[str, object],
) -> None:
    project_id = uuid.uuid4()

    response = await _request(
        app,
        "POST",
        f"/api/projects/{project_id}/private-work/threads/{path_suffix}",
        json=body,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"


@pytest.mark.asyncio
async def test_stream_run_missing_runtime_is_private_503(app: FastAPI) -> None:
    response = await _request(
        app,
        "POST",
        _stream_path(uuid.uuid4(), uuid.uuid4()),
        json={"input": {}},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_UNAVAILABLE"
    assert response.headers["retry-after"] == "1"


@pytest.mark.asyncio
async def test_durable_consumer_drains_next_page_before_terminal_fallback() -> None:
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
    context = SimpleNamespace(request_id="page", resource_scope=object())
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
        )
    ]

    assert len(chunks) == 101
    assert chunks[-1].startswith("id: 101\n")
    bridge.read_after.assert_awaited_once_with(
        context.resource_scope,
        thread_id,
        cursor=100,
        limit=100,
        run_id=run_id,
        full_state_horizon=None,
    )
    service.get.assert_not_awaited()
    bridge.ensure_settled_terminal.assert_not_awaited()


@pytest.mark.asyncio
async def test_durable_consumer_preserves_model_output_limit_terminal_code() -> None:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    terminal = StoredStreamFrame(
        id="1",
        thread_id=thread_id,
        run_id=run_id,
        event="end",
        data={"status": "error", "error_code": "MODEL_OUTPUT_LIMIT"},
        terminal=True,
    )
    bridge = SimpleNamespace(
        read_after=AsyncMock(return_value=()),
        ensure_settled_terminal=AsyncMock(return_value=terminal),
    )
    service = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                status="error",
                error="MODEL_OUTPUT_LIMIT",
            ),
        ),
    )
    context = SimpleNamespace(request_id="output-limit", resource_scope=object())
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
            initial_frames=(),
            cancel_on_disconnect=False,
        )
    ]

    assert len(chunks) == 1
    assert '"error_code":"MODEL_OUTPUT_LIMIT"' in chunks[0]
    bridge.ensure_settled_terminal.assert_awaited_once_with(
        context.resource_scope,
        thread_id,
        run_id,
        status="error",
        error_code="MODEL_OUTPUT_LIMIT",
    )


@pytest.mark.asyncio
async def test_durable_consumer_preserves_first_persisted_terminal() -> None:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    provisional = StoredStreamFrame(
        id="7",
        thread_id=thread_id,
        run_id=run_id,
        event="end",
        data={"status": "success"},
        terminal=True,
    )
    bridge = SimpleNamespace(
        read_after=AsyncMock(),
        ensure_settled_terminal=AsyncMock(),
    )
    service = SimpleNamespace(
        get=AsyncMock(
            side_effect=(
                SimpleNamespace(status="running", error=None),
                SimpleNamespace(status="interrupted", error=None),
            ),
        ),
    )
    context = SimpleNamespace(request_id="terminal-race", resource_scope=object())
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
            cursor=6,
            initial_frames=(provisional,),
            cancel_on_disconnect=False,
        )
    ]

    assert len(chunks) == 1
    assert 'data: {"status":"success"}' in chunks[0]
    bridge.read_after.assert_not_awaited()
    service.get.assert_not_awaited()
    bridge.ensure_settled_terminal.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [7, 9])
async def test_durable_consumer_does_not_reemit_terminal_at_or_before_cursor(
    cursor: int,
) -> None:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    terminal = StoredStreamFrame(
        id="7",
        thread_id=thread_id,
        run_id=run_id,
        event="end",
        data={"status": "completed"},
        terminal=True,
        created=False,
    )
    bridge = SimpleNamespace(
        read_after=AsyncMock(return_value=()),
        ensure_settled_terminal=AsyncMock(return_value=terminal),
    )
    service = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(status="success", error=None),
        ),
    )
    context = SimpleNamespace(request_id="terminal-cursor", resource_scope=object())
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
            cursor=cursor,
            initial_frames=(),
            cancel_on_disconnect=False,
        )
    ]

    assert chunks == []
    bridge.ensure_settled_terminal.assert_awaited_once_with(
        context.resource_scope,
        thread_id,
        run_id,
        status="completed",
        error_code=None,
    )


@pytest.mark.asyncio
async def test_durable_consumer_emits_heartbeat_while_run_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    bridge = SimpleNamespace(read_after=AsyncMock(return_value=()))
    service = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status="running")),
    )
    context = SimpleNamespace(request_id="heartbeat", resource_scope=object())
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    monkeypatch.setattr(private_work_router, "_PRIVATE_STREAM_HEARTBEAT_SECONDS", 0)
    stream = private_work_router._durable_private_sse_consumer(
        bridge=bridge,
        service=service,
        context=context,
        thread_id=thread_id,
        run_id=run_id,
        request=request,
        cursor=0,
        initial_frames=(),
        cancel_on_disconnect=False,
    )

    assert await anext(stream) == ": heartbeat\n\n"
    await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read_after", "get", "ensure_terminal"])
async def test_durable_consumer_waits_for_database_owner_before_propagating_cancellation(
    operation: str,
) -> None:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    terminal = StoredStreamFrame(
        id="1",
        thread_id=thread_id,
        run_id=run_id,
        event="end",
        data={"status": "completed"},
        terminal=True,
    )
    probe = _CancellationProbe(terminal if operation == "ensure_terminal" else ())
    bridge = SimpleNamespace(
        read_after=(probe.run if operation == "read_after" else AsyncMock(return_value=())),
        ensure_settled_terminal=(probe.run if operation == "ensure_terminal" else AsyncMock(return_value=terminal)),
    )
    service = SimpleNamespace(
        get=(
            probe.run
            if operation == "get"
            else AsyncMock(
                return_value=SimpleNamespace(
                    status=("success" if operation == "ensure_terminal" else "running"),
                    error=None,
                )
            )
        ),
    )
    context = SimpleNamespace(request_id="cancel-db", resource_scope=object())
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    stream = private_work_router._durable_private_sse_consumer(
        bridge=bridge,
        service=service,
        context=context,
        thread_id=thread_id,
        run_id=run_id,
        request=request,
        cursor=0,
        initial_frames=(),
        cancel_on_disconnect=False,
    )
    pending = asyncio.create_task(anext(stream))
    await probe.entered.wait()

    await _assert_cancellation_waits_for_probe(pending, probe)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "read_after"])
async def test_reconnect_waits_for_initial_database_owner_before_propagating_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    thread_id = uuid.uuid4()
    run_id = uuid.uuid4()
    probe = _CancellationProbe(SimpleNamespace(status="running", error=None) if operation == "get" else ())
    bridge = SimpleNamespace(
        read_after=(probe.run if operation == "read_after" else AsyncMock(return_value=())),
        latest_full_state_seq=AsyncMock(return_value=0),
    )
    service = SimpleNamespace(
        require_browser_chat_thread=AsyncMock(),
        get=(probe.run if operation == "get" else AsyncMock(return_value=SimpleNamespace(status="running", error=None))),
    )
    context = SimpleNamespace(
        request_id="reconnect-cancel-db",
        project_id=uuid.uuid4(),
        resource_scope=object(),
    )
    request = SimpleNamespace()
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
    pending = asyncio.create_task(
        private_work_router.reconnect_private_run_stream(
            thread_id,
            run_id,
            request,
            context,
        )
    )
    await probe.entered.wait()

    await _assert_cancellation_waits_for_probe(pending, probe)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_durable_consumer_cancellation_returns_real_postgres_connection_to_pool(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    probe = _CancellationProbe(())

    async def database_read(*_args: object, **_kwargs: object) -> object:
        async with factory() as session:
            await session.execute(sa.text("SELECT 1"))
            return await probe.run()

    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    bridge = SimpleNamespace(read_after=database_read)
    service = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status="running")),
    )
    context = SimpleNamespace(request_id="cancel-real-db", resource_scope=object())
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    stream = private_work_router._durable_private_sse_consumer(
        bridge=bridge,
        service=service,
        context=context,
        thread_id=thread_id,
        run_id=run_id,
        request=request,
        cursor=0,
        initial_frames=(),
        cancel_on_disconnect=False,
    )
    pending = asyncio.create_task(anext(stream))
    try:
        baseline = engine.pool.checkedout()
        await probe.entered.wait()
        assert engine.pool.checkedout() == baseline + 1

        await _assert_cancellation_waits_for_probe(pending, probe)

        assert engine.pool.checkedout() == baseline
    finally:
        probe.release.set()
        if not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        await engine.dispose()


@pytest.mark.asyncio
async def test_durable_consumer_persists_cancel_when_stream_task_is_cancelled() -> None:
    probe = _CancellationProbe(())

    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    bridge = SimpleNamespace(read_after=probe.run)
    service = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status="running")),
        cancel=AsyncMock(),
    )
    context = SimpleNamespace(request_id="cancel", resource_scope=object())
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    stream = private_work_router._durable_private_sse_consumer(
        bridge=bridge,
        service=service,
        context=context,
        thread_id=thread_id,
        run_id=run_id,
        request=request,
        cursor=0,
        initial_frames=(),
        cancel_on_disconnect=True,
    )
    pending = asyncio.create_task(anext(stream))
    await probe.entered.wait()

    await _assert_cancellation_waits_for_probe(pending, probe)

    service.cancel.assert_awaited_once_with(
        context,
        thread_id,
        run_id,
        reason="client_disconnected",
    )


@pytest.mark.asyncio
async def test_durable_wait_persists_cancel_when_request_task_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    async def running(*_args, **_kwargs):
        entered.set()
        return SimpleNamespace(status="running", error=None)

    service = SimpleNamespace(get=running, cancel=AsyncMock())
    context = SimpleNamespace(request_id="wait-cancel")
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    monkeypatch.setattr(private_work_router, "_PRIVATE_STREAM_POLL_SECONDS", 60)
    pending = asyncio.create_task(
        private_work_router._wait_for_durable_private_run(
            service=service,
            context=context,
            thread_id="thread-1",
            run_id="run-1",
            request=request,
            cancel_on_disconnect=True,
        )
    )
    await entered.wait()

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    service.cancel.assert_awaited_once_with(
        context,
        "thread-1",
        "run-1",
        reason="client_disconnected",
    )
