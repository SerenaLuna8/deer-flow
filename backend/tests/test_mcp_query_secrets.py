from __future__ import annotations

import uuid
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

import pytest

from app.private_work.asset_runtime import PrivateAgentRuntime
from app.private_work.errors import PrivateWorkAssetStale
from app.private_work.snapshot_repository import RunSnapshotRepository
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.mcp_service import McpDefinition, McpSecretSlot, McpService
from app.shared_assets.models import AssetScope
from deerflow.assets.catalog import AssetCatalogUnavailable
from deerflow.mcp.tools import _merge_catalog_mcp_secrets
from deerflow.mcp_definition_policy import (
    ExactMcpEndpointPolicy,
    McpDefinitionPolicyError,
    validate_project_mcp_definition,
)

_BASE_ENDPOINT = "https://mcp.example.test/mcp"


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="req-mcp-query-credential",
    )


def _endpoint_policy(endpoint: str = _BASE_ENDPOINT) -> ExactMcpEndpointPolicy:
    return ExactMcpEndpointPolicy(frozenset({endpoint}))


def test_project_mcp_policy_accepts_query_secret_against_secret_free_base_url() -> None:
    assert (
        validate_project_mcp_definition(
            transport="http",
            url=_BASE_ENDPOINT,
            env={},
            headers={},
            oauth={},
            secret_slot_schemas=({"query": ("key",)},),
            endpoint_policy=_endpoint_policy(),
        )
        == _BASE_ENDPOINT
    )


def test_project_mcp_policy_accepts_header_and_query_in_one_slot() -> None:
    assert (
        validate_project_mcp_definition(
            transport="http",
            url=_BASE_ENDPOINT,
            env={},
            headers={},
            oauth={},
            secret_slot_schemas=(
                {
                    "headers": ("Authorization",),
                    "query": ("api_key",),
                },
            ),
            endpoint_policy=_endpoint_policy(),
        )
        == _BASE_ENDPOINT
    )


@pytest.mark.parametrize(
    ("url", "schemas"),
    [
        (f"{_BASE_ENDPOINT}?key=public", ({"query": ("key",)},)),
        (_BASE_ENDPOINT, ({"query": ("bad name",)},)),
        (_BASE_ENDPOINT, ({"query": ("key",)}, {"query": ("key",)})),
        (
            _BASE_ENDPOINT,
            (
                {"headers": ("Authorization",)},
                {"headers": ("authorization",)},
            ),
        ),
    ],
)
def test_project_mcp_policy_rejects_ambiguous_or_unsafe_secret_names(
    url: str,
    schemas: tuple[dict[str, tuple[str, ...]], ...],
) -> None:
    with pytest.raises(McpDefinitionPolicyError):
        validate_project_mcp_definition(
            transport="http",
            url=url,
            env={},
            headers={},
            oauth={},
            secret_slot_schemas=schemas,
            endpoint_policy=_endpoint_policy(url),
        )


def test_project_mcp_service_accepts_query_slot_without_persisting_secret() -> None:
    context = _context()
    normalized = McpService._validate_definition(
        context,
        McpDefinition(
            transport="http",
            url=_BASE_ENDPOINT,
            secret_slots=(
                McpSecretSlot(
                    name="api-key",
                    purpose="Remote API query key",
                    payload_schema={"query": ("key",)},
                ),
            ),
        ),
        endpoint_policy=_endpoint_policy(),
    )

    assert normalized.url == _BASE_ENDPOINT
    assert normalized.secret_slots[0].payload_schema == {"query": ("key",)}


def test_project_mcp_service_accepts_combined_slot_without_persisting_secret() -> None:
    normalized = McpService._validate_definition(
        _context(),
        McpDefinition(
            transport="http",
            url=_BASE_ENDPOINT,
            secret_slots=(
                McpSecretSlot(
                    name="auth",
                    purpose="Remote request credentials",
                    payload_schema={
                        "headers": ("Authorization",),
                        "query": ("api_key",),
                    },
                ),
            ),
        ),
        endpoint_policy=_endpoint_policy(),
    )

    assert normalized.secret_slots[0].payload_schema == {
        "headers": ("Authorization",),
        "query": ("api_key",),
    }


def test_runtime_merges_query_secret_into_url_only_in_materialized_config() -> None:
    secret = "query-secret-with symbols/+"
    server_config = {
        "transport": "http",
        "url": f"{_BASE_ENDPOINT}?mode=read",
    }

    merged = _merge_catalog_mcp_secrets(
        server_config,
        {"api-key": {"query": {"key": secret}}},
    )

    assert server_config["url"] == f"{_BASE_ENDPOINT}?mode=read"
    assert parse_qsl(urlsplit(str(merged["url"])).query, keep_blank_values=True) == [
        ("mode", "read"),
        ("key", secret),
    ]


def test_runtime_merges_header_and_query_from_one_materialized_slot() -> None:
    server_config = {
        "transport": "http",
        "url": _BASE_ENDPOINT,
        "headers": {"X-Public-Mode": "read"},
    }

    merged = _merge_catalog_mcp_secrets(
        server_config,
        {
            "auth": {
                "headers": {"Authorization": "Bearer secret"},
                "query": {"api_key": "query secret"},
            }
        },
    )

    assert server_config == {
        "transport": "http",
        "url": _BASE_ENDPOINT,
        "headers": {"X-Public-Mode": "read"},
    }
    assert merged["headers"] == {
        "X-Public-Mode": "read",
        "Authorization": "Bearer secret",
    }
    assert parse_qsl(
        urlsplit(str(merged["url"])).query,
        keep_blank_values=True,
    ) == [("api_key", "query secret")]


def test_runtime_rejects_query_secret_for_non_remote_or_duplicate_parameter() -> None:
    with pytest.raises(AssetCatalogUnavailable):
        _merge_catalog_mcp_secrets(
            {"transport": "stdio", "command": "example"},
            {"api-key": {"query": {"key": "secret"}}},
        )
    with pytest.raises(AssetCatalogUnavailable):
        _merge_catalog_mcp_secrets(
            {"transport": "http", "url": f"{_BASE_ENDPOINT}?key=public"},
            {"api-key": {"query": {"key": "secret"}}},
        )


@pytest.mark.asyncio
async def test_discovery_and_tool_call_receive_combined_credentials_in_one_shot_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_mcp_adapters import client as client_module

    secret = "one-shot-query-secret"
    header_secret = "one-shot-header-secret"
    captured_urls: list[str] = []
    captured_headers: list[dict[str, str]] = []
    close_count = 0

    class RemoteTool:
        name = "lookup"

        async def ainvoke(self, arguments):
            return {"value": arguments["value"]}

    class OneShotClient:
        def __init__(self, servers, **_kwargs):
            server = next(iter(servers.values()))
            captured_urls.append(server["url"])
            captured_headers.append(dict(server["headers"]))

        async def get_tools(self, *, server_name):
            del server_name
            return [RemoteTool()]

        async def aclose(self):
            nonlocal close_count
            close_count += 1

    monkeypatch.setattr(client_module, "MultiServerMCPClient", OneShotClient)
    definition = {"transport": "http", "url": _BASE_ENDPOINT}
    material = {
        "auth": {
            "headers": {"Authorization": header_secret},
            "query": {"key": secret},
        }
    }

    async def discover(tools, _derived):
        return [tool.name for tool in tools]

    async def call(tools, _derived):
        return await tools[0].ainvoke({"value": "ok"})

    assert await PrivateAgentRuntime._with_one_shot_mcp_tools(
        uuid.uuid4(),
        definition,
        material,
        discover,
    ) == ["lookup"]
    assert await PrivateAgentRuntime._with_one_shot_mcp_tools(
        uuid.uuid4(),
        definition,
        material,
        call,
    ) == {"value": "ok"}
    assert definition["url"] == _BASE_ENDPOINT
    assert len(captured_urls) == 2
    assert all(parse_qsl(urlsplit(url).query, keep_blank_values=True) == [("key", secret)] for url in captured_urls)
    assert captured_headers == [
        {"Authorization": header_secret},
        {"Authorization": header_secret},
    ]
    assert close_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoded_echo",
    (
        quote_plus("secret with/+symbols"),
        quote("secret with/+symbols", safe=""),
        quote("secret with/+symbols", safe="").replace("%2F", "%2f").replace("%2B", "%2b"),
    ),
)
async def test_discovery_rejects_percent_encoded_query_secret_echo(
    monkeypatch: pytest.MonkeyPatch,
    encoded_echo: str,
) -> None:
    from langchain_mcp_adapters import client as client_module

    secret = "secret with/+symbols"

    class RemoteTool:
        name = "lookup"
        description = f"leak:{encoded_echo}"

    class OneShotClient:
        def __init__(self, _servers, **_kwargs):
            pass

        async def get_tools(self, *, server_name):
            del server_name
            return [RemoteTool()]

        async def aclose(self):
            pass

    monkeypatch.setattr(client_module, "MultiServerMCPClient", OneShotClient)

    with pytest.raises(PrivateWorkAssetStale):
        await PrivateAgentRuntime._discover_exact_mcp(
            uuid.uuid4(),
            {"transport": "http", "url": _BASE_ENDPOINT},
            {"api-key": {"query": {"key": secret}}},
        )


def test_run_admission_and_worker_runtime_accept_query_secret_material() -> None:
    version_id = uuid.uuid4()
    asset = SimpleNamespace(scope="project")
    version = SimpleNamespace(
        id=version_id,
        transport="http",
        url=_BASE_ENDPOINT,
        non_secret_env={},
        non_secret_headers={},
        oauth_metadata={},
    )
    closures = {
        version_id: SimpleNamespace(
            slots=(SimpleNamespace(payload_schema={"query": ["key"]}),),
        )
    }

    RunSnapshotRepository._validate_project_mcp_secret_slots(
        [(asset, version)],
        closures,
        endpoint_policy=_endpoint_policy(),
    )

    snapshot = SimpleNamespace(scope=AssetScope.PROJECT)
    PrivateAgentRuntime._validate_project_mcp_material(
        snapshot,
        {"api-key": {"query": {"key": "secret"}}},
    )


def test_worker_runtime_rejects_unknown_project_secret_material_group() -> None:
    snapshot = SimpleNamespace(scope=AssetScope.PROJECT)
    with pytest.raises(McpDefinitionPolicyError):
        PrivateAgentRuntime._validate_project_mcp_material(
            snapshot,
            {"api-key": {"cookies": {"key": "secret"}}},
        )
