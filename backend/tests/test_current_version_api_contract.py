from __future__ import annotations

import uuid
from dataclasses import fields
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import project_assets
from app.gateway.routers.project_assets import (
    AgentCapabilityBindingsRequest,
    AgentCreateRequest,
    AgentVersionItemResponse,
    CurrentVersionAssetItemResponse,
    McpVersionItemResponse,
    SkillVersionItemResponse,
    _scoped_assets,
    project_router,
)
from app.projects.capabilities import Capability
from app.shared_assets.agent_service import AgentAssetView
from app.shared_assets.binding_service import SystemAssetBinding
from app.shared_assets.models import AssetKind, AssetScope
from app.shared_assets.skill_service import SkillAssetView
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
    ProjectSystemAgentBindingRow,
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionRow,
)


def test_agent_and_skill_asset_api_uses_current_version_and_revision() -> None:
    fields = CurrentVersionAssetItemResponse.model_fields

    assert "current_version_id" in fields
    assert "revision" in fields
    assert "current_published_version_id" not in fields
    assert "version" not in fields


def test_agent_and_skill_domain_views_use_current_version_and_revision() -> None:
    for view_type in (AgentAssetView, SkillAssetView):
        names = {field.name for field in fields(view_type)}

        assert "current_version_id" in names
        assert "revision" in names
        assert "current_published_version_id" not in names
        assert "version" not in names


def test_agent_and_skill_version_api_exposes_relation_without_workflow_state() -> None:
    for response_type in (AgentVersionItemResponse, SkillVersionItemResponse):
        fields = response_type.model_fields

        assert "relation" in fields
        assert "workflow_status" not in fields

    agent_fields = AgentVersionItemResponse.model_fields
    assert "skill_refs" in agent_fields
    assert "skill_version_ids" not in agent_fields

    # MCP keeps its independent governance workflow.
    assert "workflow_status" in McpVersionItemResponse.model_fields


def test_project_agent_api_uses_skill_assets_and_activation_actions() -> None:
    for request_type in (AgentCreateRequest, AgentCapabilityBindingsRequest):
        fields = request_type.model_fields
        assert "skill_refs" in fields
        assert "skill_version_ids" not in fields

    route_paths = {route.path for route in project_router.routes}
    assert "/api/projects/{project_id}/agents/{asset_id}/versions/{version_id}/activate" in route_paths
    assert "/api/projects/{project_id}/agents/{asset_id}/enable" in route_paths
    assert "/api/projects/{project_id}/agents/{asset_id}/versions/{version_id}/publish" not in route_paths
    assert "/api/projects/{project_id}/agents/{asset_id}/versions/{version_id}/restore" not in route_paths


def test_agent_and_skill_persistence_uses_current_version_without_workflow_state() -> None:
    for row_type in (AgentRow, SkillRow):
        columns = row_type.__table__.columns
        assert "current_version_id" in columns
        assert "revision" in columns
        assert "current_published_version_id" not in columns
        assert "version" not in columns

    for row_type in (AgentVersionRow, SkillVersionRow):
        assert "workflow_status" not in row_type.__table__.columns

    ref_columns = AgentVersionSkillRefRow.__table__.columns
    assert "skill_asset_scope" in ref_columns
    assert "skill_asset_id" in ref_columns
    assert "skill_version_id" not in ref_columns

    assert "agent_version_id" not in ProjectSystemAgentBindingRow.__table__.columns
    assert "skill_version_id" not in ProjectSystemSkillBindingRow.__table__.columns


def test_project_skill_list_serializes_system_binding_with_current_version() -> None:
    project_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    current_version_id = uuid.uuid4()
    user_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    skill = SkillAssetView(
        id=skill_id,
        scope=AssetScope.SYSTEM,
        project_id=None,
        slug="system-skill",
        display_name="System Skill",
        status="active",
        current_version_id=current_version_id,
        revision=1,
        created_by_user_id=user_id,
        created_at=now,
        updated_at=now,
        description="System Skill",
    )
    binding = SystemAssetBinding(
        project_id=project_id,
        kind=AssetKind.SKILL,
        asset_id=skill_id,
        version_id=current_version_id,
        enabled=True,
        version=1,
        created_by_user_id=user_id,
        updated_by_user_id=user_id,
        created_at=now,
        updated_at=now,
    )
    context = SimpleNamespace(
        capabilities=frozenset({Capability.SHARED_ASSETS_READ}),
        request_id="current-binding-contract",
    )

    response = _scoped_assets((skill,), (binding,), context, AssetKind.SKILL)
    payload = response.model_dump(mode="json")

    binding_payload = payload["system_items"][0]["binding"]
    assert binding_payload["current_version_id"] == str(current_version_id)
    assert "version_id" not in binding_payload


class _EmptySystemVersionService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, uuid.UUID]] = []

    async def get_version_history(self, actor, asset_id):
        self.calls.append((actor, asset_id))
        return ()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agents", "skills"])
async def test_system_catalog_exposes_read_only_current_version_detail(kind: str) -> None:
    actor = SimpleNamespace(request_id="system-current-detail")
    asset_id = uuid.uuid4()
    service = _EmptySystemVersionService()
    application = FastAPI()
    application.include_router(project_assets.catalog_router)
    application.dependency_overrides[project_assets.system_asset_catalog_actor] = lambda: actor
    dependency = project_assets.get_agent_service if kind == "agents" else project_assets.get_skill_service
    application.dependency_overrides[dependency] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/assets/catalog/{kind}/{asset_id}/versions",
        )

    assert response.status_code == 200
    assert response.json() == {"data": [], "request_id": actor.request_id}
    assert service.calls == [(actor, asset_id)]
