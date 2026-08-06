"""Canonical security policy for project-authored MCP definitions.

This module intentionally lives outside :mod:`deerflow.mcp`. Gateway and
Scheduler import it during process startup, and importing the MCP package also
loads runtime tool adapters and the Agent graph.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

from deerflow.mcp_endpoint_policy import (
    validate_remote_mcp_endpoint_syntax,
    validate_remote_mcp_network,
)

_HTTP_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_QUERY_PARAMETER_NAME = re.compile(r"[A-Za-z0-9._~-]{1,128}\Z")
_PROJECT_CREDENTIAL_SECTIONS = frozenset({"headers", "query"})
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
    """Compatibility/test policy for complete trusted endpoint strings.

    Production process composition uses :class:`NetworkMcpEndpointPolicy`.
    """

    allowed_endpoints: frozenset[str]

    def __post_init__(self) -> None:
        if type(self.allowed_endpoints) is not frozenset:
            raise McpDefinitionPolicyError
        normalized = frozenset(_validate_remote_mcp_endpoint_syntax(endpoint) for endpoint in self.allowed_endpoints)
        object.__setattr__(self, "allowed_endpoints", normalized)

    def allows(self, endpoint: str, /) -> bool:
        return endpoint in self.allowed_endpoints


@dataclass(frozen=True)
class NetworkMcpEndpointPolicy:
    """Allow IP-literal endpoints only when their address is in a configured CIDR."""

    allowed_networks: tuple[str, ...]
    _parsed_networks: tuple[IPv4Network | IPv6Network, ...] = field(
        default=(),
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.allowed_networks) is not tuple:
            raise McpDefinitionPolicyError
        try:
            normalized = tuple(validate_remote_mcp_network(value) for value in self.allowed_networks)
        except ValueError:
            raise McpDefinitionPolicyError() from None
        if len(set(normalized)) != len(normalized):
            raise McpDefinitionPolicyError
        object.__setattr__(self, "allowed_networks", normalized)
        object.__setattr__(
            self,
            "_parsed_networks",
            tuple(ip_network(value, strict=True) for value in normalized),
        )

    def allows(self, endpoint: str, /) -> bool:
        try:
            normalized = validate_remote_mcp_endpoint_syntax(endpoint)
            hostname = urlsplit(normalized).hostname
            if hostname is None:
                return False
            address = ip_address(hostname)
        except ValueError:
            return False
        return any(address.version == network.version and address in network for network in self._parsed_networks)


def validate_remote_mcp_endpoint(
    endpoint: object,
    *,
    endpoint_policy: McpEndpointPolicy | None,
) -> str:
    """Validate a remote endpoint and apply the caller-owned network policy."""

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
    normalized_endpoint = validate_remote_mcp_endpoint(
        url,
        endpoint_policy=endpoint_policy,
    )
    base_query_names = {
        name
        for name, _value in parse_qsl(
            urlsplit(normalized_endpoint).query,
            keep_blank_values=True,
        )
    }
    credential_query_names: set[str] = set()
    for schema in credential_slot_schemas:
        if not isinstance(schema, Mapping) or len(schema) != 1 or not set(schema).issubset(_PROJECT_CREDENTIAL_SECTIONS):
            raise McpDefinitionPolicyError
        header_names = schema.get("headers")
        if header_names is not None and (
            not isinstance(header_names, (list, tuple))
            or not header_names
            or any(type(name) is not str or len(name) > 255 or _HTTP_HEADER_NAME.fullmatch(name) is None or name.casefold() in _FORBIDDEN_PROJECT_CREDENTIAL_HEADERS for name in header_names)
            or len({name.casefold() for name in header_names}) != len(header_names)
        ):
            raise McpDefinitionPolicyError
        query_names = schema.get("query")
        if query_names is not None:
            if (
                not isinstance(query_names, (list, tuple))
                or not query_names
                or any(type(name) is not str or _QUERY_PARAMETER_NAME.fullmatch(name) is None for name in query_names)
                or len(set(query_names)) != len(query_names)
                or base_query_names.intersection(query_names)
                or credential_query_names.intersection(query_names)
            ):
                raise McpDefinitionPolicyError
            credential_query_names.update(query_names)
    return normalized_endpoint


__all__ = [
    "ExactMcpEndpointPolicy",
    "McpDefinitionPolicyError",
    "McpEndpointPolicy",
    "NetworkMcpEndpointPolicy",
    "validate_project_mcp_definition",
    "validate_remote_mcp_endpoint",
]
