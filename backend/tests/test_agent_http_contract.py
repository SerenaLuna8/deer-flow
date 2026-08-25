from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import project_assets
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_service import (
    AgentAssetView,
    AgentCapabilityBindings,
    AgentDefinitionView,
    AgentInstructions,
    CreateAgent,
    ProjectAgentCreateResult,
)
from app.shared_assets.models import AgentModelSettings, AgentPayload, AssetScope, SkillAssetRef

_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_MEMBERSHIP_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_AGENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
_DEFINITION_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
_SKILL_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
_MCP_VERSION_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")
_REQUEST_ID = "agent-definition-http"
_NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


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


def _result() -> ProjectAgentCreateResult:
    return ProjectAgentCreateResult(
        asset=AgentAssetView(
            id=_AGENT_ID,
            scope=AssetScope.PROJECT,
            project_id=_PROJECT_ID,
            slug="release-agent",
            display_name="Release Agent",
            status="suspended",
            definition_id=_DEFINITION_ID,
            revision=1,
            created_by_user_id=str(_USER_ID),
            created_at=_NOW,
            updated_at=_NOW,
            description="Prepares releases",
        ),
        definition=AgentDefinitionView(
            definition_id=_DEFINITION_ID,
            agent_id=_AGENT_ID,
            description="Prepares releases",
            agents_instructions="Prepare a safe release plan.",
            soul="Be careful and explicit.",
            identity="Release coordinator",
            user_context="The user owns the release decision.",
            model_ref="default",
            model_settings=AgentModelSettings(temperature=0.2, max_tokens=4096),
            tool_groups=("web", "file:read"),
            skill_refs=(SkillAssetRef(AssetScope.PROJECT, _SKILL_ID),),
            mcp_version_ids=(_MCP_VERSION_ID,),
            payload_schema_version=4,
            payload_checksum="a" * 64,
            updated_by_user_id=str(_USER_ID),
            updated_at=_NOW,
        ),
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
        "model_ref": "default",
        "model_settings": {"temperature": 0.2, "max_tokens": 4096},
        "tool_groups": ["web", "file:read"],
        "skill_refs": [{"scope": "project", "asset_id": str(_SKILL_ID)}],
        "mcp_version_ids": [str(_MCP_VERSION_ID)],
    }


class _AgentService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def create_project(self, actor: ProjectContext, command: CreateAgent, payload: AgentPayload) -> ProjectAgentCreateResult:
        self.calls.append(("create", (actor, command, payload)))
        return _result()

    async def get(self, actor: ProjectContext, asset_id: uuid.UUID) -> ProjectAgentCreateResult:
        self.calls.append(("get", (actor, asset_id)))
        return _result()

    async def update_instructions(
        self,
        actor: ProjectContext,
        asset_id: uuid.UUID,
        instructions: AgentInstructions,
        *,
        expected_asset_version: int,
    ) -> ProjectAgentCreateResult:
        self.calls.append(("instructions", (actor, asset_id, instructions, expected_asset_version)))
        return _result()

    async def update_capability_bindings(
        self,
        actor: ProjectContext,
        asset_id: uuid.UUID,
        bindings: AgentCapabilityBindings,
        *,
        expected_asset_version: int,
    ) -> ProjectAgentCreateResult:
        self.calls.append(("bindings", (actor, asset_id, bindings, expected_asset_version)))
        return _result()


def _app(service: _AgentService) -> FastAPI:
    application = FastAPI()
    application.dependency_overrides[project_assets.project_asset_context] = _context
    application.dependency_overrides[project_assets.get_agent_service] = lambda: service
    application.include_router(project_assets.project_router)
    return application


def _assert_definition_contract(payload: dict[str, object]) -> None:
    assert set(payload) == {"item", "definition", "request_id"}
    assert payload["request_id"] == _REQUEST_ID
    item = payload["item"]
    definition = payload["definition"]
    assert isinstance(item, dict)
    assert isinstance(definition, dict)
    assert set(item) == {
        "id",
        "scope",
        "project_id",
        "slug",
        "display_name",
        "status",
        "definition_id",
        "revision",
        "created_by_user_id",
        "created_at",
        "updated_at",
    }
    assert set(definition) == {
        "definition_id",
        "agent_id",
        "description",
        "agents_instructions",
        "soul",
        "identity",
        "user_context",
        "model_ref",
        "model_settings",
        "tool_groups",
        "skill_refs",
        "mcp_version_ids",
        "payload_schema_version",
        "payload_checksum",
        "updated_by_user_id",
        "updated_at",
    }
    assert item["definition_id"] == str(_DEFINITION_ID)
    assert definition["definition_id"] == str(_DEFINITION_ID)
    assert "version_number" not in definition
    assert "relation" not in definition
    assert "supersedes_version_id" not in definition


@pytest.mark.asyncio
async def test_project_agent_create_requires_complete_definition() -> None:
    service = _AgentService()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(service)), base_url="http://test") as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/agents",
            json={"slug": "release-agent", "display_name": "Release Agent"},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "asset_validation_failed"
    assert service.calls == []


@pytest.mark.asyncio
async def test_project_agent_create_returns_item_and_definition() -> None:
    service = _AgentService()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(service)), base_url="http://test") as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/agents",
            json=_complete_request(),
        )

    assert response.status_code == 201
    _assert_definition_contract(response.json())
    assert service.calls[0][0] == "create"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "suffix", "body", "expected_call"),
    [
        ("GET", "", None, "get"),
        (
            "PUT",
            "/instructions",
            {
                "agents_instructions": "Prepare a safe release plan.",
                "soul": "Be careful and explicit.",
                "identity": "Release coordinator",
                "user_context": "The user owns the release decision.",
                "expected_revision": 1,
            },
            "instructions",
        ),
        (
            "PUT",
            "/capability-bindings",
            {
                "skill_refs": [{"scope": "project", "asset_id": str(_SKILL_ID)}],
                "mcp_version_ids": [str(_MCP_VERSION_ID)],
                "expected_revision": 1,
            },
            "bindings",
        ),
    ],
)
async def test_project_agent_read_and_updates_share_definition_contract(
    method: str,
    suffix: str,
    body: dict[str, object] | None,
    expected_call: str,
) -> None:
    service = _AgentService()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(service)), base_url="http://test") as client:
        response = await client.request(
            method,
            f"/api/projects/{_PROJECT_ID}/agents/{_AGENT_ID}{suffix}",
            json=body,
        )

    assert response.status_code == 200
    _assert_definition_contract(response.json())
    assert service.calls[0][0] == expected_call
