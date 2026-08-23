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
from app.private_work.errors import PrivateWorkContextUsageUnsupported


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
    error_allowance_tokens: int
    safety_bound_tokens: int
    provider_input_tokens: int | None
    estimator_revision: str
    error_contract: str
    components: dict[str, dict[str, int]]
    fixed_over_trigger: bool
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
            error_allowance_tokens=23_476,
            safety_bound_tokens=139_576,
            provider_input_tokens=117_004,
            estimator_revision="provider-request-engineering-v1",
            error_contract=("versioned_engineering_allowance_for_app_owned_serialized_material_plus_declared_provider_overhead"),
            components={
                "compressible": {
                    "estimated_tokens": 100_000,
                    "error_allowance_tokens": 20_000,
                    "safety_bound_tokens": 120_000,
                },
                "fixed": {
                    "estimated_tokens": 15_000,
                    "error_allowance_tokens": 3_256,
                    "safety_bound_tokens": 18_256,
                },
                "ephemeral": {
                    "estimated_tokens": 1_100,
                    "error_allowance_tokens": 220,
                    "safety_bound_tokens": 1_320,
                },
            },
            fixed_over_trigger=False,
            message_count=42,
            summary_present=True,
            context_window_tokens=258_000,
            triggers=(trigger,),
            primary_trigger=trigger,
        )

    async def context_usage_authority_marker(self, context, thread_id: str):
        self.calls.append(
            {
                "context": context,
                "thread_id": thread_id,
                "operation": "authority-marker",
            }
        )
        return SimpleNamespace(cache_marker="active:44444444-4444-4444-8444-444444444444")


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
        "error_allowance_tokens": 23_476,
        "safety_bound_tokens": 139_576,
        "provider_input_tokens": 117_004,
        "estimator_revision": "provider-request-engineering-v1",
        "error_contract": ("versioned_engineering_allowance_for_app_owned_serialized_material_plus_declared_provider_overhead"),
        "components": {
            "compressible": {
                "estimated_tokens": 100_000,
                "error_allowance_tokens": 20_000,
                "safety_bound_tokens": 120_000,
            },
            "fixed": {
                "estimated_tokens": 15_000,
                "error_allowance_tokens": 3_256,
                "safety_bound_tokens": 18_256,
            },
            "ephemeral": {
                "estimated_tokens": 1_100,
                "error_allowance_tokens": 220,
                "safety_bound_tokens": 1_320,
            },
        },
        "fixed_over_trigger": False,
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


@pytest.mark.asyncio
async def test_context_usage_authority_api_is_a_lightweight_runtime_config_free_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(private_work_router.router)
    context = SimpleNamespace(request_id="context-authority-api")
    service = _ContextUsageService()
    app.dependency_overrides[private_work_context] = lambda: context
    app.dependency_overrides[require_project_private_open] = lambda: None
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
            f"/api/projects/{project_id}/private-work/threads/{thread_id}/context-usage/authority",
        )

    assert response.status_code == 200
    assert response.json() == {
        "thread_id": str(thread_id),
        "cache_marker": "active:44444444-4444-4444-8444-444444444444",
    }
    assert service.calls == [
        {
            "context": context,
            "thread_id": str(thread_id),
            "operation": "authority-marker",
        }
    ]


@pytest.mark.asyncio
async def test_context_usage_api_forwards_the_composer_selected_model(
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
    selected_model = uuid.uuid4()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/projects/{project_id}/private-work/threads/{thread_id}/context-usage",
            params={"model_name": str(selected_model)},
        )

    assert response.status_code == 200
    assert service.calls == [
        {
            "context": context,
            "thread_id": str(thread_id),
            "app_config": runtime_config,
            "selected_model_name": str(selected_model),
        }
    ]


@pytest.mark.asyncio
async def test_context_usage_api_returns_typed_unsupported_when_profile_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnsupportedService:
        async def context_usage(self, *_args, **_kwargs):
            raise PrivateWorkContextUsageUnsupported("context-usage-api")

    app = FastAPI()
    app.include_router(private_work_router.router)
    context = SimpleNamespace(request_id="context-usage-api")
    app.dependency_overrides[private_work_context] = lambda: context
    app.dependency_overrides[require_project_private_open] = lambda: None
    app.dependency_overrides[get_current_agent_runtime_config] = lambda: object()
    monkeypatch.setattr(
        private_work_router,
        "_chat_control_service",
        lambda _request, _request_id: _UnsupportedService(),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/projects/{uuid.uuid4()}/private-work/threads/{uuid.uuid4()}/context-usage",
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "CONTEXT_USAGE_UNSUPPORTED",
        "message": "Context usage cannot be measured safely for this Thread.",
        "request_id": "context-usage-api",
    }
