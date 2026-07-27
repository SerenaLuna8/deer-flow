"""Operator-owned security policy for project MCP execution."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deerflow.mcp_endpoint_policy import validate_remote_mcp_endpoint_syntax


def _validated_endpoint(value: str) -> str:
    try:
        validate_remote_mcp_endpoint_syntax(value)
    except ValueError:
        raise ValueError("MCP endpoint violates the remote endpoint syntax policy") from None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("MCP endpoint is invalid") from exc
    hostname = parsed.hostname
    if parsed.scheme != "https" or hostname is None or parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.query or not parsed.netloc:
        raise ValueError("MCP endpoint must be an exact HTTPS URL without credentials, query, or fragment")
    normalized_host = hostname.rstrip(".").lower()
    if not normalized_host or "*" in normalized_host or normalized_host == "localhost" or normalized_host.endswith(".localhost") or hostname != normalized_host:
        raise ValueError("MCP endpoint hostname must be canonical")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("MCP endpoint port is invalid")
    return value


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
    """Fail-closed project MCP endpoint and timeout policy.

    System MCP definitions come from the digest-checked packaged catalog and
    are governed separately. Project remote MCP execution is disabled until an
    operator explicitly lists exact endpoints.
    """

    project_remote_allowed_endpoints: tuple[str, ...] = Field(
        default=(),
        description="Exact HTTPS project MCP URLs approved by the platform operator.",
    )
    require_egress_proxy: bool = Field(
        default=True,
        description="Require project remote MCP traffic to use the configured controlled egress proxy.",
    )
    egress_proxy_url: str | None = Field(
        default=None,
        description="Operator-controlled HTTP(S) egress proxy origin. Credentials are not accepted in the URL.",
    )
    discovery_timeout_seconds: int = Field(default=15, ge=1, le=300)
    tool_call_timeout_seconds: int = Field(default=60, ge=1, le=300)
    model_config = ConfigDict(extra="forbid")

    @field_validator("project_remote_allowed_endpoints")
    @classmethod
    def _validate_endpoints(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validated_endpoint(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("MCP endpoints must be unique")
        return normalized

    @field_validator("egress_proxy_url")
    @classmethod
    def _validate_proxy(cls, value: str | None) -> str | None:
        return _validated_proxy_url(value)

    @model_validator(mode="after")
    def _require_proxy_for_enabled_project_remote(self) -> McpSecurityConfig:
        if self.project_remote_allowed_endpoints and self.require_egress_proxy and self.egress_proxy_url is None:
            raise ValueError("MCP egress proxy is required when project remote endpoints are enabled")
        return self
