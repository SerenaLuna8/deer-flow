from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import admin_assets, project_assets
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.models import WorkflowStatus
from app.shared_assets.skill_credential_service import (
    SkillCredentialPublishRequirementView,
    SkillPublishPlanView,
)
from app.shared_assets.skill_service import SkillVersionView

_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_MEMBERSHIP_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_SKILL_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
_VERSION_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
_CREDENTIAL_VERSION_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")
_REQUEST_ID = "req-publish-route"


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=_USER_ID,
        project_id=_PROJECT_ID,
        membership_id=_MEMBERSHIP_ID,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id=_REQUEST_ID,
    )


def _admin_project_context() -> SystemAssetGovernanceContext:
    return SystemAssetGovernanceContext(
        user_id=_USER_ID,
        project_id=_PROJECT_ID,
        request_id=_REQUEST_ID,
    )


def _version() -> SkillVersionView:
    return SkillVersionView(
        id=_VERSION_ID,
        skill_id=_SKILL_ID,
        version_number=2,
        workflow_status=WorkflowStatus.PUBLISHED,
        description="Publish route Skill",
        frontmatter={
            "name": "publish-route-skill",
            "description": "Publish route Skill",
            "required-secrets": [
                {"name": "API_KEY", "optional": False},
            ],
        },
        compatibility=None,
        secret_requirements=(),
        scan_decision="allow",
        scan_rule_ids=(),
        scan_summary={},
        file_views=(),
        supersedes_version_id=None,
        payload_checksum="a" * 64,
        revoked_at=None,
        revoked_by_user_id=None,
        revocation_reason_code=None,
        governance_status="active",
        binding_eligible=True,
        created_by_user_id=str(_USER_ID),
        created_at=datetime(2026, 8, 19, tzinfo=UTC),
    )


@dataclass
class _PlanService:
    calls: list[tuple[ProjectContext, uuid.UUID, uuid.UUID]]

    async def get_for_version(self, actor, skill_id, version_id):
        self.calls.append((actor, skill_id, version_id))
        return SkillPublishPlanView(
            skill_id=skill_id,
            skill_version_id=version_id,
            asset_version=8,
            payload_checksum="a" * 64,
            binding_revision=0,
            secrets_autonomous=True,
            ready=False,
            required_count=1,
            configured_required_count=0,
            invalid_count=0,
            requirements=(
                SkillCredentialPublishRequirementView(
                    name="API_KEY",
                    optional=False,
                    mapping_status="missing",
                ),
            ),
        )


@dataclass
class _PublishService:
    calls: list[dict[str, object]]

    async def publish(self, actor, asset_id, version_id, **kwargs):
        self.calls.append(
            {
                "actor": actor,
                "asset_id": asset_id,
                "version_id": version_id,
                **kwargs,
            }
        )
        return _version()


@pytest.mark.asyncio
async def test_exact_version_publish_plan_route_returns_read_only_preflight() -> None:
    service = _PlanService(calls=[])
    context = _context()
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.dependency_overrides[project_assets.project_asset_context] = lambda: context
    app.dependency_overrides[project_assets.get_skill_credential_binding_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/skills/{_SKILL_ID}/versions/{_VERSION_ID}/publish-plan")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json() == {
        "skill_id": str(_SKILL_ID),
        "skill_version_id": str(_VERSION_ID),
        "asset_version": 8,
        "payload_checksum": "a" * 64,
        "binding_revision": 0,
        "secrets_autonomous": True,
        "ready": False,
        "required_count": 1,
        "configured_required_count": 0,
        "invalid_count": 0,
        "requirements": [
            {
                "name": "API_KEY",
                "optional": False,
                "mapping_status": "missing",
            }
        ],
        "request_id": _REQUEST_ID,
    }
    assert service.calls == [(context, _SKILL_ID, _VERSION_ID)]


@pytest.mark.asyncio
async def test_publish_route_forwards_read_only_preflight_cas_fields() -> None:
    service = _PublishService(calls=[])
    context = _context()
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.dependency_overrides[project_assets.project_asset_context] = lambda: context
    app.dependency_overrides[project_assets.get_skill_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/skills/{_SKILL_ID}/versions/{_VERSION_ID}/publish",
            json={
                "expected_asset_version": 8,
                "expected_payload_checksum": "a" * 64,
                "expected_binding_revision": 0,
                "acknowledge_stale_base": False,
            },
        )

    assert response.status_code == 200
    assert service.calls == [
        {
            "actor": context,
            "asset_id": _SKILL_ID,
            "version_id": _VERSION_ID,
            "expected_asset_version": 8,
            "expected_payload_checksum": "a" * 64,
            "expected_binding_revision": 0,
            "acknowledge_stale_base": False,
        }
    ]


@pytest.mark.asyncio
async def test_publish_route_requires_payload_and_binding_revision_cas() -> None:
    service = _PublishService(calls=[])
    context = _context()
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.dependency_overrides[project_assets.project_asset_context] = lambda: context
    app.dependency_overrides[project_assets.get_skill_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/skills/{_SKILL_ID}/versions/{_VERSION_ID}/publish",
            json={"expected_asset_version": 8},
        )

    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.asyncio
async def test_admin_project_publish_route_does_not_require_project_binding_cas() -> None:
    service = _PublishService(calls=[])
    context = _admin_project_context()
    app = FastAPI()
    app.include_router(admin_assets.admin_project_router)
    app.dependency_overrides[admin_assets._admin_project_actor] = lambda: context
    app.dependency_overrides[project_assets.get_skill_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/admin/projects/{_PROJECT_ID}/assets/skills/{_SKILL_ID}/versions/{_VERSION_ID}/publish",
            json={"expected_asset_version": 8},
        )

    assert response.status_code == 200
    assert service.calls == [
        {
            "actor": context,
            "asset_id": _SKILL_ID,
            "version_id": _VERSION_ID,
            "expected_asset_version": 8,
            "expected_payload_checksum": None,
            "expected_binding_revision": None,
            "acknowledge_stale_base": False,
        }
    ]


@pytest.mark.asyncio
async def test_publish_route_rejects_inline_binding_selection() -> None:
    service = _PublishService(calls=[])
    context = _context()
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.dependency_overrides[project_assets.project_asset_context] = lambda: context
    app.dependency_overrides[project_assets.get_skill_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/skills/{_SKILL_ID}/versions/{_VERSION_ID}/publish",
            json={
                "expected_asset_version": 8,
                "expected_payload_checksum": "a" * 64,
                "expected_binding_revision": 0,
                "credential_bindings": [
                    {
                        "name": "API_KEY",
                        "credential_version_id": str(_CREDENTIAL_VERSION_ID),
                        "source_env_field_name": "API_KEY",
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.asyncio
async def test_publish_route_rejects_explicit_null_binding_selection() -> None:
    service = _PublishService(calls=[])
    context = _context()
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.dependency_overrides[project_assets.project_asset_context] = lambda: context
    app.dependency_overrides[project_assets.get_skill_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/skills/{_SKILL_ID}/versions/{_VERSION_ID}/publish",
            json={
                "expected_asset_version": 8,
                "expected_payload_checksum": "a" * 64,
                "expected_binding_revision": 0,
                "credential_bindings": None,
            },
        )

    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.asyncio
async def test_publish_route_rejects_explicit_empty_binding_selection() -> None:
    service = _PublishService(calls=[])
    context = _context()
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.dependency_overrides[project_assets.project_asset_context] = lambda: context
    app.dependency_overrides[project_assets.get_skill_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/skills/{_SKILL_ID}/versions/{_VERSION_ID}/publish",
            json={
                "expected_asset_version": 8,
                "expected_payload_checksum": "a" * 64,
                "expected_binding_revision": 0,
                "credential_bindings": [],
            },
        )

    assert response.status_code == 422
    assert service.calls == []
