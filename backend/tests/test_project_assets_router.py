from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.routers import project_assets
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_service import AgentAssetView
from app.shared_assets.binding_service import SystemAssetBinding
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import AssetKind, AssetScope

PROJECT_ID = uuid.uuid4()
NOW = datetime.now(UTC)


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=PROJECT_ID,
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="req-project-assets",
    )


def _agent(scope: AssetScope) -> AgentAssetView:
    return AgentAssetView(
        id=uuid.uuid4(),
        scope=scope,
        project_id=PROJECT_ID if scope is AssetScope.PROJECT else None,
        slug=f"{scope.value}-agent",
        display_name=f"{scope.value} agent",
        status="active",
        current_published_version_id=None,
        version=1,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
        updated_at=NOW,
    )


def _client(*, agent_service=None, binding_service=None) -> TestClient:
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.dependency_overrides[project_assets.project_asset_context] = _context
    if agent_service is not None:
        app.dependency_overrides[project_assets.get_agent_service] = lambda: agent_service
    if binding_service is not None:
        app.dependency_overrides[project_assets.get_binding_service] = lambda: binding_service
    return TestClient(app)


def test_project_asset_list_separates_scopes() -> None:
    service = AsyncMock()
    service.list_visible.return_value = (_agent(AssetScope.SYSTEM), _agent(AssetScope.PROJECT))

    response = _client(agent_service=service).get(f"/api/projects/{PROJECT_ID}/agents")

    assert response.status_code == 200
    assert set(response.json()) == {"system_items", "project_items", "request_id"}
    assert [item["scope"] for item in response.json()["system_items"]] == ["system"]
    assert [item["scope"] for item in response.json()["project_items"]] == ["project"]
    service.list_visible.assert_awaited_once()
    actor = service.list_visible.await_args.args[0]
    assert actor.project_id == PROJECT_ID
    assert actor.request_id == "req-project-assets"


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
    service.list_visible.side_effect = error("req-project-assets")

    response = _client(agent_service=service).get(f"/api/projects/{PROJECT_ID}/agents")

    assert response.status_code == status
    assert response.json() == {
        "detail": {
            "code": code,
            "message": error.public_message,
            "request_id": "req-project-assets",
        }
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
