from __future__ import annotations

import dataclasses
import importlib
import inspect
import uuid
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetConflict, AssetForbidden


def _context(role: ProjectRole = ProjectRole.ADMIN) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-default-agent-unit",
    )


def _must_not_open_session():
    raise AssertionError("authorization and validation must precede storage")


def test_default_agent_contract_is_project_scoped_and_exported() -> None:
    package = importlib.import_module("app.shared_assets")
    module = importlib.import_module("app.shared_assets.default_agent_service")
    repository_module = importlib.import_module("app.shared_assets.default_agent_repository")
    from deerflow.persistence.projects import ProjectDefaultAgentRow

    assert package.ProjectDefaultAgentService is module.ProjectDefaultAgentService
    assert package.ProjectDefaultAgentSelection is module.ProjectDefaultAgentSelection
    assert dataclasses.is_dataclass(module.ProjectDefaultAgentSelection)
    assert module.ProjectDefaultAgentSelection.__dataclass_params__.frozen is True

    primary_key = tuple(column.name for column in ProjectDefaultAgentRow.__table__.primary_key.columns)
    assert primary_key == ("project_id",)
    foreign_keys = {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in ProjectDefaultAgentRow.__table__.foreign_key_constraints
    }
    assert (
        ("project_id", "agent_asset_id"),
        ("agents.project_id", "agents.id"),
    ) in foreign_keys
    revision_check = next(constraint for constraint in ProjectDefaultAgentRow.__table__.constraints if isinstance(constraint, sa.CheckConstraint) and constraint.name == "ck_project_default_agents_revision")
    assert str(revision_check.sqltext) == "revision >= 1"

    for name, method in inspect.getmembers(
        repository_module.ProjectDefaultAgentRepository,
        predicate=inspect.isfunction,
    ):
        if not name.startswith("_"):
            assert "project_id" not in inspect.signature(method).parameters, name


@pytest.mark.asyncio
async def test_default_agent_get_requires_read_capability_before_storage() -> None:
    module = importlib.import_module("app.shared_assets.default_agent_service")
    actor = dataclasses.replace(_context(ProjectRole.VIEWER), capabilities=frozenset())
    service = module.ProjectDefaultAgentService(_must_not_open_session)

    with pytest.raises(AssetForbidden):
        await service.get(actor)


@pytest.mark.asyncio
async def test_default_agent_replace_requires_binding_management_before_storage() -> None:
    module = importlib.import_module("app.shared_assets.default_agent_service")
    service = module.ProjectDefaultAgentService(_must_not_open_session)

    with pytest.raises(AssetForbidden):
        await service.replace(
            _context(ProjectRole.EDITOR),
            uuid.uuid4(),
            expected_revision=0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expected_revision",
    [-1, True, "1", 9_223_372_036_854_775_808],
)
async def test_default_agent_replace_rejects_invalid_revision_before_storage(
    expected_revision: object,
) -> None:
    module = importlib.import_module("app.shared_assets.default_agent_service")
    service = module.ProjectDefaultAgentService(_must_not_open_session)

    with pytest.raises(AssetConflict):
        await service.replace(
            _context(),
            uuid.uuid4(),
            expected_revision=expected_revision,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_default_agent_in_session_returns_revision_zero_without_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("app.shared_assets.default_agent_service")
    repository_module = importlib.import_module("app.shared_assets.default_agent_repository")
    actor = _context(ProjectRole.VIEWER)
    session = Mock(spec=AsyncSession)
    session.in_transaction.return_value = True
    get = AsyncMock(return_value=None)
    monkeypatch.setattr(
        repository_module.ProjectDefaultAgentRepository,
        "get_in_session",
        get,
    )
    service = module.ProjectDefaultAgentService(lambda: None)

    result = await service.get_in_session(session, actor)

    assert result == module.ProjectDefaultAgentSelection(actor.project_id, None, 0)
    get.assert_awaited_once_with(actor, for_update=False)


@pytest.mark.asyncio
async def test_thread_resolution_locks_default_pointer_and_returns_none() -> None:
    module = importlib.import_module("app.shared_assets.default_agent_service")
    actor = _context(ProjectRole.RUNNER)
    service = module.ProjectDefaultAgentService(lambda: None)
    service.get_in_session = AsyncMock(return_value=module.ProjectDefaultAgentSelection(actor.project_id, None, 0))
    session = Mock(spec=AsyncSession)

    assert await service.resolve_configured_agent_in_session(session, actor) is None
    service.get_in_session.assert_awaited_once_with(session, actor, lock=True)


def test_default_agent_api_get_and_put_contract() -> None:
    from app.gateway.routers import project_assets
    from app.shared_assets.default_agent_service import ProjectDefaultAgentSelection

    actor = _context()
    first_agent = uuid.uuid4()
    second_agent = uuid.uuid4()
    service = AsyncMock()
    service.get.return_value = ProjectDefaultAgentSelection(
        actor.project_id,
        first_agent,
        3,
    )
    service.replace.return_value = ProjectDefaultAgentSelection(
        actor.project_id,
        second_agent,
        4,
    )
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.dependency_overrides[project_assets.project_asset_context] = lambda: actor
    app.dependency_overrides[project_assets.get_project_default_agent_service] = lambda: service
    client = TestClient(app)

    get_response = client.get(f"/api/projects/{actor.project_id}/default-agent")
    put_response = client.put(
        f"/api/projects/{actor.project_id}/default-agent",
        json={
            "agent_asset_id": str(second_agent),
            "expected_revision": 3,
        },
    )

    assert get_response.status_code == 200
    assert get_response.json() == {
        "agent_asset_id": str(first_agent),
        "revision": 3,
        "request_id": actor.request_id,
    }
    assert put_response.status_code == 200
    assert put_response.json() == {
        "agent_asset_id": str(second_agent),
        "revision": 4,
        "request_id": actor.request_id,
    }
    service.get.assert_awaited_once_with(actor)
    service.replace.assert_awaited_once_with(
        actor,
        second_agent,
        expected_revision=3,
    )


def test_default_agent_api_requires_explicit_nullable_selection_and_revision() -> None:
    from app.gateway.routers import project_assets

    actor = _context()
    service = AsyncMock()
    app = FastAPI()
    app.include_router(project_assets.project_router)
    app.dependency_overrides[project_assets.project_asset_context] = lambda: actor
    app.dependency_overrides[project_assets.get_project_default_agent_service] = lambda: service
    client = TestClient(app)

    missing_selection = client.put(
        f"/api/projects/{actor.project_id}/default-agent",
        json={"expected_revision": 0},
    )
    overflow = client.put(
        f"/api/projects/{actor.project_id}/default-agent",
        json={
            "agent_asset_id": None,
            "expected_revision": 9_223_372_036_854_775_808,
        },
    )

    assert missing_selection.status_code == 422
    assert overflow.status_code == 422
    service.replace.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["suspend", "delete"])
async def test_current_default_agent_blocks_destructive_lifecycle_change(
    operation: str,
) -> None:
    module = importlib.import_module("app.shared_assets.agent_service")
    actor = _context()
    asset_id = uuid.uuid4()
    repository = AsyncMock()
    repository.ensure_not_current_project_default.side_effect = AssetConflict(actor.request_id)
    service = module.AgentService(lambda: None)

    async def execute(_actor, callback, governance=None):
        del governance
        return await callback(repository)

    service._execute = execute
    with pytest.raises(AssetConflict):
        await getattr(service, operation)(
            actor,
            asset_id,
            expected_asset_version=1,
        )
    repository.ensure_not_current_project_default.assert_awaited_once_with(
        actor,
        asset_id,
    )
    repository.get_project_asset.assert_not_awaited()
