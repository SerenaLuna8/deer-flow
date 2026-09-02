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
from app.gateway.routers.private_work_routes import context_controls
from app.private_work.errors import (
    PrivateWorkCompactionDisabled,
    PrivateWorkThreadBusy,
)
from deerflow.runtime.context_compaction import ThreadCompactionResult


class _CompactService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.error: Exception | None = None

    async def compact(self, context, thread_id: str, **kwargs):
        self.calls.append(
            {
                "context": context,
                "thread_id": thread_id,
                **kwargs,
            }
        )
        if self.error is not None:
            raise self.error
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
        context_controls,
        "_chat_control_service",
        lambda _request, _request_id: service,
    )
    return app, service, runtime_config


async def _compact_request(
    app: FastAPI,
    body: dict[str, object],
) -> httpx.Response:
    project_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.post(
            f"/api/projects/{project_id}/private-work/threads/{thread_id}/compact",
            json=body,
        )


@pytest.mark.asyncio
async def test_compact_api_defaults_to_the_policy_keep_and_preserves_failure_reason(
    compact_app: tuple[FastAPI, _CompactService, object],
) -> None:
    app, service, runtime_config = compact_app

    response = await _compact_request(app, {})

    assert response.status_code == 200
    assert response.json()["compacted"] is False
    assert response.json()["reason"] == "compaction_failed"
    assert len(service.calls) == 1
    assert service.calls[0]["keep"] is None
    assert service.calls[0]["force"] is True
    assert service.calls[0]["app_config"] is runtime_config


@pytest.mark.asyncio
async def test_compact_api_forwards_a_token_keep_override(
    compact_app: tuple[FastAPI, _CompactService, object],
) -> None:
    app, service, _runtime_config = compact_app

    response = await _compact_request(
        app,
        {"keep": {"type": "tokens", "value": 32_000}},
    )

    assert response.status_code == 200
    assert len(service.calls) == 1
    assert service.calls[0]["keep"] == ("tokens", 32_000)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keep",
    [
        {"type": "messages", "value": 0},
        {"type": "messages", "value": 10},
        {"type": "fraction", "value": 0.8},
    ],
)
async def test_compact_api_rejects_retired_keep_measurements(
    compact_app: tuple[FastAPI, _CompactService, object],
    keep: dict[str, object],
) -> None:
    app, service, _runtime_config = compact_app

    response = await _compact_request(app, {"keep": keep})

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -1, 0.5, 2_000_001])
async def test_compact_api_rejects_out_of_range_token_keep_values(
    compact_app: tuple[FastAPI, _CompactService, object],
    value: int | float,
) -> None:
    app, service, _runtime_config = compact_app

    response = await _compact_request(
        app,
        {"keep": {"type": "tokens", "value": value}},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [PrivateWorkThreadBusy, PrivateWorkCompactionDisabled],
)
async def test_compact_api_preserves_specific_compaction_conflicts(
    compact_app: tuple[FastAPI, _CompactService, object],
    error_type: type[Exception],
) -> None:
    app, service, _runtime_config = compact_app
    service.error = error_type("compact-api")

    response = await _compact_request(app, {})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_CONFLICT"
