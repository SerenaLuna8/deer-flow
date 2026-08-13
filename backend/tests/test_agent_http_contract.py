from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from app.gateway.routers import project_assets
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import (
    AgentAssetView,
    AgentModelSettings,
    AgentPayload,
    AgentVersionView,
    AssetScope,
    CreateAgent,
    ProjectAgentCreateResult,
    WorkflowStatus,
)
from app.shared_assets.contexts import SystemAssetGovernanceContext

_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_MEMBERSHIP_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_ASSET_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
_VERSION_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
_SKILL_VERSION_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
_MCP_VERSION_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")
_REQUEST_ID = "agent-http-contract"
_NOW = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)


def _context() -> ProjectContext:
    role = ProjectRole.ADMIN
    return ProjectContext(
        user_id=_USER_ID,
        project_id=_PROJECT_ID,
        membership_id=_MEMBERSHIP_ID,
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id=_REQUEST_ID,
    )


def _asset() -> AgentAssetView:
    return AgentAssetView(
        id=_ASSET_ID,
        scope=AssetScope.PROJECT,
        project_id=_PROJECT_ID,
        slug="release-agent",
        display_name="Release Agent",
        status="suspended",
        current_published_version_id=None,
        version=2,
        created_by_user_id=str(_USER_ID),
        created_at=_NOW,
        updated_at=_NOW,
        description="Prepares releases",
    )


def _version(*, workflow_status: WorkflowStatus) -> AgentVersionView:
    return AgentVersionView(
        id=_VERSION_ID,
        agent_id=_ASSET_ID,
        version_number=1,
        workflow_status=workflow_status,
        description="Prepares releases",
        agents_instructions="Prepare a safe release plan.",
        soul="Be careful and explicit.",
        identity="Release coordinator",
        user_context="The user owns the release decision.",
        model_ref="primary",
        model_settings=AgentModelSettings(
            temperature=0.2,
            max_tokens=4096,
            thinking_enabled=True,
            reasoning_effort="medium",
        ),
        tool_groups=("web", "filesystem"),
        skill_version_ids=(_SKILL_VERSION_ID,),
        mcp_version_ids=(_MCP_VERSION_ID,),
        supersedes_version_id=None,
        payload_schema_version=3,
        payload_checksum="a" * 64,
        created_by_user_id=str(_USER_ID),
        created_at=_NOW,
    )


def _complete_request() -> dict[str, object]:
    return {
        "slug": "release-agent",
        "display_name": "Release Agent",
        "description": "Prepares releases",
        "agents_instructions": "Prepare a safe release plan.",
        "soul": "Be careful and explicit.",
        "identity": "Release coordinator",
        "user_context": "The user owns the release decision.",
        "model_ref": "primary",
        "model_settings": {
            "temperature": 0.2,
            "max_tokens": 4096,
            "thinking_enabled": True,
            "reasoning_effort": "medium",
        },
        "tool_groups": ["web", "filesystem"],
        "skill_version_ids": [str(_SKILL_VERSION_ID)],
        "mcp_version_ids": [str(_MCP_VERSION_ID)],
    }


class _AgentService:
    def __init__(self) -> None:
        self.create_calls: list[
            tuple[
                ProjectContext | SystemAssetGovernanceContext,
                CreateAgent,
                AgentPayload,
            ]
        ] = []
        self.publish_calls: list[tuple[ProjectContext, uuid.UUID, uuid.UUID, int]] = []

    async def create_project(
        self,
        actor: ProjectContext | SystemAssetGovernanceContext,
        command: CreateAgent,
        payload: AgentPayload,
    ) -> ProjectAgentCreateResult:
        self.create_calls.append((actor, command, payload))
        return ProjectAgentCreateResult(
            asset=_asset(),
            version=_version(workflow_status=WorkflowStatus.DRAFT),
        )

    async def publish(
        self,
        actor: ProjectContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> AgentVersionView:
        self.publish_calls.append((actor, asset_id, version_id, expected_asset_version))
        return _version(workflow_status=WorkflowStatus.PUBLISHED)


def _app(
    service: _AgentService,
    *,
    context: ProjectContext | SystemAssetGovernanceContext | None = None,
) -> FastAPI:
    application = FastAPI()
    actor = context or _context()
    application.dependency_overrides[project_assets.project_asset_context] = lambda: actor
    application.dependency_overrides[project_assets.get_agent_service] = lambda: service
    application.include_router(project_assets.project_router)
    return application


async def _post(
    application: FastAPI,
    path: str,
    payload: dict[str, object],
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        return await client.post(path, json=payload)


@pytest.mark.asyncio
async def test_project_agent_create_requires_the_complete_definition() -> None:
    service = _AgentService()
    application = _app(service)

    response = await _post(
        application,
        f"/api/projects/{_PROJECT_ID}/agents",
        {"slug": "release-agent", "display_name": "Release Agent"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "asset_validation_failed"
    assert service.create_calls == []


@pytest.mark.asyncio
async def test_project_agent_create_is_atomic_asset_and_draft_version_contract() -> None:
    service = _AgentService()
    application = _app(service)

    response = await _post(
        application,
        f"/api/projects/{_PROJECT_ID}/agents",
        _complete_request(),
    )

    assert response.status_code == 201
    assert response.json() == {
        "item": {
            "id": str(_ASSET_ID),
            "scope": "project",
            "project_id": str(_PROJECT_ID),
            "slug": "release-agent",
            "display_name": "Release Agent",
            "status": "suspended",
            "current_published_version_id": None,
            "version": 2,
            "created_by_user_id": str(_USER_ID),
            "created_at": _NOW.isoformat().replace("+00:00", "Z"),
            "updated_at": _NOW.isoformat().replace("+00:00", "Z"),
        },
        "version": {
            "id": str(_VERSION_ID),
            "agent_id": str(_ASSET_ID),
            "version_number": 1,
            "workflow_status": "draft",
            "description": "Prepares releases",
            "agents_instructions": "Prepare a safe release plan.",
            "soul": "Be careful and explicit.",
            "identity": "Release coordinator",
            "user_context": "The user owns the release decision.",
            "model_ref": "primary",
            "model_settings": {
                "temperature": 0.2,
                "max_tokens": 4096,
                "thinking_enabled": True,
                "reasoning_effort": "medium",
            },
            "tool_groups": ["web", "filesystem"],
            "skill_version_ids": [str(_SKILL_VERSION_ID)],
            "mcp_version_ids": [str(_MCP_VERSION_ID)],
            "supersedes_version_id": None,
            "payload_schema_version": 3,
            "payload_checksum": "a" * 64,
            "created_by_user_id": str(_USER_ID),
            "created_at": _NOW.isoformat().replace("+00:00", "Z"),
        },
        "request_id": _REQUEST_ID,
    }
    assert service.create_calls == [
        (
            _context(),
            CreateAgent(slug="release-agent", display_name="Release Agent"),
            AgentPayload(
                description="Prepares releases",
                agents_instructions="Prepare a safe release plan.",
                soul="Be careful and explicit.",
                identity="Release coordinator",
                user_context="The user owns the release decision.",
                model_ref="primary",
                model_settings=AgentModelSettings(
                    temperature=0.2,
                    max_tokens=4096,
                    thinking_enabled=True,
                    reasoning_effort="medium",
                ),
                tool_groups=("web", "filesystem"),
                skill_version_ids=(_SKILL_VERSION_ID,),
                mcp_version_ids=(_MCP_VERSION_ID,),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_project_agent_publish_has_an_explicit_cas_route() -> None:
    service = _AgentService()
    application = _app(service)

    response = await _post(
        application,
        (f"/api/projects/{_PROJECT_ID}/agents/{_ASSET_ID}/versions/{_VERSION_ID}/publish"),
        {"expected_asset_version": 2},
    )

    assert response.status_code == 200
    assert response.json()["data"]["workflow_status"] == "published"
    assert response.json()["request_id"] == _REQUEST_ID
    assert service.publish_calls == [
        (_context(), _ASSET_ID, _VERSION_ID, 2),
    ]


@pytest.mark.asyncio
async def test_admin_project_agent_create_uses_the_same_complete_contract() -> None:
    service = _AgentService()
    actor = SystemAssetGovernanceContext(
        user_id=_USER_ID,
        request_id=_REQUEST_ID,
        project_id=_PROJECT_ID,
    )
    router = APIRouter(
        prefix="/api/admin/projects/{project_id}/assets",
        route_class=project_assets.AssetRoute,
    )

    async def actor_dependency() -> SystemAssetGovernanceContext:
        return actor

    project_assets.register_asset_routes(router, actor_dependency)
    application = FastAPI()
    application.dependency_overrides[project_assets.get_agent_service] = lambda: service
    application.include_router(router)

    response = await _post(
        application,
        f"/api/admin/projects/{_PROJECT_ID}/assets/agents",
        _complete_request(),
    )

    assert response.status_code == 201
    assert response.json()["version"]["workflow_status"] == "draft"
    assert response.json()["request_id"] == _REQUEST_ID
    assert len(service.create_calls) == 1
    called_actor, command, payload = service.create_calls[0]
    assert called_actor == actor
    assert command == CreateAgent(
        slug="release-agent",
        display_name="Release Agent",
    )
    assert payload.model_ref == "primary"
    assert payload.skill_version_ids == (_SKILL_VERSION_ID,)
    assert payload.mcp_version_ids == (_MCP_VERSION_ID,)
