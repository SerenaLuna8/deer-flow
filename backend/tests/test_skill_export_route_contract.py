from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import admin_assets, project_assets
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.contexts import SystemAssetGovernanceContext

_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_ASSET_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_VERSION_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[object, uuid.UUID, uuid.UUID]] = []

    async def export_distribution_package(self, actor, asset_id, version_id):
        self.calls.append((actor, asset_id, version_id))
        return SimpleNamespace(
            filename="meeting-brief-v7.zip",
            content=b"PK\x03\x04deterministic-zip",
            version_number=7,
        )


def _project_context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        project_id=_PROJECT_ID,
        membership_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="req-project-export",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["project", "admin"])
async def test_skill_export_routes_return_a_private_zip_download(surface: str) -> None:
    application = FastAPI()
    service = _Service()
    if surface == "project":
        actor: object = _project_context()
        application.dependency_overrides[project_assets.project_asset_context] = lambda: actor
        application.include_router(project_assets.project_router)
        url = f"/api/projects/{_PROJECT_ID}/skills/{_ASSET_ID}/versions/{_VERSION_ID}/export"
    else:
        actor = SystemAssetGovernanceContext(
            user_id=uuid.UUID("66666666-6666-4666-8666-666666666666"),
            request_id="req-admin-export",
        )
        application.dependency_overrides[admin_assets._admin_actor] = lambda: actor
        application.include_router(admin_assets.admin_router)
        url = f"/api/admin/assets/skills/{_ASSET_ID}/versions/{_VERSION_ID}/export"
    application.dependency_overrides[project_assets.get_skill_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(url)

    assert response.status_code == 200
    assert response.content == b"PK\x03\x04deterministic-zip"
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == ('attachment; filename="meeting-brief-v7.zip"')
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-length"] == str(len(response.content))
    assert service.calls == [(actor, _ASSET_ID, _VERSION_ID)]
