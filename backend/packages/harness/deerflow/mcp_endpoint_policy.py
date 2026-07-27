"""Shared syntax validation for operator and runtime MCP endpoint policies."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

_LEGACY_NUMERIC_HOST_LABEL = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)\Z")


def validate_remote_mcp_endpoint_syntax(endpoint: object) -> str:
    """Return an HTTPS MCP endpoint only when its authority is remotely safe.

    This deliberately does not decide whether an endpoint is operator-allowed.
    Callers layer exact allow-list and configuration-specific requirements on
    top of this shared syntax boundary.
    """

    try:
        if type(endpoint) is not str or not endpoint or endpoint != endpoint.strip():
            raise ValueError
        if "\\" in endpoint or "#" in endpoint or any(character.isspace() or ord(character) < 0x21 for character in endpoint):
            raise ValueError
        parsed = urlsplit(endpoint)
        if parsed.scheme.casefold() != "https" or not parsed.netloc or parsed.fragment:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        hostname = parsed.hostname
        if not hostname or "%" in hostname:
            raise ValueError
        port = parsed.port
        if port == 0:
            raise ValueError

        canonical_hostname = hostname.encode("idna").decode("ascii").casefold().rstrip(".")
        if (
            not canonical_hostname
            or "." not in canonical_hostname
            or canonical_hostname == "localhost"
            or canonical_hostname.endswith(".localhost")
            or canonical_hostname == "localhost.localdomain"
            or canonical_hostname.endswith(".localhost.localdomain")
        ):
            raise ValueError
        labels = canonical_hostname.split(".")
        if len(labels) <= 4 and all(_LEGACY_NUMERIC_HOST_LABEL.fullmatch(label) is not None for label in labels):
            raise ValueError
        try:
            ipaddress.ip_address(canonical_hostname)
        except ValueError:
            compact_numeric = canonical_hostname.replace(".", "")
            if compact_numeric.isdecimal() or ":" in canonical_hostname:
                raise ValueError
        else:
            raise ValueError
        return endpoint
    except (UnicodeError, ValueError):
        raise ValueError("MCP endpoint violates the remote endpoint syntax policy") from None


__all__ = ["validate_remote_mcp_endpoint_syntax"]
