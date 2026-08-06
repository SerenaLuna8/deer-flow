from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from starlette.responses import StreamingResponse

from app.final_schema import FinalSchemaProbe
from app.gateway import deps
from app.gateway.deps import (
    get_current_user_from_request,
    private_work_context,
    project_session,
    require_project_automation_open,
    require_project_private_open,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole


class _TrackedTransaction:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> None:
        self._events.append("transaction-enter")

    async def __aexit__(self, *_args: object) -> None:
        self._events.append("transaction-exit")


class _TrackedSession:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def begin(self) -> _TrackedTransaction:
        return _TrackedTransaction(self._events)


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["private", "automation"])
async def test_project_schema_gate_releases_its_autobegin_transaction(
    dependency: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    session = _TrackedSession(events)
    context = SimpleNamespace(request_id="request-1")

    async def require_ready(
        _probe: FinalSchemaProbe,
        selected_session: object,
    ) -> None:
        assert selected_session is session
        events.append("probe")

    monkeypatch.setattr(FinalSchemaProbe, "require_ready", require_ready)

    if dependency == "private":
        request = SimpleNamespace(
            url=SimpleNamespace(path="/api/projects/project-1/private-work/threads"),
        )
        await require_project_private_open(request, context, session)  # type: ignore[arg-type]
    else:
        await require_project_automation_open(context, session)  # type: ignore[arg-type]

    assert events == ["transaction-enter", "probe", "transaction-exit"]


@pytest.mark.asyncio
async def test_private_project_session_closes_before_streaming_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    project = ProjectContext(
        user_id=user_id,
        project_id=project_id,
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="request-1",
    )

    async def tracked_project_session() -> AsyncIterator[object]:
        events.append("session-open")
        try:
            yield object()
        finally:
            events.append("session-close")

    async def resolve_project_context(*_args: object, **_kwargs: object) -> ProjectContext:
        events.append("context-resolved")
        return project

    app = FastAPI()
    app.dependency_overrides[project_session] = tracked_project_session
    app.dependency_overrides[get_current_user_from_request] = lambda: SimpleNamespace(
        id=user_id,
    )
    monkeypatch.setattr(deps, "resolve_project_context", resolve_project_context)

    @app.get("/api/projects/{project_id}/stream")
    async def stream(
        _context=Depends(private_work_context),
    ) -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            events.append("stream-body")
            yield b"ok"

        return StreamingResponse(body())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/api/projects/{project_id}/stream")

    assert response.content == b"ok"
    assert events == [
        "session-open",
        "context-resolved",
        "session-close",
        "stream-body",
    ]
