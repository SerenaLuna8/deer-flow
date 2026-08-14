from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.private_work.mcp_runtime_contracts import (
    DiscoveredMcpTool,
    mcp_tool_inventory_description,
    mcp_tool_inventory_payload,
    safe_mcp_definition_copy,
    validate_project_mcp_material_policy,
    validate_project_mcp_snapshot_policy,
)
from app.private_work.snapshot_repository import RunSnapshotAssetStale
from app.shared_assets.mcp_tool_inventory_repository import (
    MAX_MCP_TOOL_INVENTORY_DESCRIPTION_CHARS,
)
from app.shared_assets.models import AssetScope
from deerflow.mcp_definition_policy import McpDefinitionPolicyError


class _Args(BaseModel):
    query: str


def test_inventory_description_normalizes_controls_and_whitespace() -> None:
    assert mcp_tool_inventory_description("  Search\tproject\nrecords\u200b\x00  safely  ") == "Search project records safely"


def test_inventory_description_truncates_with_an_ellipsis() -> None:
    description = "x" * (MAX_MCP_TOOL_INVENTORY_DESCRIPTION_CHARS + 20)

    normalized = mcp_tool_inventory_description(description)

    assert len(normalized) == MAX_MCP_TOOL_INVENTORY_DESCRIPTION_CHARS
    assert normalized == ("x" * (MAX_MCP_TOOL_INVENTORY_DESCRIPTION_CHARS - 1) + "…")


def test_inventory_payload_uses_provider_names_and_normalized_descriptions() -> None:
    tool = DiscoveredMcpTool(
        version_id=uuid.uuid4(),
        name="project_123_lookup",
        provider_name="lookup",
        description="  Search\trecords  ",
        args_schema=_Args,
    )

    assert mcp_tool_inventory_payload((tool,)) == (
        {
            "name": "lookup",
            "description": "Search records",
        },
    )


def test_project_mcp_material_accepts_headers_and_query_only() -> None:
    snapshot = SimpleNamespace(scope=AssetScope.PROJECT)

    validate_project_mcp_material_policy(
        snapshot,
        {
            "header-slot": {"headers": {"Authorization": "secret"}},
            "query-slot": {"query": {"key": "secret"}},
            "combined-slot": {
                "headers": {"X-Key": "secret"},
                "query": {"tenant": "secret"},
            },
        },
    )


@pytest.mark.parametrize(
    "material",
    [
        {"slot": {}},
        {"slot": {"env": {"TOKEN": "secret"}}},
        {"slot": {"cookies": {"token": "secret"}}},
        {"slot": {"headers": "secret"}},
        {"slot": object()},
    ],
)
def test_project_mcp_material_rejects_every_other_shape(
    material: dict[str, object],
) -> None:
    snapshot = SimpleNamespace(scope=AssetScope.PROJECT)

    with pytest.raises(McpDefinitionPolicyError):
        validate_project_mcp_material_policy(snapshot, material)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        b"not-json",
        {"invalid": {1, 2}},
        {"invalid": object()},
    ],
)
def test_safe_mcp_definition_copy_rejects_non_json_like_values(
    value: object,
) -> None:
    with pytest.raises(RunSnapshotAssetStale):
        safe_mcp_definition_copy(value)


def test_safe_mcp_definition_copy_preserves_the_existing_copy_shape() -> None:
    source = {1: ["one", {"nested": True}], "none": None}

    assert safe_mcp_definition_copy(source) == {
        "1": ("one", {"nested": True}),
        "none": None,
    }


def test_asset_runtime_preserves_private_contract_aliases() -> None:
    from app.private_work import asset_runtime

    assert asset_runtime._DiscoveredMcpTool is DiscoveredMcpTool
    assert asset_runtime._mcp_tool_inventory_description is mcp_tool_inventory_description
    assert asset_runtime._mcp_tool_inventory_payload is mcp_tool_inventory_payload
    assert asset_runtime._safe_copy is safe_mcp_definition_copy
    assert asset_runtime._validate_project_mcp_material_policy is validate_project_mcp_material_policy
    assert asset_runtime._validate_project_mcp_snapshot_policy is validate_project_mcp_snapshot_policy


def test_worker_discovery_uses_public_contract_exports() -> None:
    from app.worker import mcp_discovery

    assert mcp_discovery.DiscoveredMcpTool is DiscoveredMcpTool
    assert mcp_discovery.mcp_tool_inventory_payload is mcp_tool_inventory_payload
    assert mcp_discovery.validate_project_mcp_material_policy is validate_project_mcp_material_policy
    assert mcp_discovery.validate_project_mcp_snapshot_policy is validate_project_mcp_snapshot_policy
    assert "_DiscoveredMcpTool" not in vars(mcp_discovery)
    assert "_mcp_tool_inventory_payload" not in vars(mcp_discovery)
    assert "_validate_project_mcp_material_policy" not in vars(mcp_discovery)
    assert "_validate_project_mcp_snapshot_policy" not in vars(mcp_discovery)
