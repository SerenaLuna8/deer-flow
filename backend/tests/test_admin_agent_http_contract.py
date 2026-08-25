from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import admin_assets
from app.shared_assets.agent_service import AgentAssetView
from app.shared_assets.binding_service import SystemAssetBinding
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.models import AssetKind, AssetScope

_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_ADMIN_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_SYSTEM_AGENT_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_SYSTEM_DEFINITION_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
_PROJECT_AGENT_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
_PROJECT_DEFINITION_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
_REQUEST_ID = "admin-agent-http-contract"
_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _agent(
    *,
    asset_id: uuid.UUID,
    definition_id: uuid.UUID,
    scope: AssetScope,
) -> AgentAssetView:
    project_id = _PROJECT_ID if scope is AssetScope.PROJECT else None
    label = "Project" if scope is AssetScope.PROJECT else "System"
    return AgentAssetView(
        id=asset_id,
        scope=scope,
        project_id=project_id,
        slug=f"{label.lower()}-agent",
        display_name=f"{label} Agent",
        status="active",
        definition_id=definition_id,
        revision=3,
        created_by_user_id=str(_ADMIN_ID),
        created_at=_NOW,
        updated_at=_NOW,
        description=f"{label} Agent description",
    )


_SYSTEM_AGENT = _agent(
    asset_id=_SYSTEM_AGENT_ID,
    definition_id=_SYSTEM_DEFINITION_ID,
    scope=AssetScope.SYSTEM,
)
_PROJECT_AGENT = _agent(
    asset_id=_PROJECT_AGENT_ID,
    definition_id=_PROJECT_DEFINITION_ID,
    scope=AssetScope.PROJECT,
)
_BINDING = SystemAssetBinding(
    project_id=_PROJECT_ID,
    kind=AssetKind.AGENT,
    asset_id=_SYSTEM_AGENT_ID,
    version_id=_SYSTEM_DEFINITION_ID,
    enabled=True,
    version=1,
    created_by_user_id=str(_ADMIN_ID),
    updated_by_user_id=str(_ADMIN_ID),
    created_at=_NOW,
    updated_at=_NOW,
)


class _AgentService:
    async def list_visible(self, actor) -> tuple[AgentAssetView, ...]:
        if actor.project_id is None:
            return (_SYSTEM_AGENT,)
        return (_PROJECT_AGENT,)


class _BindingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def list_visible(
        self,
        actor: SystemAssetGovernanceContext,
        kind: AssetKind,
    ) -> tuple[SystemAssetBinding, ...]:
        self.calls.append(("list", (actor, kind)))
        return (_BINDING,)

    async def enable(
        self,
        actor: SystemAssetGovernanceContext,
        selection,
        *,
        expected_binding_version: int | None = None,
    ) -> SystemAssetBinding:
        self.calls.append(("enable", (actor, selection, expected_binding_version)))
        return _BINDING

    async def disable(
        self,
        actor: SystemAssetGovernanceContext,
        selection,
        *,
        expected_binding_version: int,
    ) -> SystemAssetBinding:
        self.calls.append(("disable", (actor, selection, expected_binding_version)))
        return replace(_BINDING, enabled=False, version=2)


def _global_actor() -> SystemAssetGovernanceContext:
    return SystemAssetGovernanceContext(
        user_id=_ADMIN_ID,
        request_id=_REQUEST_ID,
    )


def _project_actor() -> SystemAssetGovernanceContext:
    return SystemAssetGovernanceContext(
        user_id=_ADMIN_ID,
        request_id=_REQUEST_ID,
        project_id=_PROJECT_ID,
    )


def _app(
    agent_service: _AgentService,
    binding_service: _BindingService,
) -> FastAPI:
    application = FastAPI()
    application.dependency_overrides[admin_assets._admin_actor] = _global_actor
    application.dependency_overrides[admin_assets._admin_project_actor] = _project_actor
    application.dependency_overrides[admin_assets.get_agent_service] = lambda: agent_service
    application.dependency_overrides[admin_assets.get_binding_service] = lambda: binding_service
    application.include_router(admin_assets.admin_router)
    application.include_router(admin_assets.admin_project_router)
    return application


def _assert_agent_item(item: dict[str, object], *, definition_id: uuid.UUID) -> None:
    assert item["definition_id"] == str(definition_id)
    assert "current_version_id" not in item


@pytest.mark.asyncio
async def test_admin_system_agent_list_uses_definition_contract() -> None:
    application = _app(_AgentService(), _BindingService())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/admin/assets/agents")

    assert response.status_code == 200
    payload = response.json()
    assert payload["request_id"] == _REQUEST_ID
    assert len(payload["items"]) == 1
    _assert_agent_item(payload["items"][0], definition_id=_SYSTEM_DEFINITION_ID)


@pytest.mark.asyncio
async def test_admin_project_agent_list_uses_definition_and_binding_contracts() -> None:
    application = _app(_AgentService(), _BindingService())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/api/admin/projects/{_PROJECT_ID}/assets/agents")

    assert response.status_code == 200
    payload = response.json()
    system_item = payload["system_items"][0]
    project_item = payload["project_items"][0]
    _assert_agent_item(system_item, definition_id=_SYSTEM_DEFINITION_ID)
    _assert_agent_item(project_item, definition_id=_PROJECT_DEFINITION_ID)
    assert system_item["binding"]["definition_id"] == str(_SYSTEM_DEFINITION_ID)
    assert "current_version_id" not in system_item["binding"]
    assert project_item["binding"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "body", "expected_status", "expected_enabled", "expected_version"),
    [
        ("", {"asset_id": str(_SYSTEM_AGENT_ID)}, 201, True, 1),
        (
            f"/{_SYSTEM_AGENT_ID}/disable",
            {"expected_binding_version": 1},
            200,
            False,
            2,
        ),
    ],
)
async def test_admin_project_agent_binding_mutations_use_definition_contract(
    suffix: str,
    body: dict[str, object],
    expected_status: int,
    expected_enabled: bool,
    expected_version: int,
) -> None:
    application = _app(_AgentService(), _BindingService())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/admin/projects/{_PROJECT_ID}/assets/system-agent-bindings{suffix}",
            json=body,
        )

    assert response.status_code == expected_status
    payload = response.json()
    assert payload["definition_id"] == str(_SYSTEM_DEFINITION_ID)
    assert payload["enabled"] is expected_enabled
    assert payload["version"] == expected_version
    assert "current_version_id" not in payload
    assert "version_id" not in payload
