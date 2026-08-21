from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers.project_agent_builder import (
    get_agent_design_service,
    router,
)
from app.gateway.routers.project_assets import project_asset_context
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_design_generation import AgentDesignConflict
from app.shared_assets.agent_design_service import (
    AgentDesignSessionPage,
    AgentDesignSessionSummary,
    AgentDesignSessionView,
    AgentDesignStatus,
)
from app.shared_assets.agent_service import AgentAssetView
from app.shared_assets.models import AssetScope

_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_SESSION_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_THREAD_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_NOW = datetime(2026, 8, 18, tzinfo=UTC)


def _context() -> ProjectContext:
    role = ProjectRole.EDITOR
    return ProjectContext(
        user_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        project_id=_PROJECT_ID,
        membership_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="agent-builder-contract-version",
    )


def _session_view() -> AgentDesignSessionView:
    return AgentDesignSessionView(
        id=_SESSION_ID,
        project_id=_PROJECT_ID,
        owner_user_id=str(_context().user_id),
        thread_id=_THREAD_ID,
        slug="contract-agent",
        display_name="Contract Agent",
        status=AgentDesignStatus.PROPOSAL_READY,
        revision=2,
        blueprint=None,
        blueprint_checksum=None,
        assumptions=("Only inspect the current project.",),
        conflicts=(
            AgentDesignConflict(
                code="AMBIGUOUS_SCOPE",
                fields=("agents_instructions",),
                message="The review scope needs confirmation.",
                severity="warning",
            ),
        ),
        messages=(),
        active_clarification=None,
        active_clarifications=(),
        progress=(),
        error_code=None,
        error_message=None,
        created_agent_id=None,
        created_at=_NOW,
        updated_at=_NOW,
        generation_preference={"model_ref": "default", "mode": "pro"},
    )


class _BuilderService:
    def __init__(self) -> None:
        self.view = _session_view()

    async def create(self, *_args: object, **_kwargs: object) -> AgentDesignSessionView:
        return self.view

    async def get(self, *_args: object, **_kwargs: object) -> AgentDesignSessionView:
        return self.view

    async def list_incomplete(self, *_args: object, **_kwargs: object) -> AgentDesignSessionPage:
        return AgentDesignSessionPage(
            items=(
                AgentDesignSessionSummary(
                    id=self.view.id,
                    slug=self.view.slug,
                    display_name=self.view.display_name,
                    status=self.view.status,
                    revision=self.view.revision,
                    updated_at=self.view.updated_at,
                ),
            ),
            next_cursor="next-contract-page",
        )

    async def submit_turn(self, *_args: object, **_kwargs: object) -> AgentDesignSessionView:
        return self.view

    async def commit(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            session=self.view,
            agent=AgentAssetView(
                id=uuid.UUID("66666666-6666-4666-8666-666666666666"),
                scope=AssetScope.PROJECT,
                project_id=_PROJECT_ID,
                slug=self.view.slug,
                display_name=self.view.display_name,
                status="suspended",
                current_version_id=None,
                revision=1,
                created_by_user_id=self.view.owner_user_id,
                created_at=_NOW,
                updated_at=_NOW,
            ),
        )

    async def cancel(self, *_args: object, **_kwargs: object) -> AgentDesignSessionView:
        return self.view


def _app() -> FastAPI:
    application = FastAPI()
    service = _BuilderService()
    application.dependency_overrides[project_asset_context] = _context
    application.dependency_overrides[get_agent_design_service] = lambda: service
    application.include_router(router)
    return application


_ROUTE_CASES: tuple[
    tuple[
        Literal["GET", "POST"],
        str,
        dict[str, object] | None,
        Literal["session", "list", "commit"],
        int,
    ],
    ...,
] = (
    (
        "POST",
        f"/api/projects/{_PROJECT_ID}/agent-builder/sessions",
        {
            "slug": "contract-agent",
            "display_name": "Contract Agent",
            "idempotency_key": "create-contract-agent",
        },
        "session",
        201,
    ),
    (
        "GET",
        f"/api/projects/{_PROJECT_ID}/agent-builder/sessions/{_SESSION_ID}",
        None,
        "session",
        200,
    ),
    (
        "GET",
        f"/api/projects/{_PROJECT_ID}/agent-builder/sessions",
        None,
        "list",
        200,
    ),
    (
        "POST",
        f"/api/projects/{_PROJECT_ID}/agent-builder/sessions/{_SESSION_ID}/turns",
        {
            "input": {"kind": "message", "message": "Design an Agent"},
            "expected_revision": 2,
            "idempotency_key": "turn-contract-agent",
        },
        "session",
        200,
    ),
    (
        "POST",
        f"/api/projects/{_PROJECT_ID}/agent-builder/sessions/{_SESSION_ID}/commit",
        {
            "expected_revision": 2,
            "expected_blueprint_checksum": "a" * 64,
            "idempotency_key": "commit-contract-agent",
        },
        "commit",
        200,
    ),
    (
        "POST",
        f"/api/projects/{_PROJECT_ID}/agent-builder/sessions/{_SESSION_ID}/cancel",
        {
            "expected_revision": 2,
            "idempotency_key": "cancel-contract-agent",
        },
        "session",
        200,
    ),
)


async def _request(
    application: FastAPI,
    method: Literal["GET", "POST"],
    path: str,
    body: dict[str, object] | None,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, json=body)


def _session_document(
    payload: dict[str, object],
    response_kind: Literal["session", "commit"],
) -> dict[str, object]:
    data = payload["data"]
    assert isinstance(data, dict)
    if response_kind == "commit":
        session = data["session"]
        assert isinstance(session, dict)
        return session
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body", "response_kind", "expected_status"),
    _ROUTE_CASES,
)
async def test_default_contract_omits_v2_only_fields(
    method: Literal["GET", "POST"],
    path: str,
    body: dict[str, object] | None,
    response_kind: Literal["session", "list", "commit"],
    expected_status: int,
) -> None:
    response = await _request(_app(), method, path, body)

    assert response.status_code == expected_status
    payload = response.json()
    if response_kind == "list":
        assert "next_cursor" not in payload
    else:
        session = _session_document(payload, response_kind)
        assert "assumptions" not in session
        assert "conflicts" not in session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body", "response_kind", "expected_status"),
    _ROUTE_CASES,
)
async def test_contract_v2_exposes_extended_fields(
    method: Literal["GET", "POST"],
    path: str,
    body: dict[str, object] | None,
    response_kind: Literal["session", "list", "commit"],
    expected_status: int,
) -> None:
    separator = "&" if "?" in path else "?"
    response = await _request(
        _app(),
        method,
        f"{path}{separator}contract_version=2",
        body,
    )

    assert response.status_code == expected_status
    payload = response.json()
    if response_kind == "list":
        assert payload["next_cursor"] == "next-contract-page"
    else:
        session = _session_document(payload, response_kind)
        assert session["assumptions"] == ["Only inspect the current project."]
        assert session["conflicts"][0]["code"] == "AMBIGUOUS_SCOPE"


@pytest.mark.asyncio
async def test_explicit_contract_v1_matches_the_default_shape() -> None:
    response = await _request(
        _app(),
        "GET",
        (f"/api/projects/{_PROJECT_ID}/agent-builder/sessions/{_SESSION_ID}?contract_version=1"),
        None,
    )

    assert response.status_code == 200
    session = _session_document(response.json(), "session")
    assert "assumptions" not in session
    assert "conflicts" not in session


@pytest.mark.asyncio
async def test_explicit_contract_v3_exposes_builder_generation_preference() -> None:
    response = await _request(
        _app(),
        "GET",
        (f"/api/projects/{_PROJECT_ID}/agent-builder/sessions/{_SESSION_ID}?contract_version=3"),
        None,
    )

    assert response.status_code == 200
    session = _session_document(response.json(), "session")
    assert session["generation_preference"] == {
        "model_ref": "default",
        "mode": "pro",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contract_version",
    ("0", "4", "latest", "1.0", "01", "2.0"),
)
async def test_contract_version_rejects_values_other_than_supported_versions(
    contract_version: str,
) -> None:
    response = await _request(
        _app(),
        "GET",
        (f"/api/projects/{_PROJECT_ID}/agent-builder/sessions/{_SESSION_ID}?contract_version={contract_version}"),
        None,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "asset_validation_failed"
