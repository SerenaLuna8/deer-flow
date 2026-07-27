from __future__ import annotations

import importlib
import uuid
from types import SimpleNamespace

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetValidationFailed


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="req-mcp-definition-policy",
    )


class _PermitEveryEndpoint:
    def allows(self, endpoint: str) -> bool:
        del endpoint
        return True


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://mcp.example.test/tools",
        "https://user:password@mcp.example.test/tools",
        "https://mcp.example.test/tools#fragment",
        "https://127.0.0.1/tools",
        "https://[::1]/tools",
        "https://localhost/tools",
        "https://api.localhost/tools",
        "https://0x7f.0.0.1/tools",
    ],
)
def test_remote_mcp_endpoint_rejects_unsafe_network_targets_before_exact_policy(
    endpoint: str,
) -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")

    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.validate_remote_mcp_endpoint(
            endpoint,
            endpoint_policy=_PermitEveryEndpoint(),
        )


def test_remote_mcp_endpoint_requires_an_injected_exact_policy() -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")

    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.validate_remote_mcp_endpoint(
            "https://mcp.example.test/tools",
            endpoint_policy=None,
        )


def test_exact_mcp_endpoint_policy_matches_the_complete_endpoint() -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")
    allowed = "https://mcp.example.test/tools?mode=read"
    policy = policy_module.ExactMcpEndpointPolicy(frozenset({allowed}))

    assert (
        policy_module.validate_remote_mcp_endpoint(
            allowed,
            endpoint_policy=policy,
        )
        == allowed
    )
    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.validate_remote_mcp_endpoint(
            "https://mcp.example.test/tools?mode=write",
            endpoint_policy=policy,
        )


def test_exact_mcp_endpoint_policy_cannot_allow_an_unsafe_endpoint() -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")

    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.ExactMcpEndpointPolicy(
            frozenset({"https://localhost/tools"}),
        )


@pytest.mark.parametrize(
    ("transport", "url"),
    [
        ("stdio", None),
        ("streamable_http", "https://mcp.example.test/tools"),
    ],
)
def test_project_mcp_definition_rejects_non_remote_supported_transports(
    transport: str,
    url: str | None,
) -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")

    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.validate_project_mcp_definition(
            transport=transport,
            url=url,
            env={},
            headers={},
            oauth={},
            credential_slot_schemas=(),
            endpoint_policy=_PermitEveryEndpoint(),
        )


@pytest.mark.parametrize(
    ("env", "headers"),
    [
        ({"PUBLIC_MODE": "read"}, {}),
        ({}, {"X-Service-Key": "opaque-value"}),
    ],
)
def test_project_mcp_definition_rejects_every_literal_env_or_header(
    env: dict[str, str],
    headers: dict[str, str],
) -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")

    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.validate_project_mcp_definition(
            transport="http",
            url="https://mcp.example.test/tools",
            env=env,
            headers=headers,
            oauth={},
            credential_slot_schemas=(),
            endpoint_policy=_PermitEveryEndpoint(),
        )


@pytest.mark.parametrize(
    ("oauth", "credential_slot_schemas"),
    [
        (
            {
                "enabled": True,
                "token_url": "https://identity.example.test/token",
            },
            (),
        ),
        ({}, ({"env": ("TOKEN",)},)),
        ({}, ({"oauth": ("client_secret",)},)),
        ({}, ({"headers": ("Authorization",), "env": ("TOKEN",)},)),
    ],
)
def test_project_mcp_definition_rejects_runtime_unsupported_secret_targets(
    oauth: dict[str, object],
    credential_slot_schemas: tuple[dict[str, tuple[str, ...]], ...],
) -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")

    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.validate_project_mcp_definition(
            transport="http",
            url="https://mcp.example.test/tools",
            env={},
            headers={},
            oauth=oauth,
            credential_slot_schemas=credential_slot_schemas,
            endpoint_policy=_PermitEveryEndpoint(),
        )


@pytest.mark.parametrize(
    "header_name",
    [
        "Host",
        "Connection",
        "Content-Length",
        "Proxy-Authorization",
        "Transfer-Encoding",
        "Upgrade",
        "bad header",
        "X-Test\r\nInjected",
    ],
)
def test_project_mcp_definition_rejects_unsafe_credential_header_names(
    header_name: str,
) -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")

    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.validate_project_mcp_definition(
            transport="http",
            url="https://mcp.example.test/tools",
            env={},
            headers={},
            oauth={},
            credential_slot_schemas=({"headers": (header_name,)},),
            endpoint_policy=_PermitEveryEndpoint(),
        )


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_project_mcp_definition_accepts_an_exact_allowed_remote_endpoint(
    transport: str,
) -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")
    endpoint = "https://mcp.example.test/tools"

    assert (
        policy_module.validate_project_mcp_definition(
            transport=transport,
            url=endpoint,
            env={},
            headers={},
            oauth={},
            credential_slot_schemas=({"headers": ("Authorization",)},),
            endpoint_policy=policy_module.ExactMcpEndpointPolicy(
                frozenset({endpoint}),
            ),
        )
        == endpoint
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definition",
    [
        {"transport": "stdio", "command": "mcp"},
        {
            "transport": "streamable_http",
            "url": "https://mcp.example.test/tools",
        },
        {
            "transport": "http",
            "url": "https://mcp.example.test/tools",
            "env": {"PUBLIC_MODE": "read"},
        },
        {
            "transport": "http",
            "url": "https://mcp.example.test/tools",
            "headers": {"X-Service-Key": "opaque-value"},
        },
    ],
)
async def test_project_mcp_service_enforces_authoring_policy_before_storage(
    definition: dict[str, object],
) -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("invalid authoring must not open storage")

    service = service_module.McpService(
        ExplodingFactory(),
        endpoint_policy=_PermitEveryEndpoint(),
    )
    with pytest.raises(AssetValidationFailed):
        await service.create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(**definition),
            expected_asset_version=1,
        )


@pytest.mark.asyncio
async def test_project_mcp_service_defaults_to_fail_closed_endpoint_policy() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("missing endpoint policy must not open storage")

    with pytest.raises(AssetValidationFailed):
        await service_module.McpService(ExplodingFactory()).create_version(
            _context(),
            uuid.uuid4(),
            service_module.McpDefinition(
                transport="http",
                url="https://mcp.example.test/tools",
            ),
            expected_asset_version=1,
        )


def test_system_packaged_definition_read_remains_legacy_compatible() -> None:
    service_module = importlib.import_module("app.shared_assets.mcp_service")
    record = SimpleNamespace(
        row=SimpleNamespace(
            description="Packaged local MCP",
            transport="stdio",
            command="uvx",
            args=["packaged-mcp"],
            url=None,
            non_secret_env={"NODE_ENV": "production"},
            non_secret_headers={},
            oauth_metadata={},
            routing={},
            tool_overrides={},
            timeout_seconds=30,
        ),
        slots=(),
    )

    definition = service_module.McpService._definition_from_record(record)

    assert definition.transport == "stdio"
    assert definition.env == {"NODE_ENV": "production"}
