"""Shared syntax validation for operator and runtime MCP endpoint policies."""

from __future__ import annotations

import re
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit, urlunsplit

_LEGACY_NUMERIC_HOST_LABEL = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)\Z", re.IGNORECASE)


def validate_remote_mcp_endpoint_syntax(endpoint: object) -> str:
    """Return a syntactically safe HTTP(S) MCP endpoint.

    Host trust is deliberately not inferred from DNS shape or address scope.
    Production callers layer the configured CIDR policy on top of this syntax
    boundary before a project endpoint can execute.
    """

    try:
        if type(endpoint) is not str or not endpoint or endpoint != endpoint.strip():
            raise ValueError
        if "\\" in endpoint or "?" in endpoint or "#" in endpoint or any(character.isspace() or ord(character) < 0x21 for character in endpoint):
            raise ValueError
        parsed = urlsplit(endpoint)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc or parsed.fragment:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        hostname = parsed.hostname
        if not hostname or "*" in hostname or "%" in hostname or parsed.netloc.endswith(":"):
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if hostname.casefold() == "localhost":
            authority = "127.0.0.1"
            if port is not None:
                authority = f"{authority}:{port}"
            return urlunsplit(
                (
                    parsed.scheme.casefold(),
                    authority,
                    parsed.path,
                    "",
                    "",
                )
            )
        try:
            address = ip_address(hostname)
        except ValueError:
            labels = hostname.split(".")
            if len(labels) <= 4 and all(_LEGACY_NUMERIC_HOST_LABEL.fullmatch(label) is not None for label in labels):
                raise ValueError
        else:
            if address.version == 6:
                if not parsed.netloc.startswith("["):
                    raise ValueError
                raw_hostname = parsed.netloc[1 : parsed.netloc.index("]")]
            elif port is None:
                raw_hostname = parsed.netloc
            else:
                raw_hostname = parsed.netloc.rsplit(":", 1)[0]
            if raw_hostname != address.compressed:
                raise ValueError
        return endpoint
    except ValueError:
        raise ValueError("MCP endpoint violates the remote endpoint syntax policy") from None


def validate_remote_mcp_network(value: object) -> str:
    """Return one canonical CIDR network without silently widening host bits."""

    try:
        if type(value) is not str or not value or value != value.strip() or "/" not in value:
            raise ValueError
        if any(character.isspace() or ord(character) < 0x21 for character in value):
            raise ValueError
        return str(ip_network(value, strict=True))
    except ValueError:
        raise ValueError("MCP network must be a canonical IPv4 or IPv6 CIDR") from None


__all__ = [
    "validate_remote_mcp_endpoint_syntax",
    "validate_remote_mcp_network",
]
