"""Canonical security policy for project-authored MCP definitions.

This module intentionally lives outside :mod:`deerflow.mcp`. Gateway and
Scheduler import it during process startup, and importing the MCP package also
loads runtime tool adapters and the Agent graph.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from deerflow.mcp_endpoint_policy import validate_remote_mcp_endpoint_syntax

_HTTP_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_FORBIDDEN_PROJECT_CREDENTIAL_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class McpDefinitionPolicyError(ValueError):
    """Stable, endpoint-free failure for an unsafe MCP definition."""

    def __init__(self) -> None:
        super().__init__("MCP definition violates the configured security policy")


class McpEndpointPolicy(Protocol):
    """Caller-owned allow policy evaluated after baseline URL validation."""

    def allows(self, endpoint: str, /) -> bool:
        """Return exactly ``True`` when the complete endpoint is allowed."""


def _validate_remote_mcp_endpoint_syntax(endpoint: object) -> str:
    try:
        return validate_remote_mcp_endpoint_syntax(endpoint)
    except ValueError:
        raise McpDefinitionPolicyError() from None


@dataclass(frozen=True)
class ExactMcpEndpointPolicy:
    """Allow only complete endpoint strings supplied by trusted configuration."""

    allowed_endpoints: frozenset[str]

    def __post_init__(self) -> None:
        if type(self.allowed_endpoints) is not frozenset:
            raise McpDefinitionPolicyError
        normalized = frozenset(_validate_remote_mcp_endpoint_syntax(endpoint) for endpoint in self.allowed_endpoints)
        object.__setattr__(self, "allowed_endpoints", normalized)

    def allows(self, endpoint: str, /) -> bool:
        return endpoint in self.allowed_endpoints


def validate_remote_mcp_endpoint(
    endpoint: object,
    *,
    endpoint_policy: McpEndpointPolicy | None,
) -> str:
    """Validate a remote endpoint and apply the caller's exact allow policy."""

    normalized = _validate_remote_mcp_endpoint_syntax(endpoint)
    try:
        allowed = endpoint_policy is not None and endpoint_policy.allows(normalized) is True
    except Exception:
        raise McpDefinitionPolicyError() from None
    if not allowed:
        raise McpDefinitionPolicyError
    return normalized


def validate_project_mcp_definition(
    *,
    transport: object,
    url: object,
    env: Mapping[object, object],
    headers: Mapping[object, object],
    oauth: Mapping[object, object],
    credential_slot_schemas: tuple[Mapping[object, object], ...],
    endpoint_policy: McpEndpointPolicy | None,
) -> str:
    """Enforce the project-authored MCP subset shared by Gateway and Worker."""

    if type(transport) is not str or transport.strip() not in {"http", "sse"}:
        raise McpDefinitionPolicyError
    if not isinstance(env, Mapping) or not isinstance(headers, Mapping) or not isinstance(oauth, Mapping) or type(credential_slot_schemas) is not tuple or env or headers or oauth:
        raise McpDefinitionPolicyError
    seen_header_names: set[str] = set()
    for schema in credential_slot_schemas:
        if not isinstance(schema, Mapping) or set(schema) != {"headers"}:
            raise McpDefinitionPolicyError
        names = schema["headers"]
        if not isinstance(names, (list, tuple)) or not names:
            raise McpDefinitionPolicyError
        for name in names:
            if type(name) is not str or len(name) > 255 or _HTTP_HEADER_NAME.fullmatch(name) is None:
                raise McpDefinitionPolicyError
            normalized_name = name.casefold()
            if normalized_name in _FORBIDDEN_PROJECT_CREDENTIAL_HEADERS or normalized_name in seen_header_names:
                raise McpDefinitionPolicyError
            seen_header_names.add(normalized_name)
    return validate_remote_mcp_endpoint(
        url,
        endpoint_policy=endpoint_policy,
    )


__all__ = [
    "ExactMcpEndpointPolicy",
    "McpDefinitionPolicyError",
    "McpEndpointPolicy",
    "validate_project_mcp_definition",
    "validate_remote_mcp_endpoint",
]
