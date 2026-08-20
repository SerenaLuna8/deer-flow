from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import project_assets
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import SkillCredentialSelectionStale
from app.shared_assets.skill_credential_service import (
    SkillCredentialBindingSetView,
)

_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_MEMBERSHIP_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_SKILL_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
_VERSION_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
_CREDENTIAL_VERSION_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
_REQUEST_ID = "req-binding-route"


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


@dataclass
class _BindingService:
    calls: list[dict[str, object]]

    async def replace_for_version(
        self,
        actor,
        skill_id,
        skill_version_id,
        bindings,
        **kwargs,
    ):
        self.calls.append(
            {
                "actor": actor,
                "skill_id": skill_id,
                "skill_version_id": skill_version_id,
                "bindings": bindings,
                **kwargs,
            }
        )
        return SkillCredentialBindingSetView(
            skill_id=skill_id,
            skill_version_id=skill_version_id,
            revision=kwargs["expected_revision"] + 1,
            requirements=(),
        )


def _app(service: object) -> FastAPI:
    application = FastAPI()
    application.include_router(project_assets.project_router)
    application.dependency_overrides[project_assets.project_asset_context] = _context
    application.dependency_overrides[project_assets.get_skill_credential_binding_service] = lambda: service
    return application


@pytest.mark.asyncio
async def test_replace_binding_route_forwards_exact_version_and_source_field_cas() -> None:
    service = _BindingService(calls=[])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)),
        base_url="http://test",
    ) as client:
        response = await client.put(
            f"/api/projects/{_PROJECT_ID}/skills/{_SKILL_ID}/versions/{_VERSION_ID}/credential-bindings",
            json={
                "expected_revision": 3,
                "bindings": [
                    {
                        "name": "API_KEY",
                        "credential_version_id": str(_CREDENTIAL_VERSION_ID),
                        "source_env_field_name": "PROJECT_API_TOKEN",
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert service.calls == [
        {
            "actor": _context(),
            "skill_id": _SKILL_ID,
            "skill_version_id": _VERSION_ID,
            "bindings": (
                project_assets.SkillCredentialBindingInput(
                    "API_KEY",
                    _CREDENTIAL_VERSION_ID,
                    "PROJECT_API_TOKEN",
                ),
            ),
            "expected_revision": 3,
        }
    ]


@pytest.mark.asyncio
async def test_replace_binding_route_requires_revision_cas() -> None:
    service = _BindingService(calls=[])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)),
        base_url="http://test",
    ) as client:
        response = await client.put(
            f"/api/projects/{_PROJECT_ID}/skills/{_SKILL_ID}/versions/{_VERSION_ID}/credential-bindings",
            json={"bindings": []},
        )

    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.asyncio
async def test_replace_binding_route_returns_stable_stale_version_conflict() -> None:
    class _StaleBindingService:
        async def replace_for_version(
            self,
            actor,
            _skill_id,
            _skill_version_id,
            _bindings,
            **_kwargs,
        ):
            raise SkillCredentialSelectionStale(actor.request_id)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_StaleBindingService())),
        base_url="http://test",
    ) as client:
        response = await client.put(
            f"/api/projects/{_PROJECT_ID}/skills/{_SKILL_ID}/versions/{_VERSION_ID}/credential-bindings",
            json={
                "expected_revision": 3,
                "bindings": [],
            },
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "SKILL_CREDENTIAL_SELECTION_STALE",
            "message": "Skill credential selection is stale",
            "request_id": _REQUEST_ID,
        }
    }
