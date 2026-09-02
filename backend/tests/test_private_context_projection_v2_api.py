from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.deps import (
    private_work_context,
    require_project_private_open,
)
from app.gateway.routers import private_work as private_work_router
from app.gateway.routers.private_work_routes import context_controls

THREAD_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
EXECUTION_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _projection_payload(*, subagent: bool = False) -> dict[str, object]:
    subject: dict[str, object] = {
        "kind": "subagent_task" if subagent else "lead_thread",
        "thread_id": str(THREAD_ID),
        "execution_id": str(EXECUTION_ID) if subagent else None,
    }
    return {
        "contract_version": 2,
        "thread_id": str(THREAD_ID),
        "subject": subject,
        "phase": "settled" if subagent else "active",
        "projection_seq": "7",
        "evidence_seq": "11",
        "context_window_generation": "44444444-4444-4444-8444-444444444444",
        "checkpoint_id": "55555555-5555-4555-8555-555555555555",
        "projector_revision": "context-projector-v1",
        "model": {
            "identity_digest": "a" * 64,
            "context_window_tokens": 300_000,
        },
        "basis": "hybrid",
        "coverage": "complete",
        "freshness": "current",
        "totals": {
            "projected_tokens": 134_100,
            "lower_bound_tokens": 131_000,
            "safety_upper_bound_tokens": 140_000,
            "context_window_tokens": 300_000,
            "remaining_tokens": 165_900,
            "progress_percent": 44.7,
        },
        "lanes": [
            {
                "lane": "system_prompt",
                "projected_tokens": 5_700,
                "lower_bound_tokens": 5_500,
                "safety_upper_bound_tokens": 5_900,
            },
            {
                "lane": "conversation",
                "projected_tokens": 128_400,
                "lower_bound_tokens": 125_500,
                "safety_upper_bound_tokens": 134_100,
            },
        ],
        "last_provider_observation": {
            "provider_call_id": "b" * 64,
            "input_tokens": 132_800,
            "observed_at": "2026-08-27T00:00:00Z",
        },
        "compaction": {
            "enabled": True,
            "threshold_tokens": 240_000,
            "reached": False,
            "authority": "frozen_run",
            "blocked_reason": None,
        },
        "notices": [],
        "as_of": "2026-08-27T00:00:01Z",
    }


class _ContextProjectionService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def context_projection(
        self,
        context: object,
        thread_id: str,
        *,
        subject_kind: str,
        execution_id: str | None,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "context": context,
                "thread_id": thread_id,
                "subject_kind": subject_kind,
                "execution_id": execution_id,
            }
        )
        return _projection_payload(subagent=subject_kind == "subagent_task")


def _app(
    monkeypatch: pytest.MonkeyPatch,
    service: _ContextProjectionService,
) -> tuple[FastAPI, object]:
    app = FastAPI()
    app.include_router(private_work_router.router)
    context = SimpleNamespace(request_id="context-projection-v2-api")
    app.dependency_overrides[private_work_context] = lambda: context
    app.dependency_overrides[require_project_private_open] = lambda: None
    monkeypatch.setattr(
        context_controls,
        "_chat_control_service",
        lambda _request, _request_id: service,
    )
    return app, context


@pytest.mark.asyncio
async def test_context_projection_v2_returns_the_strict_lead_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _ContextProjectionService()
    app, context = _app(monkeypatch, service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/projects/{uuid.uuid4()}/private-work/threads/{THREAD_ID}/context-usage",
        )

    assert response.status_code == 200
    assert response.json() == _projection_payload()
    assert service.calls == [
        {
            "context": context,
            "thread_id": str(THREAD_ID),
            "subject_kind": "lead_thread",
            "execution_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_context_projection_v2_reads_one_authorized_subagent_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _ContextProjectionService()
    app, context = _app(monkeypatch, service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/projects/{uuid.uuid4()}/private-work/threads/{THREAD_ID}/context-usage",
            params={
                "subject_kind": "subagent_task",
                "subject_id": str(EXECUTION_ID),
            },
        )

    assert response.status_code == 200
    assert response.json() == _projection_payload(subagent=True)
    assert service.calls == [
        {
            "context": context,
            "thread_id": str(THREAD_ID),
            "subject_kind": "subagent_task",
            "execution_id": str(EXECUTION_ID),
        }
    ]


@pytest.mark.asyncio
async def test_context_projection_v2_removes_the_authority_marker_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _ContextProjectionService()
    app, _context = _app(monkeypatch, service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/projects/{uuid.uuid4()}/private-work/threads/{THREAD_ID}/context-usage/authority",
        )

    assert response.status_code == 404
    assert service.calls == []
