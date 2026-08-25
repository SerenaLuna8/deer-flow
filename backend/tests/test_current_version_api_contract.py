from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.gateway.routers.project_assets import (
    AgentAssetItemResponse,
    AgentCapabilityBindingsRequest,
    AgentCreateRequest,
    AgentDefinitionItemResponse,
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
    AgentMcpRefRow,
    AgentRow,
    AgentSkillRefRow,
    ProjectSystemAgentBindingRow,
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionRow,
)


def test_project_agent_api_exposes_one_definition_without_version_routes() -> None:
    item_fields = AgentAssetItemResponse.model_fields
    definition_fields = AgentDefinitionItemResponse.model_fields

    assert "definition_id" in item_fields
    assert "current_version_id" not in item_fields
    assert "definition_id" in definition_fields
    assert "agent_id" in definition_fields
    assert "version_number" not in definition_fields
    assert "relation" not in definition_fields
    assert "supersedes_version_id" not in definition_fields

    for request_type in (AgentCreateRequest, AgentCapabilityBindingsRequest):
        assert "skill_refs" in request_type.model_fields
        assert "skill_version_ids" not in request_type.model_fields

    route_paths = {route.path for route in project_router.routes}
    assert "/api/projects/{project_id}/agents/{asset_id}" in route_paths
    assert "/api/projects/{project_id}/agents/{asset_id}/versions" not in route_paths
    assert "/api/projects/{project_id}/agents/{asset_id}/versions/{version_id}/activate" not in route_paths


def test_agent_persistence_owns_definition_and_direct_references() -> None:
    columns = AgentRow.__table__.columns
    assert "definition_id" in columns
    assert "current_version_id" not in columns
    assert "payload_checksum" in columns
    assert "revision" in columns

    assert AgentSkillRefRow.__tablename__ == "agent_skill_refs"
    assert "agent_id" in AgentSkillRefRow.__table__.columns
    assert "agent_version_id" not in AgentSkillRefRow.__table__.columns
    assert AgentMcpRefRow.__tablename__ == "agent_mcp_refs"
    assert "agent_id" in AgentMcpRefRow.__table__.columns
    assert "agent_version_id" not in AgentMcpRefRow.__table__.columns
    assert "agent_version_id" not in ProjectSystemAgentBindingRow.__table__.columns


def test_skill_keeps_current_version_and_mcp_keeps_release_workflow() -> None:
    fields = CurrentVersionAssetItemResponse.model_fields
    assert "current_version_id" in fields
    assert "revision" in fields
    assert "current_published_version_id" not in fields

    assert "relation" in SkillVersionItemResponse.model_fields
    assert "workflow_status" not in SkillVersionItemResponse.model_fields
    assert "workflow_status" in McpVersionItemResponse.model_fields

    assert "current_version_id" in SkillRow.__table__.columns
    assert "workflow_status" not in SkillVersionRow.__table__.columns
    assert "skill_version_id" not in ProjectSystemSkillBindingRow.__table__.columns


def test_project_agent_list_serializes_system_binding_with_definition_id() -> None:
    project_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    definition_id = uuid.uuid4()
    user_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    agent = AgentAssetView(
        id=agent_id,
        scope=AssetScope.SYSTEM,
        project_id=None,
        slug="system-agent",
        display_name="System Agent",
        status="active",
        definition_id=definition_id,
        revision=1,
        created_by_user_id=user_id,
        created_at=now,
        updated_at=now,
        description="System Agent",
    )
    binding = SystemAssetBinding(
        project_id=project_id,
        kind=AssetKind.AGENT,
        asset_id=agent_id,
        version_id=definition_id,
        enabled=True,
        version=1,
        created_by_user_id=user_id,
        updated_by_user_id=user_id,
        created_at=now,
        updated_at=now,
    )
    context = SimpleNamespace(
        capabilities=frozenset({Capability.SHARED_ASSETS_READ}),
        request_id="agent-definition-binding-contract",
    )

    payload = _scoped_assets((agent,), (binding,), context, AssetKind.AGENT).model_dump(mode="json")

    binding_payload = payload["system_items"][0]["binding"]
    assert binding_payload["definition_id"] == str(definition_id)
    assert "current_version_id" not in binding_payload
    assert "version_id" not in binding_payload


def test_project_skill_list_still_serializes_current_version_binding() -> None:
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
        request_id="skill-current-binding-contract",
    )

    payload = _scoped_assets((skill,), (binding,), context, AssetKind.SKILL).model_dump(mode="json")

    binding_payload = payload["system_items"][0]["binding"]
    assert binding_payload["current_version_id"] == str(current_version_id)
    assert "version_id" not in binding_payload
