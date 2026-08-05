from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.deps import (
    get_current_agent_runtime_config,
    private_work_context,
    require_project_private_open,
)
from app.gateway.routers import project_memory as memory_router
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.config.app_config import AppConfig
from deerflow.config.memory_config import MemoryConfig


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="memory-pr8-request",
        )
    )


def _runtime_config() -> AppConfig:
    return AppConfig(
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        memory=MemoryConfig(
            enabled=False,
            pipeline_mode="consolidate",
            search_enabled=False,
            injection_enabled=True,
            consolidation_interval_minutes=45,
            candidate_retention_days=21,
        ),
    )


class _FactsService:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    async def list_facts(self, *_args: object, **kwargs: object) -> tuple[()]:
        self.kwargs = kwargs
        return ()


@pytest.fixture()
def app(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FastAPI, _FactsService]:
    service = _FactsService()
    value = FastAPI()
    value.include_router(memory_router.router)
    value.dependency_overrides[private_work_context] = _context
    value.dependency_overrides[require_project_private_open] = lambda: None
    value.dependency_overrides[get_current_agent_runtime_config] = _runtime_config
    monkeypatch.setattr(memory_router, "_v2_service", lambda _request: service)
    return value, service


async def _request(
    app: FastAPI,
    path: str,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_v2_status_returns_only_current_memory_pipeline_fields(
    app: tuple[FastAPI, _FactsService],
) -> None:
    value, _service = app

    response = await _request(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/v2/status",
    )

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "pipelineMode": "consolidate",
        "searchEnabled": False,
        "injectionEnabled": True,
        "consolidationIntervalMinutes": 45,
        "candidateRetentionDays": 21,
    }


@pytest.mark.asyncio
async def test_v2_fact_filters_are_trimmed_and_forwarded_before_pagination(
    app: tuple[FastAPI, _FactsService],
) -> None:
    value, service = app

    response = await _request(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/v2/facts?query=%20%20Concise%20%20&category=%20preference%20&limit=12&offset=4",
    )

    assert response.status_code == 200
    assert response.json() == {"namespace": "default", "items": []}
    assert service.kwargs == {
        "namespace": "default",
        "statuses": ("active",),
        "limit": 12,
        "offset": 4,
        "query": "Concise",
        "category": "preference",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("parameter", ["query=%20%20%20", f"query={'x' * 201}", "category=%20%20%20", f"category={'x' * 33}"])
async def test_v2_fact_filters_reject_empty_or_oversized_trimmed_values(
    app: tuple[FastAPI, _FactsService],
    parameter: str,
) -> None:
    value, _service = app

    response = await _request(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/v2/facts?{parameter}",
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"
