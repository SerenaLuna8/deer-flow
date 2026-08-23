from __future__ import annotations

import uuid
from datetime import UTC, datetime
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
from app.shared_assets.mcp_service import _mcp_tool_inventory_view
from app.shared_assets.mcp_tool_inventory_repository import (
    MAX_MCP_TOOL_INVENTORY_DESCRIPTION_CHARS,
    McpToolInventoryRecord,
    normalize_mcp_tool_inventory,
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

    payload = mcp_tool_inventory_payload((tool,))

    assert payload[0]["name"] == "lookup"
    assert payload[0]["runtime_name"] == "project_123_lookup"
    assert payload[0]["description"] == "Search records"
    assert isinstance(payload[0]["schema_utf8_bytes"], int)
    assert payload[0]["schema_utf8_bytes"] > 0
    assert len(payload[0]["schema_sha256"]) == 64
    assert normalize_mcp_tool_inventory(payload) == payload


def test_old_inventory_remains_readable_but_has_no_schema_contract() -> None:
    assert normalize_mcp_tool_inventory(({"name": "lookup", "description": "Search records"},)) == ({"name": "lookup", "description": "Search records"},)


def test_public_inventory_view_does_not_project_private_schema_facts() -> None:
    now = datetime.now(UTC)
    inventory = McpToolInventoryRecord(
        attempt_payload_checksum="payload",
        attempt_secret_digest="secret",
        attempt_status="succeeded",
        public_error_code=None,
        tools=(
            {
                "name": "lookup",
                "runtime_name": "project_lookup",
                "description": "Search records",
                "schema_utf8_bytes": 123,
                "schema_sha256": "a" * 64,
            },
        ),
        tools_payload_checksum="payload",
        tools_secret_digest="secret",
        last_attempt_at=now,
        last_success_at=now,
    )

    view = _mcp_tool_inventory_view(
        payload_checksum="payload",
        secret_digest="secret",
        inventory=inventory,
    )

    assert [(tool.name, tool.description) for tool in view.tools] == [("lookup", "Search records")]


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
