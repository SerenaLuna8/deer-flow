from __future__ import annotations

import dataclasses
import importlib
import uuid
from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.gateway.deps import get_current_user_from_request
from app.gateway.routers import admin_assets, project_assets
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_service import AgentAssetView, AgentVersionView
from app.shared_assets.binding_service import SystemAssetBinding
from app.shared_assets.credential_service import (
    CredentialGrantMigrationView,
    CredentialVersionView,
    CredentialView,
)
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.mcp_service import (
    McpAssetView,
    McpCredentialSlotView,
    McpDefinition,
    McpToolInventoryView,
    McpToolView,
    McpVersionView,
)
from app.shared_assets.models import AssetKind, AssetScope, WorkflowStatus
from app.shared_assets.skill_service import (
    ProjectSkillArchiveCreateResult,
    SkillAssetView,
    SkillFileContentView,
    SkillVersionView,
)

PROJECT_ID = uuid.uuid4()
NOW = datetime.now(UTC)


def test_authenticated_user_can_read_postgres_system_catalog_without_admin_role() -> None:
    service = AsyncMock()
    system_agent = _agent(AssetScope.SYSTEM)
    service.list_visible.return_value = (system_agent,)
    user_id = uuid.uuid4()
    app = FastAPI()
    app.include_router(project_assets.catalog_router)
    app.dependency_overrides[get_current_user_from_request] = lambda: SimpleNamespace(
        id=user_id,
        system_role="user",
    )
    app.dependency_overrides[project_assets.get_agent_service] = lambda: service

    response = TestClient(app).get("/api/assets/catalog/agents")

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "id": str(system_agent.id),
            "scope": "system",
            "project_id": None,
            "slug": system_agent.slug,
            "display_name": system_agent.display_name,
            "status": "active",
            "current_published_version_id": None,
            "version": 1,
            "created_by_user_id": system_agent.created_by_user_id,
            "created_at": system_agent.created_at.isoformat().replace("+00:00", "Z"),
            "updated_at": system_agent.updated_at.isoformat().replace("+00:00", "Z"),
        }
    ]
    assert response.json()["request_id"]
    actor = service.list_visible.await_args.args[0]
    assert actor.user_id == user_id
    assert type(actor).__name__ == "SystemAssetReadContext"


def _context(role: ProjectRole = ProjectRole.ADMIN) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=PROJECT_ID,
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-project-assets",
    )


def _agent(
    scope: AssetScope,
    *,
    current_published_version_id: uuid.UUID | None = None,
    description: str = "",
) -> AgentAssetView:
    return AgentAssetView(
        id=uuid.uuid4(),
        scope=scope,
        project_id=PROJECT_ID if scope is AssetScope.PROJECT else None,
        slug=f"{scope.value}-agent",
        display_name=f"{scope.value} agent",
        status="active",
        current_published_version_id=current_published_version_id,
        version=1,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
        updated_at=NOW,
        description=description,
    )


def _client(
    *,
    agent_service=None,
    binding_service=None,
    credential_service=None,
    mcp_service=None,
    skill_service=None,
) -> TestClient:
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.dependency_overrides[project_assets.project_asset_context] = _context
    if agent_service is not None:
        app.dependency_overrides[project_assets.get_agent_service] = lambda: agent_service
    if binding_service is not None:
        app.dependency_overrides[project_assets.get_binding_service] = lambda: binding_service
    if credential_service is not None:
        app.dependency_overrides[project_assets.get_credential_service] = lambda: credential_service
    if mcp_service is not None:
        app.dependency_overrides[project_assets.get_mcp_service] = lambda: mcp_service
    if skill_service is not None:
        app.dependency_overrides[project_assets.get_skill_service] = lambda: skill_service
    return TestClient(app)


def test_project_mcp_tool_inventory_returns_only_bounded_safe_metadata() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    service.get_tool_inventory.return_value = McpToolInventoryView(
        status="degraded",
        tools=(
            McpToolView(
                name="maps_weather",
                description="根据城市名称查询天气",
            ),
            McpToolView(
                name="maps_direction_driving",
                description="根据起终点规划驾车路线",
            ),
        ),
        last_attempt_at=NOW,
        last_success_at=NOW,
        error_code="mcp_discovery_unavailable",
    )

    response = _client(mcp_service=service).get(f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}/versions/{version_id}/tools")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "data": {
            "status": "degraded",
            "tools": [
                {
                    "name": "maps_weather",
                    "description": "根据城市名称查询天气",
                },
                {
                    "name": "maps_direction_driving",
                    "description": "根据起终点规划驾车路线",
                },
            ],
            "last_attempt_at": NOW.isoformat().replace("+00:00", "Z"),
            "last_success_at": NOW.isoformat().replace("+00:00", "Z"),
            "error_code": "mcp_discovery_unavailable",
        },
        "request_id": "req-project-assets",
    }
    service.get_tool_inventory.assert_awaited_once()
    actor, called_asset_id, called_version_id = service.get_tool_inventory.await_args.args
    assert actor.project_id == PROJECT_ID
    assert called_asset_id == asset_id
    assert called_version_id == version_id


def test_project_mcp_tool_inventory_exposes_active_discovery_as_testing() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    service.get_tool_inventory.return_value = McpToolInventoryView(
        status="testing",  # type: ignore[arg-type]
        tools=(
            McpToolView(
                name="maps_weather",
                description="Last successful observation",
            ),
        ),
        last_attempt_at=NOW,
        last_success_at=NOW,
        error_code=None,
    )

    response = _client(mcp_service=service).get(f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}/versions/{version_id}/tools")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "status": "testing",
        "tools": [
            {
                "name": "maps_weather",
                "description": "Last successful observation",
            }
        ],
        "last_attempt_at": NOW.isoformat().replace("+00:00", "Z"),
        "last_success_at": NOW.isoformat().replace("+00:00", "Z"),
        "error_code": None,
    }


def test_project_mcp_tool_discovery_post_enqueues_and_get_reads_attempt() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    attempt = SimpleNamespace(
        id=attempt_id,
        mcp_server_id=asset_id,
        mcp_server_version_id=version_id,
        status="queued",
        requested_at=NOW,
        started_at=None,
        completed_at=None,
        error_code=None,
    )
    service.request_tool_discovery.return_value = attempt
    service.get_tool_discovery_attempt.return_value = attempt
    client = _client(mcp_service=service)

    created = client.post(f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}/versions/{version_id}/tool-discovery")
    latest = client.get(f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}/versions/{version_id}/tool-discovery")
    exact = client.get(
        f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}/versions/{version_id}/tool-discovery",
        params={"attempt_id": str(attempt_id)},
    )

    expected = {
        "data": {
            "id": str(attempt_id),
            "mcp_server_id": str(asset_id),
            "mcp_server_version_id": str(version_id),
            "status": "queued",
            "requested_at": NOW.isoformat().replace("+00:00", "Z"),
            "started_at": None,
            "completed_at": None,
            "error_code": None,
        },
        "request_id": "req-project-assets",
    }
    assert created.status_code == 202
    assert created.json() == expected
    assert latest.status_code == 200
    assert latest.json() == expected
    assert exact.status_code == 200
    assert exact.json() == expected
    service.request_tool_discovery.assert_awaited_once()
    service.get_tool_discovery_attempt.assert_any_await(
        ANY,
        asset_id,
        version_id,
        attempt_id=None,
    )
    service.get_tool_discovery_attempt.assert_any_await(
        ANY,
        asset_id,
        version_id,
        attempt_id=attempt_id,
    )


def test_project_asset_list_separates_scopes() -> None:
    service = AsyncMock()
    binding_service = AsyncMock()
    published_version_id = uuid.uuid4()
    system_agent = _agent(
        AssetScope.SYSTEM,
        current_published_version_id=published_version_id,
    )
    project_agent = _agent(AssetScope.PROJECT)
    binding_service.list_visible.return_value = (
        SystemAssetBinding(
            project_id=PROJECT_ID,
            kind=AssetKind.AGENT,
            asset_id=system_agent.id,
            version_id=published_version_id,
            enabled=True,
            version=3,
            created_by_user_id=str(uuid.uuid4()),
            updated_by_user_id=str(uuid.uuid4()),
            created_at=NOW,
            updated_at=NOW,
        ),
    )
    service.list_visible.return_value = (system_agent, project_agent)

    response = _client(
        agent_service=service,
        binding_service=binding_service,
    ).get(f"/api/projects/{PROJECT_ID}/agents")

    assert response.status_code == 200
    assert set(response.json()) == {"system_items", "project_items", "request_id"}
    assert [item["scope"] for item in response.json()["system_items"]] == ["system"]
    assert [item["scope"] for item in response.json()["project_items"]] == ["project"]
    system_item = response.json()["system_items"][0]
    project_item = response.json()["project_items"][0]
    assert system_item["current_published_version_id"] == str(published_version_id)
    assert system_item["binding"]["version_id"] == str(published_version_id)
    assert system_item["binding"]["enabled"] is True
    assert system_item["binding"]["version"] == 3
    assert "shared_assets.manage_bindings" in system_item["capabilities"]
    assert "shared_assets.edit" not in system_item["capabilities"]
    assert "mcp.credentials.approve" not in system_item["capabilities"]
    assert project_item["binding"] is None
    assert "shared_assets.edit" in project_item["capabilities"]
    assert "shared_assets.manage_bindings" in project_item["capabilities"]
    service.list_visible.assert_awaited_once()
    actor = service.list_visible.await_args.args[0]
    assert actor.project_id == PROJECT_ID
    assert actor.request_id == "req-project-assets"
    binding_service.list_visible.assert_awaited_once_with(actor, AssetKind.AGENT)


def test_project_asset_list_keeps_unbound_system_assets_visible() -> None:
    service = AsyncMock()
    binding_service = AsyncMock()
    service.list_visible.return_value = (_agent(AssetScope.SYSTEM),)
    binding_service.list_visible.return_value = ()

    response = _client(
        agent_service=service,
        binding_service=binding_service,
    ).get(f"/api/projects/{PROJECT_ID}/agents")

    assert response.status_code == 200
    assert len(response.json()["system_items"]) == 1
    assert response.json()["system_items"][0]["binding"] is None


def test_project_asset_lists_include_current_published_descriptions() -> None:
    skill_service = AsyncMock()
    agent_service = AsyncMock()
    binding_service = AsyncMock()
    system_skill = SkillAssetView(
        id=uuid.uuid4(),
        scope=AssetScope.SYSTEM,
        project_id=None,
        slug="academic-paper-review",
        display_name="academic-paper-review",
        status="active",
        current_published_version_id=uuid.uuid4(),
        version=1,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
        updated_at=NOW,
        description="Review, analyze, critique, or summarize academic papers.",
    )
    skill_service.list_visible.return_value = (system_skill,)
    system_agent = _agent(
        AssetScope.SYSTEM,
        description="Review code changes and report actionable findings.",
    )
    agent_service.list_visible.return_value = (system_agent,)
    binding_service.list_visible.return_value = ()
    client = _client(
        agent_service=agent_service,
        binding_service=binding_service,
        skill_service=skill_service,
    )

    skill_response = client.get(f"/api/projects/{PROJECT_ID}/skills")
    agent_response = client.get(f"/api/projects/{PROJECT_ID}/agents")

    assert skill_response.status_code == 200
    assert skill_response.json()["system_items"][0]["description"] == system_skill.description
    assert agent_response.status_code == 200
    assert agent_response.json()["system_items"][0]["description"] == system_agent.description


def test_project_asset_capabilities_expose_suspend_only_to_effective_admins() -> None:
    admin_capabilities = project_assets._asset_item_capabilities(
        _context(ProjectRole.ADMIN),
        AssetScope.PROJECT,
        AssetKind.AGENT,
    )
    editor_capabilities = project_assets._asset_item_capabilities(
        _context(ProjectRole.EDITOR),
        AssetScope.PROJECT,
        AssetKind.AGENT,
    )

    assert "shared_assets.edit" in admin_capabilities
    assert "shared_assets.manage_bindings" in admin_capabilities
    assert "shared_assets.edit" in editor_capabilities
    assert "shared_assets.manage_bindings" not in editor_capabilities


def test_project_mcp_and_credential_capabilities_are_scope_effective() -> None:
    mcp_service = AsyncMock()
    binding_service = AsyncMock()
    credential_service = AsyncMock()
    system_mcp = McpAssetView(**{key: value for key, value in vars(_agent(AssetScope.SYSTEM)).items() if key != "description"})
    project_mcp = McpAssetView(**{key: value for key, value in vars(_agent(AssetScope.PROJECT)).items() if key != "description"})
    mcp_service.list_visible.return_value = (system_mcp, project_mcp)
    binding_service.list_visible.return_value = ()

    credential_base = {
        "id": uuid.uuid4(),
        "name": "github",
        "display_name": "GitHub",
        "credential_type": "token",
        "status": "active",
        "current_version_id": uuid.uuid4(),
        "version": 1,
        "created_by_user_id": str(uuid.uuid4()),
        "created_at": NOW,
        "updated_at": NOW,
    }
    credential_service.list_visible.return_value = (
        CredentialView(
            **credential_base,
            scope=AssetScope.SYSTEM,
            project_id=None,
        ),
        CredentialView(
            **{**credential_base, "id": uuid.uuid4()},
            scope=AssetScope.PROJECT,
            project_id=PROJECT_ID,
        ),
    )
    client = _client(
        mcp_service=mcp_service,
        binding_service=binding_service,
        credential_service=credential_service,
    )

    mcp_response = client.get(f"/api/projects/{PROJECT_ID}/mcp-servers")
    credential_response = client.get(f"/api/projects/{PROJECT_ID}/credentials")

    assert "mcp.credentials.approve" not in mcp_response.json()["system_items"][0]["capabilities"]
    assert "mcp.credentials.approve" in mcp_response.json()["project_items"][0]["capabilities"]
    assert credential_response.json()["system_items"][0]["capabilities"] == ["shared_assets.read"]
    assert "mcp.credentials.approve" in credential_response.json()["project_items"][0]["capabilities"]


def test_project_asset_version_history_returns_typed_envelope() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    version = AgentVersionView(
        id=uuid.uuid4(),
        agent_id=asset_id,
        version_number=2,
        workflow_status="published",
        description="Review changes",
        soul="Be precise",
        model_ref="default",
        tool_groups=("web",),
        skill_version_ids=(),
        mcp_version_ids=(),
        supersedes_version_id=None,
        payload_checksum="a" * 64,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
    )
    service.get_version_history.return_value = (version,)

    response = _client(agent_service=service).get(f"/api/projects/{PROJECT_ID}/agents/{asset_id}/versions")

    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {
                **response.json()["data"][0],
                "id": str(version.id),
                "agent_id": str(asset_id),
                "version_number": 2,
                "workflow_status": "published",
            }
        ],
        "request_id": "req-project-assets",
    }
    service.get_version_history.assert_awaited_once()
    actor, requested_asset_id = service.get_version_history.await_args.args
    assert actor.project_id == PROJECT_ID
    assert actor.request_id == "req-project-assets"
    assert requested_asset_id == asset_id


def test_project_agent_manual_version_mutation_routes_are_not_public() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    client = _client(agent_service=service)

    create = client.post(
        f"/api/projects/{PROJECT_ID}/agents/{asset_id}/versions",
        json={},
    )
    publish = client.post(
        f"/api/projects/{PROJECT_ID}/agents/{asset_id}/versions/{version_id}/publish",
        json={},
    )

    assert create.status_code == 405
    assert publish.status_code == 404
    service.create_version.assert_not_awaited()
    service.publish.assert_not_awaited()


def test_project_agent_instructions_route_saves_all_virtual_files_atomically() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    version = AgentVersionView(
        id=uuid.uuid4(),
        agent_id=asset_id,
        version_number=3,
        workflow_status=WorkflowStatus.PUBLISHED,
        description="Runtime configuration",
        soul="# Soul",
        model_ref="default",
        tool_groups=("web",),
        skill_version_ids=(),
        mcp_version_ids=(),
        supersedes_version_id=uuid.uuid4(),
        payload_checksum="a" * 64,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
        agents_instructions="# Agents",
        identity="# Identity",
        user_context="# User",
        payload_schema_version=2,
    )
    service.update_instructions.return_value = version
    body = {
        "agents_instructions": "# Agents",
        "soul": "# Soul",
        "identity": "# Identity",
        "user_context": "# User",
        "expected_asset_version": 4,
    }

    response = _client(agent_service=service).put(
        f"/api/projects/{PROJECT_ID}/agents/{asset_id}/instructions",
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["data"]["agents_instructions"] == "# Agents"
    assert response.json()["data"]["soul"] == "# Soul"
    assert response.json()["data"]["identity"] == "# Identity"
    assert response.json()["data"]["user_context"] == "# User"
    assert response.json()["data"]["payload_schema_version"] == 2
    actor, selected_asset_id, instructions = service.update_instructions.await_args.args
    assert actor.project_id == PROJECT_ID
    assert selected_asset_id == asset_id
    assert dataclasses.asdict(instructions) == {
        "agents_instructions": "# Agents",
        "soul": "# Soul",
        "identity": "# Identity",
        "user_context": "# User",
    }
    assert service.update_instructions.await_args.kwargs == {"expected_asset_version": 4}

    invalid = _client(agent_service=service).put(
        f"/api/projects/{PROJECT_ID}/agents/{asset_id}/instructions",
        json={**body, "unexpected": True},
    )
    assert invalid.status_code == 422


def _skill_version(asset_id: uuid.UUID, *, source_id: uuid.UUID | None = None) -> SkillVersionView:
    return SkillVersionView(
        id=uuid.uuid4(),
        skill_id=asset_id,
        version_number=2,
        workflow_status=WorkflowStatus.DRAFT,
        description="Updated skill",
        frontmatter={"name": "updated-skill", "description": "Updated skill"},
        compatibility=None,
        secret_requirements=(),
        scan_decision="allow",
        scan_rule_ids=(),
        scan_summary={"rule_ids": [], "severity_counts": {}},
        file_views=(),
        supersedes_version_id=source_id,
        payload_checksum="b" * 64,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
    )


def test_project_skill_create_uses_atomic_template_service_and_keeps_asset_envelope() -> None:
    service = AsyncMock()
    asset = SkillAssetView(
        id=uuid.uuid4(),
        scope=AssetScope.PROJECT,
        project_id=PROJECT_ID,
        slug="meeting-brief",
        display_name="Meeting Brief",
        status="suspended",
        current_published_version_id=None,
        version=2,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
        updated_at=NOW,
        description="",
    )
    service.create_project_with_template.return_value = asset

    response = _client(skill_service=service).post(
        f"/api/projects/{PROJECT_ID}/skills",
        json={
            "slug": "meeting-brief",
            "display_name": "Meeting Brief",
        },
    )

    assert response.status_code == 201
    assert set(response.json()) == {"item", "request_id"}
    assert response.json()["item"]["id"] == str(asset.id)
    assert response.json()["item"]["status"] == "suspended"
    assert response.json()["item"]["current_published_version_id"] is None
    assert response.json()["item"]["version"] == 2
    actor, command = service.create_project_with_template.await_args.args
    assert actor.project_id == PROJECT_ID
    assert actor.request_id == "req-project-assets"
    assert command.slug == "meeting-brief"
    assert command.display_name == "Meeting Brief"
    service.create_asset.assert_not_awaited()


def test_project_skill_archive_upload_returns_created_suspended_skill_and_published_version() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    version = dataclasses.replace(
        _skill_version(asset_id),
        version_number=1,
        workflow_status=WorkflowStatus.PUBLISHED,
        description="Imported skill",
    )
    asset = SkillAssetView(
        id=asset_id,
        scope=AssetScope.PROJECT,
        project_id=PROJECT_ID,
        slug="imported-skill",
        display_name="imported-skill",
        status="suspended",
        current_published_version_id=version.id,
        version=3,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
        updated_at=NOW,
        description=version.description,
    )
    service.create_project_from_archive_upload.return_value = ProjectSkillArchiveCreateResult(
        asset=asset,
        version=version,
    )

    response = _client(skill_service=service).post(
        f"/api/projects/{PROJECT_ID}/skills/import",
        files={
            "archive": (
                "imported-skill.tar.gz",
                b"compressed-package",
                "application/gzip",
            )
        },
    )

    assert response.status_code == 201
    assert set(response.json()) == {"item", "version", "request_id"}
    assert response.json()["item"]["status"] == "suspended"
    assert response.json()["item"]["current_published_version_id"] == str(
        version.id,
    )
    assert response.json()["version"]["workflow_status"] == "published"
    actor, payload = service.create_project_from_archive_upload.await_args.args
    assert actor.project_id == PROJECT_ID
    assert payload == b"compressed-package"
    assert service.create_project_from_archive_upload.await_args.kwargs == {
        "filename": "imported-skill.tar.gz",
    }


def test_project_skill_archive_upload_validation_failure_has_stable_422_contract() -> None:
    service = AsyncMock()
    service.create_project_from_archive_upload.side_effect = AssetValidationFailed("req-project-assets")

    response = _client(skill_service=service).post(
        f"/api/projects/{PROJECT_ID}/skills/import",
        files={
            "archive": (
                "unsupported.rar",
                b"not-a-supported-package",
                "application/octet-stream",
            )
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "asset_validation_failed",
            "message": "Asset validation failed",
            "request_id": "req-project-assets",
        }
    }


def test_project_skill_file_content_preview_is_typed_and_never_cached() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    service.preview_version_file.return_value = SkillFileContentView(
        path="SKILL.md",
        media_type="text/markdown",
        size_bytes=12,
        sha256="a" * 64,
        preview_status="ready",
        encoding="utf-8",
        content="# Skill\n",
        source_payload_checksum="b" * 64,
        asset_version=3,
    )

    response = _client(skill_service=service).get(
        f"/api/projects/{PROJECT_ID}/skills/{asset_id}/versions/{version_id}/files/content",
        params={"path": "SKILL.md"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json() == {
        "data": {
            "path": "SKILL.md",
            "media_type": "text/markdown",
            "size_bytes": 12,
            "sha256": "a" * 64,
            "preview_status": "ready",
            "encoding": "utf-8",
            "content": "# Skill\n",
            "source_payload_checksum": "b" * 64,
            "asset_version": 3,
        },
        "request_id": "req-project-assets",
    }
    service.preview_version_file.assert_awaited_once_with(
        service.preview_version_file.await_args.args[0],
        asset_id,
        version_id,
        "SKILL.md",
    )


def test_project_skill_fork_route_uses_strict_discriminated_changes() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    source_id = uuid.uuid4()
    service.fork_version.return_value = _skill_version(asset_id, source_id=source_id)
    body = {
        "expected_asset_version": 3,
        "expected_source_payload_checksum": "a" * 64,
        "changes": [
            {
                "op": "replace",
                "path": "SKILL.md",
                "content": "---\nname: updated-skill\ndescription: Updated skill\n---\n",
                "media_type": "text/markdown",
            },
            {
                "op": "create",
                "path": "references/guide.md",
                "content": "Guide\n",
                "media_type": "text/markdown",
            },
            {"op": "delete", "path": "references/old.md"},
        ],
    }

    response = _client(skill_service=service).post(
        f"/api/projects/{PROJECT_ID}/skills/{asset_id}/versions/{source_id}/fork",
        json=body,
    )

    assert response.status_code == 201
    assert response.json()["data"]["workflow_status"] == "draft"
    args = service.fork_version.await_args.args
    assert args[1:3] == (asset_id, source_id)
    changes = args[3]
    assert [(item.op, item.path) for item in changes] == [
        ("replace", "SKILL.md"),
        ("create", "references/guide.md"),
        ("delete", "references/old.md"),
    ]
    assert service.fork_version.await_args.kwargs == {
        "expected_asset_version": 3,
        "expected_source_payload_checksum": "a" * 64,
    }

    invalid = _client(skill_service=service).post(
        f"/api/projects/{PROJECT_ID}/skills/{asset_id}/versions/{source_id}/fork",
        json={**body, "unexpected": True},
    )
    assert invalid.status_code == 422


def test_project_skill_delete_returns_204_and_forwards_expected_revision() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()

    response = _client(skill_service=service).request(
        "DELETE",
        f"/api/projects/{PROJECT_ID}/skills/{asset_id}",
        json={"expected_asset_version": 7},
    )

    assert response.status_code == 204
    assert response.content == b""
    service.delete.assert_awaited_once()
    actor, selected_asset_id = service.delete.await_args.args
    assert actor.project_id == PROJECT_ID
    assert selected_asset_id == asset_id
    assert service.delete.await_args.kwargs == {"expected_asset_version": 7}


def test_project_agent_delete_returns_204_and_forwards_expected_revision() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()

    response = _client(agent_service=service).request(
        "DELETE",
        f"/api/projects/{PROJECT_ID}/agents/{asset_id}",
        json={"expected_asset_version": 9},
    )

    assert response.status_code == 204
    assert response.content == b""
    service.delete.assert_awaited_once()
    actor, selected_asset_id = service.delete.await_args.args
    assert actor.project_id == PROJECT_ID
    assert selected_asset_id == asset_id
    assert service.delete.await_args.kwargs == {"expected_asset_version": 9}


def test_project_skill_activate_route_forwards_expected_revision() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    activated = SkillAssetView(
        id=asset_id,
        scope=AssetScope.PROJECT,
        project_id=PROJECT_ID,
        slug="activated-skill",
        display_name="Activated Skill",
        status="active",
        current_published_version_id=uuid.uuid4(),
        version=4,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
        updated_at=NOW,
    )
    service.activate.return_value = activated

    response = _client(skill_service=service).post(
        f"/api/projects/{PROJECT_ID}/skills/{asset_id}/activate",
        json={"expected_asset_version": 3},
    )

    assert response.status_code == 200
    assert response.json()["item"]["status"] == "active"
    service.activate.assert_awaited_once()
    actor, selected_asset_id = service.activate.await_args.args
    assert actor.project_id == PROJECT_ID
    assert selected_asset_id == asset_id
    assert service.activate.await_args.kwargs == {"expected_asset_version": 3}


def test_project_agent_activate_route_forwards_expected_revision() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    activated = dataclasses.replace(
        _agent(
            AssetScope.PROJECT,
            current_published_version_id=uuid.uuid4(),
        ),
        id=asset_id,
        status="active",
        version=4,
    )
    service.activate.return_value = activated

    response = _client(agent_service=service).post(
        f"/api/projects/{PROJECT_ID}/agents/{asset_id}/activate",
        json={"expected_asset_version": 3},
    )

    assert response.status_code == 200
    assert response.json()["item"]["status"] == "active"
    actor, selected_asset_id = service.activate.await_args.args
    assert actor.project_id == PROJECT_ID
    assert selected_asset_id == asset_id
    assert service.activate.await_args.kwargs == {
        "expected_asset_version": 3,
    }


def test_project_skill_archive_is_not_exposed() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    client = _client(skill_service=service)

    response = client.post(
        f"/api/projects/{PROJECT_ID}/skills/{asset_id}/archive",
        json={"expected_asset_version": 1},
    )

    assert response.status_code == 404
    service.archive.assert_not_awaited()
    paths = client.app.openapi()["paths"]
    assert "/api/projects/{project_id}/skills/{asset_id}/archive" not in paths
    assert "/api/projects/{project_id}/skills/{asset_id}/activate" in paths
    assert "/api/projects/{project_id}/skills/{asset_id}/suspend" in paths


def test_project_agent_archive_is_not_exposed_without_removing_other_status_routes() -> None:
    agent_service = AsyncMock()
    mcp_service = AsyncMock()
    asset_id = uuid.uuid4()
    client = _client(
        agent_service=agent_service,
        mcp_service=mcp_service,
    )

    response = client.post(
        f"/api/projects/{PROJECT_ID}/agents/{asset_id}/archive",
        json={"expected_asset_version": 1},
    )

    assert response.status_code == 404
    agent_service.archive.assert_not_awaited()
    paths = client.app.openapi()["paths"]
    assert "/api/projects/{project_id}/agents/{asset_id}/archive" not in paths
    assert "/api/projects/{project_id}/agents/{asset_id}/activate" in paths
    assert "/api/projects/{project_id}/agents/{asset_id}/suspend" in paths
    assert "/api/projects/{project_id}/mcp-servers/{asset_id}/archive" in paths
    assert "/api/projects/{project_id}/mcp-servers/{asset_id}/suspend" in paths


def _mcp_version_with_read_only_mappings(asset_id: uuid.UUID) -> McpVersionView:
    return McpVersionView(
        id=uuid.uuid4(),
        mcp_server_id=asset_id,
        version_number=2,
        workflow_status=WorkflowStatus.PENDING_APPROVAL,
        definition=McpDefinition(
            description="Analytics tools",
            transport="streamable_http",
            command="must-never-return-command",
            args=("must-never-return-arg",),
            url="https://analytics.example.test/mcp",
            env=MappingProxyType({"OPAQUE_SETTING": "must-never-return"}),
            headers=MappingProxyType({"X-Client": "must-never-return"}),
            oauth=MappingProxyType(
                {
                    "extra_token_params": {
                        "client_assertion": "must-never-return-oauth",
                    }
                }
            ),
            routing=MappingProxyType({"strategy": "must-never-return-routing"}),
            tool_overrides=MappingProxyType({"search": {"value": "must-never-return-override"}}),
            timeout_seconds=45,
            credential_slots=(
                project_assets.McpCredentialSlot(
                    name="api-key",
                    purpose="Authenticate analytics requests",
                    payload_schema=MappingProxyType({"headers": ("Authorization",)}),
                ),
            ),
        ),
        credential_slots=(
            McpCredentialSlotView(
                id=uuid.uuid4(),
                name="api-key",
                purpose="Authenticate analytics requests",
                payload_schema=MappingProxyType({"headers": ("Authorization",)}),
                required=True,
            ),
        ),
        credential_grants=(),
        supersedes_version_id=None,
        payload_checksum="b" * 64,
        submitted_at=NOW,
        reviewed_at=None,
        reviewed_by_user_id=None,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
    )


def test_project_mcp_version_history_serializes_read_only_mapping_fields() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    version = _mcp_version_with_read_only_mappings(asset_id)
    service.get_version_history.return_value = (version,)

    response = _client(mcp_service=service).get(f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}/versions")

    assert response.status_code == 200
    assert response.json()["data"][0]["definition"]["env"] == {}
    assert response.json()["data"][0]["definition"]["headers"] == {}
    assert response.json()["data"][0]["definition"]["command"] is None
    assert response.json()["data"][0]["definition"]["args"] == []
    assert response.json()["data"][0]["definition"]["oauth"] == {}
    assert response.json()["data"][0]["definition"]["routing"] == {}
    assert response.json()["data"][0]["definition"]["tool_overrides"] == {}
    assert "must-never-return" not in response.text
    assert response.json()["data"][0]["credential_slots"][0]["payload_schema"] == {"headers": ["Authorization"]}
    assert response.json()["data"][0]["workflow_status"] == "pending_approval"


def test_project_mcp_history_never_replays_signed_endpoint_details() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    version = _mcp_version_with_read_only_mappings(asset_id)
    signed_marker = "must-never-return-signed-query"
    version = dataclasses.replace(
        version,
        definition=dataclasses.replace(
            version.definition,
            url=(f"https://analytics.example.test/private/path?X-Amz-Signature={signed_marker}"),
        ),
    )
    service.get_version_history.return_value = (version,)

    response = _client(mcp_service=service).get(f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}/versions")

    assert response.status_code == 200
    assert response.json()["data"][0]["definition"]["url"] == ("https://analytics.example.test")
    assert signed_marker not in response.text
    assert "/private/path" not in response.text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:8771/api/mcp?mode=read", "http://localhost:8771"),
        ("http://127.0.0.1:8772/api/mcp", "http://127.0.0.1:8772"),
        ("http://[::1]:8773/api/mcp", "http://[::1]:8773"),
        ("https://mcp.internal/api/mcp", "https://mcp.internal"),
    ],
)
def test_project_mcp_url_redaction_preserves_http_or_https_origin(
    url: str,
    expected: str,
) -> None:
    assert project_assets._redacted_project_mcp_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "http://127.0.0.1:8771/api/mcp",
            "http://127.0.0.1:8771/api/mcp",
        ),
        (
            "http://[::1]:8772/private/mcp/",
            "http://[::1]:8772/private/mcp/",
        ),
    ],
)
def test_project_mcp_editable_url_preserves_safe_ip_literal_path(
    url: str,
    expected: str,
) -> None:
    assert project_assets._editable_project_mcp_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "http://127.0.0.1:8771/api/mcp?token=must-never-return",
            "http://127.0.0.1:8771",
        ),
        (
            "http://user:password@127.0.0.1:8771/api/mcp",
            None,
        ),
        (
            "http://127.0.0.1:8771/api/mcp#must-never-return",
            None,
        ),
        (
            "https://mcp.internal/api/mcp",
            "https://mcp.internal",
        ),
    ],
)
def test_project_mcp_editable_url_falls_back_to_origin_only_for_unsafe_or_non_ip_urls(
    url: str,
    expected: str | None,
) -> None:
    assert project_assets._editable_project_mcp_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost:8771/api/mcp",
        "http://user:password@localhost:8771/api/mcp",
        "http://localhost:8771/api/mcp#fragment",
        "http://localhost:8771/api/mcp#",
        "http:///api/mcp",
        "http://localhost:/api/mcp",
        "http://localhost:0/api/mcp",
        "http://localhost:65536/api/mcp",
        "http://localhost:not-a-port/api/mcp",
    ],
)
def test_project_mcp_url_redaction_rejects_invalid_origins(url: str) -> None:
    assert project_assets._redacted_project_mcp_url(url) is None


def test_project_mcp_version_mutation_serializes_after_committing_domain_result() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    version = _mcp_version_with_read_only_mappings(asset_id)
    service.create_version.return_value = version

    response = _client(mcp_service=service).post(
        f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}/versions",
        json={
            "description": "Analytics tools",
            "transport": "http",
            "command": None,
            "args": [],
            "url": "https://analytics.example.test/mcp",
            "env": {},
            "headers": {"X-Client": "deerflow"},
            "oauth": {},
            "routing": {},
            "tool_overrides": {},
            "timeout_seconds": 45,
            "credential_slots": [
                {
                    "name": "api-key",
                    "purpose": "Authenticate analytics requests",
                    "payload_schema": {"headers": ["Authorization"]},
                    "required": True,
                }
            ],
            "expected_asset_version": 1,
        },
    )

    assert response.status_code == 201
    assert response.json()["data"]["definition"]["env"] == {}
    assert response.json()["data"]["definition"]["headers"] == {}
    assert response.json()["data"]["definition"]["command"] is None
    assert response.json()["data"]["definition"]["args"] == []
    assert response.json()["data"]["definition"]["oauth"] == {}
    assert response.json()["data"]["definition"]["routing"] == {}
    assert response.json()["data"]["definition"]["tool_overrides"] == {}
    assert "must-never-return" not in response.text
    assert response.json()["data"]["credential_slots"][0]["payload_schema"] == {"headers": ["Authorization"]}


def test_project_configured_mcp_route_accepts_one_strict_flat_request() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    version = _mcp_version_with_read_only_mappings(asset_id)
    service.create_project_configured.return_value = SimpleNamespace(
        asset=McpAssetView(
            id=asset_id,
            scope=AssetScope.PROJECT,
            project_id=PROJECT_ID,
            slug="analytics-mcp",
            display_name="Analytics MCP",
            status="active",
            current_published_version_id=None,
            version=3,
            created_by_user_id=str(uuid.uuid4()),
            created_at=NOW,
            updated_at=NOW,
        ),
        version=version,
    )
    client = _client(mcp_service=service)
    body = {
        "slug": "analytics-mcp",
        "display_name": "Analytics MCP",
        "description": "Analytics tools",
        "transport": "http",
        "command": None,
        "args": [],
        "url": "https://analytics.example.test/mcp",
        "env": {},
        "headers": {"X-Client": "deerflow"},
        "oauth": {},
        "routing": {},
        "tool_overrides": {},
        "timeout_seconds": 45,
        "credential_slots": [
            {
                "name": "api-key",
                "purpose": "Authenticate analytics requests",
                "payload_schema": {"headers": ["Authorization"]},
                "required": True,
            }
        ],
    }

    response = client.post(
        f"/api/projects/{PROJECT_ID}/mcp-servers/configured",
        json=body,
    )

    assert response.status_code == 201
    assert response.json()["item"]["id"] == str(asset_id)
    assert response.json()["item"]["version"] == 3
    assert response.json()["version"]["mcp_server_id"] == str(asset_id)
    assert response.json()["version"]["workflow_status"] == "pending_approval"
    assert response.json()["version"]["definition"]["url"] == "https://analytics.example.test"
    assert response.json()["version"]["definition"]["headers"] == {}
    assert "must-never-return" not in response.text
    service.create_project_configured.assert_awaited_once()
    actor, command, definition = service.create_project_configured.await_args.args
    assert actor.project_id == PROJECT_ID
    assert command == project_assets.CreateMcpServer(
        "analytics-mcp",
        "Analytics MCP",
    )
    assert definition.description == "Analytics tools"
    assert definition.transport == "http"
    assert definition.url == "https://analytics.example.test/mcp"
    assert definition.headers == {"X-Client": "deerflow"}
    assert [slot.name for slot in definition.credential_slots] == ["api-key"]

    rejected = client.post(
        f"/api/projects/{PROJECT_ID}/mcp-servers/configured",
        json={**body, "expected_asset_version": 1},
    )

    assert rejected.status_code == 422
    assert service.create_project_configured.await_count == 1


def test_project_configured_mcp_update_route_accepts_definition_and_exact_revision_only() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    editable_url = "http://127.0.0.1:8771/new-mcp"
    version = dataclasses.replace(
        _mcp_version_with_read_only_mappings(asset_id),
        workflow_status=WorkflowStatus.PUBLISHED,
        definition=dataclasses.replace(
            _mcp_version_with_read_only_mappings(asset_id).definition,
            url=editable_url,
        ),
        credential_slots=(),
        submitted_at=None,
    )
    service.update_project_configured.return_value = SimpleNamespace(
        asset=McpAssetView(
            id=asset_id,
            scope=AssetScope.PROJECT,
            project_id=PROJECT_ID,
            slug="analytics-mcp",
            display_name="Analytics MCP",
            status="active",
            current_published_version_id=version.id,
            version=9,
            created_by_user_id=str(uuid.uuid4()),
            created_at=NOW,
            updated_at=NOW,
        ),
        version=version,
    )
    client = _client(mcp_service=service)
    body = {
        "description": "Updated analytics tools",
        "transport": "http",
        "command": None,
        "args": [],
        "url": editable_url,
        "env": {},
        "headers": {},
        "oauth": {},
        "routing": {},
        "tool_overrides": {},
        "timeout_seconds": 60,
        "credential_slots": [],
        "expected_asset_version": 7,
    }

    response = client.put(
        f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}/configured",
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["item"]["version"] == 9
    assert response.json()["version"]["workflow_status"] == "published"
    assert response.json()["version"]["definition"]["url"] == editable_url
    service.update_project_configured.assert_awaited_once()
    actor, called_asset_id, definition = service.update_project_configured.await_args.args
    assert actor.project_id == PROJECT_ID
    assert called_asset_id == asset_id
    assert definition.description == "Updated analytics tools"
    assert definition.url == editable_url
    assert service.update_project_configured.await_args.kwargs == {
        "expected_asset_version": 7,
    }

    for forbidden_field in (
        "slug",
        "display_name",
        "workflow_status",
        "credential_versions",
        "version_id",
    ):
        rejected = client.put(
            f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}/configured",
            json={**body, forbidden_field: "forbidden"},
        )
        assert rejected.status_code == 422
    assert service.update_project_configured.await_count == 1


def test_project_configured_mcp_get_returns_only_the_current_validated_editable_path() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    version = dataclasses.replace(
        _mcp_version_with_read_only_mappings(asset_id),
        workflow_status=WorkflowStatus.PUBLISHED,
        definition=dataclasses.replace(
            _mcp_version_with_read_only_mappings(asset_id).definition,
            url="http://127.0.0.1:8771/api/mcp",
        ),
        credential_slots=(),
        submitted_at=None,
    )
    asset = McpAssetView(
        id=asset_id,
        scope=AssetScope.PROJECT,
        project_id=PROJECT_ID,
        slug="analytics-mcp",
        display_name="Analytics MCP",
        status="active",
        current_published_version_id=version.id,
        version=9,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
        updated_at=NOW,
    )
    service.get_project_configured.return_value = SimpleNamespace(
        asset=asset,
        version=version,
    )

    response = _client(mcp_service=service).get(
        f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}/configured",
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["item"]["id"] == str(asset_id)
    assert response.json()["version"]["id"] == str(version.id)
    assert response.json()["version"]["definition"]["url"] == ("http://127.0.0.1:8771/api/mcp")
    assert response.json()["version"]["definition"]["headers"] == {}
    actor, selected_asset_id = service.get_project_configured.await_args.args
    assert actor.project_id == PROJECT_ID
    assert selected_asset_id == asset_id


def test_configured_mcp_route_is_project_only_in_openapi() -> None:
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.include_router(admin_assets.admin_router)
    app.include_router(admin_assets.admin_project_router)
    openapi = app.openapi()
    path = "/api/projects/{project_id}/mcp-servers/configured"

    assert openapi["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/McpConfiguredRequest",
    }
    assert openapi["paths"][path]["post"]["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/McpConfiguredResponse",
    }
    request_schema = openapi["components"]["schemas"]["McpConfiguredRequest"]
    assert request_schema["additionalProperties"] is False
    assert {"slug", "display_name"}.issubset(request_schema["properties"])
    assert "expected_asset_version" not in request_schema["properties"]
    assert "/api/admin/assets/mcp-servers/configured" not in openapi["paths"]
    assert "/api/admin/projects/{project_id}/assets/mcp-servers/configured" not in openapi["paths"]
    update_path = "/api/projects/{project_id}/mcp-servers/{asset_id}/configured"
    assert openapi["paths"][update_path]["put"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/McpVersionRequest",
    }
    assert openapi["paths"][update_path]["put"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/McpConfiguredResponse",
    }
    assert "/api/admin/assets/mcp-servers/{asset_id}/configured" not in openapi["paths"]
    assert "/api/admin/projects/{project_id}/assets/mcp-servers/{asset_id}/configured" not in openapi["paths"]


def test_project_mcp_activate_and_delete_routes_forward_strict_expected_revision() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    current_version_id = uuid.uuid4()
    service.activate.return_value = McpAssetView(
        id=asset_id,
        scope=AssetScope.PROJECT,
        project_id=PROJECT_ID,
        slug="managed-mcp",
        display_name="Managed MCP",
        status="active",
        current_published_version_id=current_version_id,
        version=5,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
        updated_at=NOW,
    )
    client = _client(mcp_service=service)

    activate = client.post(
        f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}/activate",
        json={"expected_asset_version": 4},
    )
    deleted = client.request(
        "DELETE",
        f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}",
        json={"expected_asset_version": 5},
    )
    invalid = client.request(
        "DELETE",
        f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}",
        json={"expected_asset_version": 5, "unexpected": True},
    )

    assert activate.status_code == 200
    assert activate.json()["item"]["status"] == "active"
    assert deleted.status_code == 204
    assert not deleted.content
    assert invalid.status_code == 422
    service.activate.assert_awaited_once()
    assert service.activate.await_args.kwargs == {"expected_asset_version": 4}
    service.delete.assert_awaited_once()
    assert service.delete.await_args.kwargs == {"expected_asset_version": 5}


def test_mcp_delete_route_is_project_only_in_openapi() -> None:
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.include_router(admin_assets.admin_router)
    app.include_router(admin_assets.admin_project_router)
    paths = app.openapi()["paths"]

    project_path = paths["/api/projects/{project_id}/mcp-servers/{asset_id}"]
    assert project_path["delete"]["responses"]["204"]["description"] == "Successful Response"
    assert project_path["delete"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ExpectedAssetVersionRequest",
    }
    assert "delete" not in paths["/api/admin/assets/mcp-servers/{asset_id}"]
    assert "delete" not in paths["/api/admin/projects/{project_id}/assets/mcp-servers/{asset_id}"]


@pytest.mark.parametrize("transport", ("stdio", "streamable_http"))
def test_project_mcp_version_request_rejects_non_remote_supported_transport(
    transport: str,
) -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()

    response = _client(mcp_service=service).post(
        f"/api/projects/{PROJECT_ID}/mcp-servers/{asset_id}/versions",
        json={
            "transport": transport,
            "command": "marker-command" if transport == "stdio" else None,
            "args": [],
            "url": None if transport == "stdio" else "https://mcp.example.test/api",
            "expected_asset_version": 1,
        },
    )

    assert response.status_code == 422
    service.create_version.assert_not_awaited()


def test_version_routes_register_kind_specific_strict_openapi_contracts() -> None:
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.include_router(admin_assets.admin_router)
    app.include_router(admin_assets.admin_project_router)
    openapi = app.openapi()
    prefixes = (
        "/api/projects/{project_id}",
        "/api/admin/assets",
        "/api/admin/projects/{project_id}/assets",
    )
    mutable_prefixes = (
        "/api/projects/{project_id}",
        "/api/admin/projects/{project_id}/assets",
    )
    history_models = {
        "agents": "AgentVersionHistoryResponse",
        "skills": "SkillVersionHistoryResponse",
        "mcp-servers": "McpVersionHistoryResponse",
        "credentials": "CredentialVersionHistoryResponse",
    }
    version_models = {
        "skills": "SkillVersionResponse",
        "mcp-servers": "McpVersionResponse",
    }

    for prefix in prefixes:
        for segment, model_name in history_models.items():
            response_schema = openapi["paths"][f"{prefix}/{segment}/{{asset_id}}/versions" if segment != "credentials" else f"{prefix}/{segment}/{{credential_id}}/versions"]["get"]["responses"]["200"]["content"]["application/json"][
                "schema"
            ]
            assert response_schema == {"$ref": f"#/components/schemas/{model_name}"}
        credential_replace = openapi["paths"][f"{prefix}/credentials/{{credential_id}}/replace"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert credential_replace == {"$ref": "#/components/schemas/CredentialVersionResponse"}
        credential_migration = openapi["paths"][f"{prefix}/credentials/{{credential_id}}/migrate-grants"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert credential_migration == {"$ref": "#/components/schemas/CredentialGrantMigrationResponse"}
        credential_delete = openapi["paths"][f"{prefix}/credentials/{{credential_id}}"]["delete"]
        assert credential_delete["responses"]["204"]["description"] == "Successful Response"
        assert credential_delete["requestBody"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/CredentialDeleteRequest"}

    for prefix in mutable_prefixes:
        agent_versions_path = f"{prefix}/agents/{{asset_id}}/versions"
        assert "post" not in openapi["paths"][agent_versions_path]
        assert f"{prefix}/agents/{{asset_id}}/versions/{{version_id}}/publish" not in openapi["paths"]
        agent_instructions_schema = openapi["paths"][f"{prefix}/agents/{{asset_id}}/instructions"]["put"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert agent_instructions_schema == {"$ref": "#/components/schemas/AgentVersionResponse"}
        for segment, model_name in version_models.items():
            create_schema = openapi["paths"][f"{prefix}/{segment}/{{asset_id}}/versions"]["post"]["responses"]["201"]["content"]["application/json"]["schema"]
            publish_schema = openapi["paths"][f"{prefix}/{segment}/{{asset_id}}/versions/{{version_id}}/publish"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
            expected = {"$ref": f"#/components/schemas/{model_name}"}
            assert create_schema == expected
            assert publish_schema == expected

    components = openapi["components"]["schemas"]
    assert "AgentVersionRequest" not in components
    for model_name in (*history_models.values(), *version_models.values(), "AgentVersionResponse"):
        assert components[model_name]["additionalProperties"] is False
    preview_path = openapi["paths"]["/api/projects/{project_id}/skills/{asset_id}/versions/{version_id}/files/content"]
    assert preview_path["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/SkillFileContentResponse"}
    fork_path = openapi["paths"]["/api/projects/{project_id}/skills/{asset_id}/versions/{source_version_id}/fork"]
    assert fork_path["post"]["responses"]["201"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/SkillVersionResponse"}
    assert fork_path["post"]["requestBody"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/SkillForkRequest"}
    import_path = openapi["paths"]["/api/projects/{project_id}/skills/import"]
    assert import_path["post"]["responses"]["201"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/SkillArchiveImportResponse"}
    assert "multipart/form-data" in import_path["post"]["requestBody"]["content"]
    for model_name in (
        "SkillFileContentResponse",
        "SkillFileContentItemResponse",
        "SkillFileRequest",
        "SkillVersionRequest",
        "SkillForkRequest",
        "SkillFileCreateChangeRequest",
        "SkillFileReplaceChangeRequest",
        "SkillFileDeleteChangeRequest",
    ):
        assert components[model_name]["additionalProperties"] is False
    credential_history = components["CredentialVersionHistoryResponse"]
    credential_item_ref = credential_history["properties"]["data"]["items"]["$ref"]
    credential_item = components[credential_item_ref.rsplit("/", 1)[-1]]
    assert credential_item["additionalProperties"] is False
    assert not {
        "plaintext",
        "ciphertext",
        "nonce",
        "key_id",
        "storage_locator",
        "secret_hash",
    } & set(credential_item["properties"])
    assert components["CredentialGrantMigrationRequest"]["additionalProperties"] is False
    assert components["CredentialGrantMigrationResponse"]["additionalProperties"] is False
    assert components["CredentialDeleteRequest"]["additionalProperties"] is False

    skill_file = components["SkillFileRequest"]["properties"]
    assert skill_file["path"]["maxLength"] == 1024
    assert skill_file["content_base64"]["maxLength"] == project_assets.MAX_SKILL_BASE64_FILE_CHARS
    assert skill_file["media_type"]["maxLength"] == 255
    skill_files = components["SkillVersionRequest"]["properties"]["files"]
    assert skill_files["minItems"] == 1
    assert skill_files["maxItems"] == project_assets.MAX_SKILL_ARCHIVE_FILES


def test_skill_base64_aggregate_is_rejected_before_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = project_assets.SkillVersionRequest.model_validate(
        {
            "files": [
                {
                    "path": "SKILL.md",
                    "content_base64": "eA==",
                    "media_type": "text/markdown",
                },
            ],
            "expected_asset_version": 1,
        }
    )
    monkeypatch.setattr(project_assets, "MAX_SKILL_ARCHIVE_BASE64_CHARS", 3)

    with pytest.raises(HTTPException) as exc_info:
        project_assets._decode_skill_files(body, "req-base64-bound")

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == {
        "code": "asset_validation_failed",
        "message": "Asset validation failed",
        "request_id": "req-base64-bound",
    }


def test_credential_history_response_is_secret_storage_safe() -> None:
    service = AsyncMock()
    credential_id = uuid.uuid4()
    version = CredentialVersionView(
        id=uuid.uuid4(),
        credential_id=credential_id,
        version_number=1,
        status="active",
        payload_schema_version=1,
        payload_schema=MappingProxyType({"env": ("TOKEN",)}),
        supersedes_version_id=None,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
    )
    service.get_version_history.return_value = (version,)

    response = _client(credential_service=service).get(f"/api/projects/{PROJECT_ID}/credentials/{credential_id}/versions")

    assert response.status_code == 200
    assert set(response.json()) == {"data", "request_id"}
    assert set(response.json()["data"][0]) == {
        "id",
        "credential_id",
        "version_number",
        "status",
        "payload_schema_version",
        "payload_schema",
        "supersedes_version_id",
        "created_by_user_id",
        "created_at",
    }
    assert not {
        "plaintext",
        "ciphertext",
        "nonce",
        "key_id",
        "storage_locator",
        "secret_hash",
    } & set(response.json()["data"][0])


def test_credential_grant_migration_route_is_strict_and_secret_storage_safe() -> None:
    service = AsyncMock()
    credential_id = uuid.uuid4()
    credential_version_id = uuid.uuid4()
    service.migrate_grants.return_value = CredentialGrantMigrationView(
        credential_id=credential_id,
        credential_version_id=credential_version_id,
        migrated_count=2,
    )
    client = _client(credential_service=service)

    response = client.post(
        f"/api/projects/{PROJECT_ID}/credentials/{credential_id}/migrate-grants",
        json={"expected_credential_version": 3},
    )
    invalid = client.post(
        f"/api/projects/{PROJECT_ID}/credentials/{credential_id}/migrate-grants",
        json={"expected_credential_version": 3, "credential_version_id": str(uuid.uuid4())},
    )

    assert response.status_code == 200
    assert response.json() == {
        "credential_id": str(credential_id),
        "credential_version_id": str(credential_version_id),
        "migrated_count": 2,
        "request_id": "req-project-assets",
    }
    assert invalid.status_code == 422
    service.migrate_grants.assert_awaited_once()
    actor, requested_credential_id = service.migrate_grants.await_args.args
    assert actor.project_id == PROJECT_ID
    assert requested_credential_id == credential_id
    assert service.migrate_grants.await_args.kwargs == {"expected_credential_version": 3}


def test_credential_delete_route_is_strict_and_returns_no_deleted_metadata() -> None:
    service = AsyncMock()
    credential_id = uuid.uuid4()
    client = _client(credential_service=service)

    response = client.request(
        "DELETE",
        f"/api/projects/{PROJECT_ID}/credentials/{credential_id}",
        json={"expected_credential_version": 3},
    )
    invalid = client.request(
        "DELETE",
        f"/api/projects/{PROJECT_ID}/credentials/{credential_id}",
        json={
            "expected_credential_version": 3,
            "credential_version_id": str(uuid.uuid4()),
        },
    )

    assert response.status_code == 204
    assert not response.content
    assert invalid.status_code == 422
    service.delete.assert_awaited_once()
    actor, requested_credential_id = service.delete.await_args.args
    assert actor.project_id == PROJECT_ID
    assert requested_credential_id == credential_id
    assert service.delete.await_args.kwargs == {"expected_credential_version": 3}


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (AssetNotFound, 404, "asset_not_found"),
        (AssetForbidden, 403, "asset_forbidden"),
        (AssetConflict, 409, "asset_conflict"),
        (AssetValidationFailed, 422, "asset_validation_failed"),
        (AssetStorageUnavailable, 503, "asset_storage_unavailable"),
    ],
)
def test_project_asset_domain_errors_have_stable_contract(error, status: int, code: str) -> None:
    service = AsyncMock()
    binding_service = AsyncMock()
    binding_service.list_visible.return_value = ()
    service.list_visible.side_effect = error("req-project-assets")

    response = _client(
        agent_service=service,
        binding_service=binding_service,
    ).get(f"/api/projects/{PROJECT_ID}/agents")

    assert response.status_code == status
    assert response.json() == {
        "detail": {
            "code": code,
            "message": error.public_message,
            "request_id": "req-project-assets",
        }
    }


def test_project_skill_storage_quota_error_is_429_with_retry_after() -> None:
    errors = importlib.import_module("app.shared_assets.errors")

    with pytest.raises(HTTPException) as exc_info:
        project_assets.raise_asset_domain(
            errors.AssetStorageQuotaExceeded("req-skill-quota"),
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers == {"Retry-After": "1"}
    assert exc_info.value.detail == {
        "code": "asset_storage_quota_exceeded",
        "message": "Project Skill storage quota exceeded",
        "request_id": "req-skill-quota",
    }


def test_project_binding_route_uses_typed_selection_and_forbids_extra_input() -> None:
    service = AsyncMock()
    service.enable.return_value = SystemAssetBinding(
        project_id=PROJECT_ID,
        kind=AssetKind.AGENT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        enabled=True,
        version=1,
        created_by_user_id=str(uuid.uuid4()),
        updated_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
        updated_at=NOW,
    )
    payload = {
        "asset_id": str(service.enable.return_value.asset_id),
        "version_id": str(service.enable.return_value.version_id),
    }
    client = _client(binding_service=service)

    response = client.post(f"/api/projects/{PROJECT_ID}/system-agent-bindings", json=payload)
    invalid = client.post(
        f"/api/projects/{PROJECT_ID}/system-agent-bindings",
        json={**payload, "unexpected": True},
    )

    assert response.status_code == 201
    assert response.json()["kind"] == "agent"
    assert response.json()["request_id"] == "req-project-assets"
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "asset_validation_failed"


def test_project_system_mcp_sync_current_route_never_accepts_a_version_id() -> None:
    service = AsyncMock()
    asset_id = uuid.uuid4()
    current_version_id = uuid.uuid4()
    service.sync_current_mcp.return_value = SystemAssetBinding(
        project_id=PROJECT_ID,
        kind=AssetKind.MCP,
        asset_id=asset_id,
        version_id=current_version_id,
        enabled=True,
        version=1,
        created_by_user_id=str(uuid.uuid4()),
        updated_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
        updated_at=NOW,
    )
    client = _client(binding_service=service)

    response = client.post(
        f"/api/projects/{PROJECT_ID}/system-mcp-bindings/{asset_id}/sync-current",
        json={},
    )

    assert response.status_code == 200
    assert response.json()["asset_id"] == str(asset_id)
    assert response.json()["version_id"] == str(current_version_id)
    service.sync_current_mcp.assert_awaited_once()
    actor, called_asset_id = service.sync_current_mcp.await_args.args
    assert actor.project_id == PROJECT_ID
    assert called_asset_id == asset_id
    assert service.sync_current_mcp.await_args.kwargs == {
        "expected_binding_version": None,
    }

    for rejected_body in (
        {"version_id": str(uuid.uuid4())},
        {"asset_id": str(asset_id)},
        {"unexpected": True},
        {"expected_binding_version": True},
        {"expected_binding_version": "4"},
        {"expected_binding_version": 4.0},
        {"expected_binding_version": 0},
    ):
        rejected = client.post(
            f"/api/projects/{PROJECT_ID}/system-mcp-bindings/{asset_id}/sync-current",
            json=rejected_body,
        )
        assert rejected.status_code == 422
    assert service.sync_current_mcp.await_count == 1


def test_project_system_mcp_sync_current_openapi_is_strict_and_project_only() -> None:
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.include_router(admin_assets.admin_router)
    app.include_router(admin_assets.admin_project_router)
    openapi = app.openapi()
    path = "/api/projects/{project_id}/system-mcp-bindings/{asset_id}/sync-current"
    operation = openapi["paths"][path]["post"]

    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SyncCurrentSystemMcpBindingRequest",
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/BindingResponse",
    }
    schema = openapi["components"]["schemas"]["SyncCurrentSystemMcpBindingRequest"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"expected_binding_version"}
    assert "/api/admin/assets/system-mcp-bindings/{asset_id}/sync-current" not in openapi["paths"]
    assert "/api/admin/projects/{project_id}/assets/system-mcp-bindings/{asset_id}/sync-current" not in openapi["paths"]


def test_project_asset_session_initialization_failure_uses_asset_503_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.persistence import engine as persistence_engine

    def unavailable_factory():
        raise RuntimeError("engine is not initialized")

    monkeypatch.setattr(persistence_engine, "get_session_factory", unavailable_factory)
    monkeypatch.setattr(project_assets, "get_current_trace_id", lambda: "req-asset-db")
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.dependency_overrides[project_assets.authenticated_asset_identity] = lambda: (
        uuid.uuid4(),
        "req-asset-db",
    )

    response = TestClient(app).get(f"/api/projects/{PROJECT_ID}/agents")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "asset_storage_unavailable",
            "message": AssetStorageUnavailable.public_message,
            "request_id": "req-asset-db",
        }
    }
