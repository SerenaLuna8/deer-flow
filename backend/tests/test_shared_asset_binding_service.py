from __future__ import annotations

import uuid

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetForbidden, AssetValidationFailed
from app.shared_assets.models import AssetKind, AssetSelection


def _context(role: ProjectRole) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-binding-unit",
    )


def _must_not_open_session():
    raise AssertionError("authorization and input validation must run before database access")


@pytest.mark.asyncio
async def test_only_project_admin_can_manage_system_bindings() -> None:
    from app.shared_assets.binding_service import BindingService

    service = BindingService(_must_not_open_session)
    selection = AssetSelection(AssetKind.AGENT, uuid.uuid4(), uuid.uuid4())

    with pytest.raises(AssetForbidden):
        await service.enable(_context(ProjectRole.EDITOR), selection)


@pytest.mark.asyncio
async def test_binding_requires_an_explicit_version() -> None:
    from app.shared_assets.binding_service import BindingService

    service = BindingService(_must_not_open_session)
    selection = AssetSelection(AssetKind.SKILL, uuid.uuid4())

    with pytest.raises(AssetValidationFailed):
        await service.enable(_context(ProjectRole.ADMIN), selection)
