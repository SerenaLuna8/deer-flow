from __future__ import annotations

import dataclasses
import importlib
import inspect
import uuid

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetForbidden, AssetValidationFailed


def _context(role: ProjectRole = ProjectRole.EDITOR) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-mcp-unit",
    )


def _safe_definition(service_module):
    return service_module.McpDefinition(
        description="Issue tracker",
        transport="http",
        url="https://mcp.example.test",
        env={"NODE_ENV": "production"},
        headers={"Accept": "application/json"},
        oauth={"enabled": True, "client_id": "public-client"},
    )


def test_mcp_service_exposes_frozen_contracts_and_scoped_repository() -> None:
    package = importlib.import_module("app.shared_assets")
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    repository_module = importlib.import_module("app.shared_assets.mcp_repository")

    assert package.McpService is service_module.McpService
    for value_type in (
        service_module.CreateMcpServer,
        service_module.McpCredentialSlot,
        service_module.McpDefinition,
        service_module.McpAssetView,
        service_module.McpVersionView,
    ):
        assert dataclasses.is_dataclass(value_type)
        assert value_type.__dataclass_params__.frozen is True

    for name, method in inspect.getmembers(repository_module.McpRepository, predicate=inspect.isfunction):
        if not name.startswith("_"):
            assert "project_id" not in inspect.signature(method).parameters, name
    project_get = inspect.signature(repository_module.McpRepository.get_project_asset)
    assert list(project_get.parameters) == ["self", "context", "asset_id", "for_update"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definition",
    [
        {"transport": "stdio", "command": "uvx", "env": {"API_TOKEN": "never-log-me"}},
        {"transport": "http", "url": "https://mcp.test", "headers": {"Authorization": "Bearer never-log-me"}},
        {"transport": "http", "url": "https://mcp.test", "oauth": {"client_secret": "never-log-me"}},
        {"transport": "http", "url": "https://mcp.test", "oauth": {"refreshToken": "never-log-me"}},
    ],
)
async def test_mcp_definition_rejects_secret_fields_before_storage(
    definition: dict[str, object],
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("secret definition must not open storage")

    service = service_module.McpService(ExplodingFactory())
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service.create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(**definition),
            expected_asset_version=1,
        )
    assert exc_info.value.request_id == "req-mcp-unit"
    assert "never-log-me" not in str(exc_info.value)
    assert "never-log-me" not in repr(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"command": ["TOKEN"]},
        {"env": "TOKEN"},
        {"env": ["TOKEN", "TOKEN"]},
        {"oauth": [""]},
    ],
)
async def test_mcp_slot_schema_is_validated_before_storage(schema: dict[str, object]) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("invalid slot schema must not open storage")

    definition = dataclasses.replace(
        _safe_definition(service_module),
        credential_slots=(service_module.McpCredentialSlot("primary", "Auth", schema),),
    )
    with pytest.raises(AssetValidationFailed):
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            definition,
            expected_asset_version=1,
        )


@pytest.mark.asyncio
async def test_editor_cannot_approve_credential_mcp_before_storage() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("authorization must happen before storage")

    service = service_module.McpService(ExplodingFactory())
    with pytest.raises(AssetForbidden):
        await service.approve(
            _context(ProjectRole.EDITOR),
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
            expected_asset_version=3,
        )


@pytest.mark.parametrize(
    "field_update",
    [
        {"routing": {"nested": {"apiKey": "never-log-me"}}},
        {"tool_overrides": {"search": {"private_key": "never-log-me"}}},
    ],
)
def test_mcp_definition_rejects_nested_secret_config(field_update: dict[str, object]) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    definition = dataclasses.replace(_safe_definition(service_module), **field_update)

    with pytest.raises(AssetValidationFailed) as exc_info:
        service_module.McpService._validate_definition(_context(), definition)
    assert "never-log-me" not in str(exc_info.value)


def test_mcp_definition_accepts_nonsecret_oauth_protocol_metadata() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    definition = dataclasses.replace(
        _safe_definition(service_module),
        oauth={
            "enabled": True,
            "token_url": "https://identity.example.test/oauth/token",
            "grant_type": "client_credentials",
            "client_id": "public-client",
            "token_field": "access_token",
            "token_type_field": "token_type",
            "expires_in_field": "expires_in",
        },
    )

    normalized = service_module.McpService._validate_definition(_context(), definition)
    assert normalized.oauth["token_url"] == "https://identity.example.test/oauth/token"
