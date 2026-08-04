"""Operator-owned security policy for project MCP execution."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deerflow.mcp_endpoint_policy import validate_remote_mcp_network

DEFAULT_PROJECT_REMOTE_ALLOWED_NETWORKS = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
)


def _validated_network(value: str) -> str:
    try:
        return validate_remote_mcp_network(value)
    except ValueError:
        raise ValueError("MCP project network must be an IPv4 or IPv6 CIDR without host bits") from None


def _validated_proxy_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError("MCP egress proxy URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("MCP egress proxy URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None or parsed.username is not None or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("MCP egress proxy URL must be an HTTP(S) origin without credentials")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("MCP egress proxy port is invalid")
    return value.rstrip("/")


class McpSecurityConfig(BaseModel):
    """Bounded project MCP network, proxy, and timeout policy.

    System MCP definitions come from the digest-checked packaged catalog and
    are governed separately. Project remote MCP execution accepts only IP
    literals inside the configured networks. Exact ``localhost`` URLs are
    deterministically normalized to ``127.0.0.1`` before this check.
    """

    project_remote_allowed_networks: tuple[str, ...] = Field(
        default=DEFAULT_PROJECT_REMOTE_ALLOWED_NETWORKS,
        description="IPv4 and IPv6 CIDR networks allowed for project remote MCP IP literals.",
    )
    require_egress_proxy: bool = Field(
        default=False,
        description="Optionally require project remote MCP traffic to use the configured controlled egress proxy.",
    )
    egress_proxy_url: str | None = Field(
        default=None,
        description="Operator-controlled HTTP(S) egress proxy origin. Credentials are not accepted in the URL.",
    )
    discovery_timeout_seconds: int = Field(default=15, ge=1, le=300)
    tool_call_timeout_seconds: int = Field(default=60, ge=1, le=300)
    model_config = ConfigDict(extra="forbid")

    @field_validator("project_remote_allowed_networks")
    @classmethod
    def _validate_networks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validated_network(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("MCP project networks must be unique")
        return normalized

    @field_validator("egress_proxy_url")
    @classmethod
    def _validate_proxy(cls, value: str | None) -> str | None:
        return _validated_proxy_url(value)

    @model_validator(mode="after")
    def _require_configured_proxy_when_mandatory(self) -> McpSecurityConfig:
        if self.require_egress_proxy and self.egress_proxy_url is None:
            raise ValueError("MCP egress proxy is required when require_egress_proxy is enabled")
        return self
