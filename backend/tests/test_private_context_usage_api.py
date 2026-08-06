from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.deps import (
    get_current_agent_runtime_config,
    private_work_context,
    require_project_private_open,
)
from app.gateway.routers import private_work as private_work_router


@dataclass(frozen=True)
class _Trigger:
    type: Literal["fraction"]
    configured_value: float
    current_value: float
    threshold_value: float
    remaining_value: float
    progress_percent: float
    reached: bool
    context_window_tokens: int
    threshold_tokens: int


@dataclass(frozen=True)
class _Usage:
    enabled: bool
    estimated_tokens: int
    message_count: int
    summary_present: bool
    context_window_tokens: int
    triggers: tuple[_Trigger, ...]
    primary_trigger: _Trigger


class _ContextUsageService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def context_usage(self, context, thread_id: str, **kwargs):
        self.calls.append(
            {
                "context": context,
                "thread_id": thread_id,
                **kwargs,
            }
        )
        trigger = _Trigger(
            type="fraction",
            configured_value=0.8,
            current_value=0.45,
            threshold_value=0.8,
            remaining_value=0.35,
            progress_percent=56.25,
            reached=False,
            context_window_tokens=258_000,
            threshold_tokens=206_400,
        )
        return _Usage(
            enabled=True,
            estimated_tokens=116_100,
            message_count=42,
            summary_present=True,
            context_window_tokens=258_000,
            triggers=(trigger,),
            primary_trigger=trigger,
        )


@pytest.mark.asyncio
async def test_context_usage_api_returns_strict_current_policy_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(private_work_router.router)
    context = SimpleNamespace(request_id="context-usage-api")
    runtime_config = object()
    service = _ContextUsageService()
    app.dependency_overrides[private_work_context] = lambda: context
    app.dependency_overrides[require_project_private_open] = lambda: None
    app.dependency_overrides[get_current_agent_runtime_config] = lambda: runtime_config
    monkeypatch.setattr(
        private_work_router,
        "_chat_control_service",
        lambda _request, _request_id: service,
    )
    project_id = uuid.uuid4()
    thread_id = uuid.uuid4()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/projects/{project_id}/private-work/threads/{thread_id}/context-usage",
        )

    assert response.status_code == 200
    assert response.json() == {
        "thread_id": str(thread_id),
        "enabled": True,
        "estimated_tokens": 116_100,
        "message_count": 42,
        "summary_present": True,
        "context_window_tokens": 258_000,
        "triggers": [
            {
                "type": "fraction",
                "configured_value": 0.8,
                "current_value": 0.45,
                "threshold_value": 0.8,
                "remaining_value": 0.35,
                "progress_percent": 56.25,
                "reached": False,
                "context_window_tokens": 258_000,
                "threshold_tokens": 206_400,
            }
        ],
        "primary_trigger": {
            "type": "fraction",
            "configured_value": 0.8,
            "current_value": 0.45,
            "threshold_value": 0.8,
            "remaining_value": 0.35,
            "progress_percent": 56.25,
            "reached": False,
            "context_window_tokens": 258_000,
            "threshold_tokens": 206_400,
        },
    }
    assert service.calls == [
        {
            "context": context,
            "thread_id": str(thread_id),
            "app_config": runtime_config,
        }
    ]
