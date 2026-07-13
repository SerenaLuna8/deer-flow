from __future__ import annotations

import dataclasses
import importlib
import inspect
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetConflict, AssetStorageUnavailable, AssetValidationFailed
from app.shared_assets.models import AgentPayload


def _editor_context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="req-agent-unit",
    )


def test_agent_service_exposes_frozen_typed_contracts_and_scoped_repository_api() -> None:
    package = importlib.import_module("app.shared_assets")
    service_module = importlib.import_module("app.shared_assets.agent_service")
    repository_module = importlib.import_module("app.shared_assets.agent_repository")

    assert package.AgentService is service_module.AgentService
    assert package.CreateAgent is service_module.CreateAgent
    assert package.AgentAssetView is service_module.AgentAssetView
    assert package.AgentVersionView is service_module.AgentVersionView

    create = service_module.CreateAgent(slug="analyst", display_name="Analyst")
    assert dataclasses.is_dataclass(create)
    with pytest.raises(dataclasses.FrozenInstanceError):
        create.slug = "changed"

    for view_type in (service_module.AgentAssetView, service_module.AgentVersionView):
        assert dataclasses.is_dataclass(view_type)
        assert view_type.__dataclass_params__.frozen is True

    public_methods = inspect.getmembers(repository_module.AgentRepository, predicate=inspect.isfunction)
    for name, method in public_methods:
        if name.startswith("_"):
            continue
        assert "project_id" not in inspect.signature(method).parameters, name

    project_get = inspect.signature(repository_module.AgentRepository.get_project_asset)
    assert list(project_get.parameters) == ["self", "context", "asset_id", "for_update"]


@pytest.mark.asyncio
async def test_invalid_agent_command_is_rejected_before_storage_is_opened() -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")

    class ExplodingSessionFactory:
        def __call__(self):
            raise AssertionError("invalid input must not open a database session")

    service = service_module.AgentService(ExplodingSessionFactory())
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service.create_asset(
            _editor_context(),
            service_module.CreateAgent(slug="Not Valid", display_name="Analyst"),
        )
    assert exc_info.value.request_id == "req-agent-unit"


@pytest.mark.asyncio
async def test_oversized_agent_model_ref_is_rejected_before_storage_is_opened() -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")

    class ExplodingSessionFactory:
        def __call__(self):
            raise AssertionError("invalid input must not open a database session")

    payload = AgentPayload(
        description="",
        soul="Stay within the schema.",
        model_ref="m" * 256,
        tool_groups=(),
        skill_version_ids=(),
        mcp_version_ids=(),
    )
    service = service_module.AgentService(ExplodingSessionFactory())
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service.create_version(
            _editor_context(),
            uuid.uuid4(),
            payload,
            expected_asset_version=1,
        )
    assert exc_info.value.request_id == "req-agent-unit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constraint_name", "error_type"),
    [
        ("uq_agents_project_slug", AssetConflict),
        ("uq_agent_versions_asset_number", AssetConflict),
        ("ck_agent_versions_checksum", AssetStorageUnavailable),
        (None, AssetStorageUnavailable),
    ],
)
async def test_agent_integrity_errors_only_map_known_business_conflicts_to_409(
    constraint_name: str | None,
    error_type: type[Exception],
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")

    class EmptySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def begin(self):
            return self

    class ConstraintViolation(Exception):
        def __init__(self, name: str | None):
            self.constraint_name = name

    async def fail_with_integrity_error(_repository):
        raise IntegrityError(
            "sensitive SQL must not escape",
            {"secret": "hidden"},
            ConstraintViolation(constraint_name),
        )

    service = service_module.AgentService(EmptySession)
    with pytest.raises(error_type) as exc_info:
        await service._execute(_editor_context(), fail_with_integrity_error)
    assert "sensitive SQL" not in str(exc_info.value)
    assert "hidden" not in str(exc_info.value)
