from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from support.m4_private_threads import install_open_project_cutover_guard

from app.gateway.deps import private_work_context
from app.gateway.routers import private_work as private_work_router


@pytest.fixture()
def context() -> SimpleNamespace:
    return SimpleNamespace(request_id="stream-request")


@pytest.fixture()
def app(context: SimpleNamespace) -> FastAPI:
    value = FastAPI()
    install_open_project_cutover_guard(value)
    value.include_router(private_work_router.router)
    value.dependency_overrides[private_work_context] = lambda: context
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


@pytest.mark.asyncio
async def test_stream_run_uses_private_launcher_and_shared_sse_consumer_once(
    app: FastAPI,
    context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    run_id = str(uuid.uuid4())
    bridge = object()
    run_manager = object()
    record = SimpleNamespace(run_id=run_id)
    context.project_id = project_id
    app.state.project_scoped_checkpointer = object()
    app.state.stream_bridge = bridge
    app.state.run_manager = run_manager
    launch_calls: list[tuple[object, str, object, object]] = []
    consumer_calls: list[tuple[object, object, object, object]] = []

    async def launcher(body, selected_thread_id, request, trusted_context):
        launch_calls.append((body, selected_thread_id, request, trusted_context))
        return record

    async def consumer(selected_bridge, selected_record, request, selected_manager):
        consumer_calls.append((selected_bridge, selected_record, request, selected_manager))
        yield "event: end\ndata: null\n\n"

    monkeypatch.setattr(private_work_router, "start_private_run", launcher)
    monkeypatch.setattr(private_work_router, "sse_consumer", consumer)

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
            "checkpoint": {
                "checkpoint_id": "payload-checkpoint",
                "owner_user_id": "payload-owner",
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
    assert response.text == "event: end\ndata: null\n\n"

    assert len(launch_calls) == 1
    body, selected_thread_id, request, trusted_context = launch_calls[0]
    assert selected_thread_id == str(thread_id)
    assert trusted_context is context
    assert body.input == {
        "messages": [{"role": "user", "content": "stream"}],
        "owner_user_id": "forged",
    }
    assert body.command == {"resume": {"role": "tool", "project_id": "payload-project"}}
    assert body.checkpoint == {
        "checkpoint_id": "payload-checkpoint",
        "owner_user_id": "payload-owner",
    }
    assert body.metadata == {"safe": "value"}
    assert body.config == {"context": {"thinking_enabled": True}}
    assert body.context == {"thinking_enabled": False}
    assert consumer_calls == [(bridge, record, request, run_manager)]


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
