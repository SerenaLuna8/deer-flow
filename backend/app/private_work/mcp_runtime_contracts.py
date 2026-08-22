from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel

from app.private_work.snapshot_repository import RunSnapshotAssetStale
from app.shared_assets.mcp_tool_inventory_repository import (
    MAX_MCP_TOOL_INVENTORY_DESCRIPTION_CHARS,
)
from app.shared_assets.models import AssetScope, ResolvedMcpSnapshot
from deerflow.mcp.http_security import SecureMcpHttpClientFactory
from deerflow.mcp_definition_policy import (
    McpDefinitionPolicyError,
    McpEndpointPolicy,
    validate_project_mcp_definition,
)


@dataclass(frozen=True, slots=True)
class DiscoveredMcpTool:
    version_id: uuid.UUID
    name: str
    provider_name: str
    description: str
    args_schema: type[BaseModel]
    routing: dict[str, object] | None = None


def mcp_tool_inventory_description(value: str) -> str:
    characters: list[str] = []
    for character in value:
        if character.isspace():
            characters.append(" ")
            continue
        if unicodedata.category(character) in {"Cc", "Cf"}:
            continue
        characters.append(character)
    normalized = " ".join("".join(characters).split())
    if len(normalized) <= MAX_MCP_TOOL_INVENTORY_DESCRIPTION_CHARS:
        return normalized
    return normalized[: MAX_MCP_TOOL_INVENTORY_DESCRIPTION_CHARS - 1].rstrip() + "…"


def mcp_tool_inventory_payload(
    tools: tuple[DiscoveredMcpTool, ...],
) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "name": tool.provider_name,
            "description": mcp_tool_inventory_description(tool.description),
        }
        for tool in tools
    )


def validate_project_mcp_snapshot_policy(
    snapshot: ResolvedMcpSnapshot,
    *,
    endpoint_policy: McpEndpointPolicy | None,
    http_client_factory: SecureMcpHttpClientFactory | None,
) -> None:
    """Apply the shared Project MCP network and secret-schema boundary."""

    if snapshot.scope is not AssetScope.PROJECT:
        return
    definition = snapshot.definition
    oauth = definition.get("oauth", {})
    if not isinstance(oauth, Mapping) or oauth:
        raise McpDefinitionPolicyError
    slots = definition.get("secret_slots", ())
    if not isinstance(slots, (list, tuple)):
        raise McpDefinitionPolicyError
    secret_slot_schemas: list[Mapping[object, object]] = []
    for slot in slots:
        if not isinstance(slot, Mapping):
            raise McpDefinitionPolicyError
        payload_schema = slot.get("payload_schema", {})
        if not isinstance(payload_schema, Mapping) or "env" in payload_schema or "oauth" in payload_schema:
            raise McpDefinitionPolicyError
        secret_slot_schemas.append(payload_schema)
    validate_project_mcp_definition(
        transport=definition.get("transport"),
        url=definition.get("url"),
        env=definition.get("env", {}),  # type: ignore[arg-type]
        headers=definition.get("headers", {}),  # type: ignore[arg-type]
        oauth=oauth,
        secret_slot_schemas=tuple(secret_slot_schemas),
        endpoint_policy=endpoint_policy,
    )
    if http_client_factory is None:
        raise McpDefinitionPolicyError


def validate_project_mcp_material_policy(
    snapshot: ResolvedMcpSnapshot,
    material: Mapping[str, Mapping[str, object]],
) -> None:
    """Keep decrypted Project MCP material inside headers/query only."""

    if snapshot.scope is not AssetScope.PROJECT:
        return
    for payload in material.values():
        if not isinstance(payload, Mapping) or not payload or set(payload) - {"headers", "query"} or any(not isinstance(payload.get(section), Mapping) for section in payload):
            raise McpDefinitionPolicyError


def safe_mcp_definition_copy(value: object) -> object:
    """Copy only the resolver's JSON-like, secret-free MCP definition.

    Secret schema *field names* such as ``client_secret`` and ``key_id``
    describe required input; they are not secret material. The resolver
    resolver owns the plaintext boundary and deliberately excludes envelopes
    and decrypted payloads from this definition, so this copy validates shape
    instead of guessing secrecy from key names.
    """

    if isinstance(value, Mapping):
        return {str(key): safe_mcp_definition_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(safe_mcp_definition_copy(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise RunSnapshotAssetStale


__all__ = [
    "DiscoveredMcpTool",
    "mcp_tool_inventory_description",
    "mcp_tool_inventory_payload",
    "safe_mcp_definition_copy",
    "validate_project_mcp_material_policy",
    "validate_project_mcp_snapshot_policy",
]
