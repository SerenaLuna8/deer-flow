from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.deps import (
    get_current_agent_runtime_config,
    private_work_context,
    require_project_private_open,
)
from app.gateway.routers import private_work as private_work_router
from deerflow.runtime.context_compaction import ThreadCompactionResult


class _CompactService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def compact(self, context, thread_id: str, **kwargs):
        self.calls.append(
            {
                "context": context,
                "thread_id": thread_id,
                **kwargs,
            }
        )
        return ThreadCompactionResult(
            thread_id=thread_id,
            compacted=False,
            reason="compaction_failed",
        )


@pytest.fixture()
def compact_app(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FastAPI, _CompactService, object]:
    app = FastAPI()
    app.include_router(private_work_router.router)
    context = SimpleNamespace(request_id="compact-api")
    runtime_config = object()
    service = _CompactService()
    app.dependency_overrides[private_work_context] = lambda: context
    app.dependency_overrides[require_project_private_open] = lambda: None
    app.dependency_overrides[get_current_agent_runtime_config] = lambda: runtime_config
    monkeypatch.setattr(
        private_work_router,
        "_chat_control_service",
        lambda _request, _request_id: service,
    )
    return app, service, runtime_config


async def _compact_request(
    app: FastAPI,
    *,
    keep_type: str,
    value: int | float,
) -> httpx.Response:
    project_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.post(
            f"/api/projects/{project_id}/private-work/threads/{thread_id}/compact",
            json={"keep": {"type": keep_type, "value": value}},
        )


@pytest.mark.asyncio
async def test_compact_api_accepts_messages_zero_and_preserves_failure_reason(
    compact_app: tuple[FastAPI, _CompactService, object],
) -> None:
    app, service, runtime_config = compact_app

    response = await _compact_request(
        app,
        keep_type="messages",
        value=0,
    )

    assert response.status_code == 200
    assert response.json()["compacted"] is False
    assert response.json()["reason"] == "compaction_failed"
    assert len(service.calls) == 1
    assert service.calls[0]["keep"] == ("messages", 0)
    assert service.calls[0]["force"] is True
    assert service.calls[0]["app_config"] is runtime_config


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_type", ["fraction", "tokens"])
async def test_compact_api_rejects_zero_for_non_message_keep_modes(
    compact_app: tuple[FastAPI, _CompactService, object],
    keep_type: str,
) -> None:
    app, service, _runtime_config = compact_app

    response = await _compact_request(
        app,
        keep_type=keep_type,
        value=0,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"
    assert service.calls == []
