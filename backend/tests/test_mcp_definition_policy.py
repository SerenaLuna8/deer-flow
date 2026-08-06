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
        "ftp://mcp.example.test/tools",
        "https://user:password@mcp.example.test/tools",
        "https://mcp.example.test/tools#fragment",
        "https://mcp.example.test/tools#",
        "https://mcp.example.test/tools?token=secret",
        "https://mcp.example.test/tools?",
        "http:///tools",
        "http://localhost:0/tools",
        "http://localhost:/tools",
        "http://localhost:65536/tools",
        "http://localhost:not-a-port/tools",
        "http://127.000.000.001/tools",
        "http://[2001:0db8::1]/tools",
        "http://[FD00::1]/tools",
        "http://2130706433/tools",
        "http://0x7f.0.0.1/tools",
    ],
)
def test_remote_mcp_endpoint_rejects_invalid_urls_before_network_policy(
    endpoint: str,
) -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")

    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.validate_remote_mcp_endpoint(
            endpoint,
            endpoint_policy=_PermitEveryEndpoint(),
        )


def test_remote_mcp_endpoint_requires_an_injected_network_policy() -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")

    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.validate_remote_mcp_endpoint(
            "https://mcp.example.test/tools",
            endpoint_policy=None,
        )


def test_exact_mcp_endpoint_policy_matches_the_complete_endpoint() -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")
    allowed = "http://localhost:8771/tools/read"
    policy = policy_module.ExactMcpEndpointPolicy(frozenset({allowed}))

    assert (
        policy_module.validate_remote_mcp_endpoint(
            allowed,
            endpoint_policy=policy,
        )
        == "http://127.0.0.1:8771/tools/read"
    )
    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.validate_remote_mcp_endpoint(
            "http://localhost:8771/tools/write",
            endpoint_policy=policy,
        )


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://localhost:8771/tools", "http://127.0.0.1:8771/tools"),
        ("http://LOCALHOST:8771/tools", "http://127.0.0.1:8771/tools"),
        ("http://127.0.0.1:8771/tools", "http://127.0.0.1:8771/tools"),
        ("http://[::1]:8771/tools", "http://[::1]:8771/tools"),
        ("https://10.0.0.8:8443/tools", "https://10.0.0.8:8443/tools"),
        ("https://172.31.255.254/tools", "https://172.31.255.254/tools"),
        ("https://[fd00::8]:8443/tools/deep/path", "https://[fd00::8]:8443/tools/deep/path"),
    ],
)
def test_network_mcp_endpoint_policy_accepts_localhost_and_in_network_ip_literals(
    endpoint: str,
    expected: str,
) -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")
    policy = policy_module.NetworkMcpEndpointPolicy(
        (
            "127.0.0.0/8",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "::1/128",
            "fc00::/7",
        )
    )

    assert (
        policy_module.validate_remote_mcp_endpoint(
            endpoint,
            endpoint_policy=policy,
        )
        == expected
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://8.8.8.8/tools",
        "http://172.15.255.255/tools",
        "http://172.32.0.1/tools",
        "http://192.169.0.1/tools",
        "http://169.254.1.1/tools",
        "http://0.0.0.0/tools",
        "http://[::]/tools",
        "http://[fe80::1]/tools",
        "http://[::ffff:10.0.0.8]/tools",
        "https://mcp.internal/tools",
        "https://mcp-service/tools",
        "https://localhost./tools",
        "https://foo.localhost/tools",
        "https://localhost.localdomain/tools",
        "http://2130706433/tools",
        "http://0x7f.0.0.1/tools",
    ],
)
def test_network_mcp_endpoint_policy_rejects_out_of_network_ips_and_dns_names(
    endpoint: str,
) -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")
    policy = policy_module.NetworkMcpEndpointPolicy(
        (
            "127.0.0.0/8",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "::1/128",
            "fc00::/7",
        )
    )

    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.validate_remote_mcp_endpoint(
            endpoint,
            endpoint_policy=policy,
        )


def test_network_mcp_endpoint_policy_empty_networks_deny_every_target() -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")
    policy = policy_module.NetworkMcpEndpointPolicy(())

    for endpoint in ("http://localhost:8771/tools", "http://127.0.0.1/tools"):
        with pytest.raises(policy_module.McpDefinitionPolicyError):
            policy_module.validate_remote_mcp_endpoint(
                endpoint,
                endpoint_policy=policy,
            )


def test_localhost_normalization_requires_the_ipv4_loopback_network() -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")

    assert (
        policy_module.validate_remote_mcp_endpoint(
            "http://localhost:8771/deep/path",
            endpoint_policy=policy_module.NetworkMcpEndpointPolicy(("127.0.0.0/8",)),
        )
        == "http://127.0.0.1:8771/deep/path"
    )
    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.validate_remote_mcp_endpoint(
            "http://localhost:8771/deep/path",
            endpoint_policy=policy_module.NetworkMcpEndpointPolicy(("::1/128",)),
        )


@pytest.mark.parametrize(
    ("networks", "endpoint"),
    [
        (("0.0.0.0/0",), "https://8.8.8.8/tools"),
        (("::/0",), "https://[2001:4860:4860::8888]/tools"),
        (("::ffff:0:0/96",), "http://[::ffff:10.0.0.8]/tools"),
    ],
)
def test_network_mcp_endpoint_policy_honors_explicit_broad_and_mapped_ipv6_networks(
    networks: tuple[str, ...],
    endpoint: str,
) -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")

    assert (
        policy_module.validate_remote_mcp_endpoint(
            endpoint,
            endpoint_policy=policy_module.NetworkMcpEndpointPolicy(networks),
        )
        == endpoint
    )


def test_exact_mcp_endpoint_policy_cannot_allow_an_invalid_endpoint() -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")

    with pytest.raises(policy_module.McpDefinitionPolicyError):
        policy_module.ExactMcpEndpointPolicy(
            frozenset({"http://user:password@localhost/tools"}),
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
@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://localhost:8771/tools", "http://127.0.0.1:8771/tools"),
        ("https://10.0.0.8:8443/tools/deep/path", "https://10.0.0.8:8443/tools/deep/path"),
    ],
)
def test_project_mcp_definition_accepts_an_in_network_remote_endpoint(
    transport: str,
    endpoint: str,
    expected: str,
) -> None:
    policy_module = importlib.import_module("deerflow.mcp.definition")
    network_policy = policy_module.NetworkMcpEndpointPolicy(
        (
            "127.0.0.0/8",
            "10.0.0.0/8",
        )
    )

    assert (
        policy_module.validate_project_mcp_definition(
            transport=transport,
            url=endpoint,
            env={},
            headers={},
            oauth={},
            credential_slot_schemas=({"headers": ("Authorization",)},),
            endpoint_policy=network_policy,
        )
        == expected
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
