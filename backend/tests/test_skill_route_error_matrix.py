from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import project_assets
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetValidationFailed,
    SharedAssetError,
)

_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_ASSET_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_REQUEST_ID = "skill-route-error-matrix"


class _RaisingSkillService:
    def __init__(self, error_factory: Callable[[str], SharedAssetError]) -> None:
        self._error_factory = error_factory
        self.calls: list[tuple[ProjectContext, uuid.UUID]] = []

    async def get(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
    ) -> None:
        self.calls.append((context, asset_id))
        raise self._error_factory(context.request_id)


def _context() -> ProjectContext:
    role = ProjectRole.ADMIN
    return ProjectContext(
        user_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        project_id=_PROJECT_ID,
        membership_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id=_REQUEST_ID,
    )


def _app(
    error_factory: Callable[[str], SharedAssetError],
) -> tuple[FastAPI, _RaisingSkillService]:
    application = FastAPI()
    context = _context()
    service = _RaisingSkillService(error_factory)
    application.dependency_overrides[project_assets.project_asset_context] = lambda: context
    application.dependency_overrides[project_assets.get_skill_service] = lambda: service
    application.include_router(project_assets.project_router)
    return application, service


async def _get(application: FastAPI, asset_id: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        return await client.get(f"/api/projects/{_PROJECT_ID}/skills/{asset_id}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_factory", "status_code", "code", "message"),
    [
        (AssetForbidden, 403, "asset_forbidden", "Asset capability required"),
        (AssetNotFound, 404, "asset_not_found", "Asset not found"),
        (AssetConflict, 409, "asset_conflict", "Asset state conflict"),
        (
            AssetValidationFailed,
            422,
            "asset_validation_failed",
            "Asset validation failed",
        ),
    ],
)
async def test_project_skill_route_maps_domain_errors(
    error_factory: Callable[[str], SharedAssetError],
    status_code: int,
    code: str,
    message: str,
) -> None:
    application, service = _app(error_factory)

    response = await _get(application, str(_ASSET_ID))

    assert response.status_code == status_code
    assert response.json() == {
        "detail": {
            "code": code,
            "message": message,
            "request_id": _REQUEST_ID,
        }
    }
    assert service.calls == [(_context(), _ASSET_ID)]


@pytest.mark.asyncio
async def test_project_skill_route_maps_request_validation_to_asset_contract() -> None:
    application, service = _app(AssetConflict)

    response = await _get(application, "not-a-uuid")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "asset_validation_failed"
    assert detail["message"] == "Asset validation failed"
    assert isinstance(detail["request_id"], str)
    assert detail["request_id"]
    assert service.calls == []
