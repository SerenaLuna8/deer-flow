"""OAuth token support for MCP HTTP/SSE servers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from deerflow.mcp.config import ExtensionsConfig, McpOAuthConfig
from deerflow.mcp.http_security import SecureMcpHttpClientFactory
from deerflow.mcp_definition_policy import (
    McpEndpointPolicy,
    validate_remote_mcp_endpoint,
)

logger = logging.getLogger(__name__)


@dataclass
class _OAuthToken:
    """Cached OAuth token."""

    access_token: str
    token_type: str
    expires_at: datetime


class OAuthTokenManager:
    """Acquire/cache/refresh OAuth tokens for MCP servers."""

    def __init__(
        self,
        oauth_by_server: dict[str, McpOAuthConfig],
        *,
        endpoint_policy: McpEndpointPolicy | None = None,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
    ):
        self._oauth_by_server = oauth_by_server
        self._endpoint_policy = endpoint_policy
        self._http_client_factory = http_client_factory
        self._tokens: dict[str, _OAuthToken] = {}
        self._locks: dict[str, asyncio.Lock] = {name: asyncio.Lock() for name in oauth_by_server}

    @classmethod
    def from_runtime_config(
        cls,
        runtime_mcp_config: ExtensionsConfig,
        *,
        endpoint_policy: McpEndpointPolicy | None = None,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
    ) -> OAuthTokenManager:
        oauth_by_server: dict[str, McpOAuthConfig] = {}
        for server_name, server_config in runtime_mcp_config.get_enabled_mcp_servers().items():
            if server_config.oauth and server_config.oauth.enabled:
                oauth_by_server[server_name] = server_config.oauth
        return cls(
            oauth_by_server,
            endpoint_policy=endpoint_policy,
            http_client_factory=http_client_factory,
        )

    def has_oauth_servers(self) -> bool:
        return bool(self._oauth_by_server)

    def oauth_server_names(self) -> list[str]:
        return list(self._oauth_by_server.keys())

    async def get_authorization_header(self, server_name: str) -> str | None:
        oauth = self._oauth_by_server.get(server_name)
        if not oauth:
            return None

        token = self._tokens.get(server_name)
        if token and not self._is_expiring(token, oauth):
            return f"{token.token_type} {token.access_token}"

        lock = self._locks[server_name]
        async with lock:
            token = self._tokens.get(server_name)
            if token and not self._is_expiring(token, oauth):
                return f"{token.token_type} {token.access_token}"

            fresh = await self._fetch_token(oauth)
            self._tokens[server_name] = fresh
            logger.info(f"Refreshed OAuth access token for MCP server: {server_name}")
            return f"{fresh.token_type} {fresh.access_token}"

    @staticmethod
    def _is_expiring(token: _OAuthToken, oauth: McpOAuthConfig) -> bool:
        now = datetime.now(UTC)
        return token.expires_at <= now + timedelta(seconds=max(oauth.refresh_skew_seconds, 0))

    async def _fetch_token(self, oauth: McpOAuthConfig) -> _OAuthToken:
        import httpx  # pyright: ignore[reportMissingImports]

        token_url = validate_remote_mcp_endpoint(
            oauth.token_url,
            endpoint_policy=self._endpoint_policy,
        )
        if self._http_client_factory is None:
            raise ValueError("OAuth token HTTP client policy is unavailable")

        data: dict[str, str] = {
            "grant_type": oauth.grant_type,
            **oauth.extra_token_params,
        }

        if oauth.scope:
            data["scope"] = oauth.scope
        if oauth.audience:
            data["audience"] = oauth.audience

        if not oauth.client_id or not oauth.client_secret:
            raise ValueError("OAuth client_credentials requires client_id and client_secret")
        data["client_id"] = oauth.client_id
        data["client_secret"] = oauth.client_secret

        async with self._http_client_factory(
            None,
            httpx.Timeout(15.0),
            None,
        ) as client:
            response = await client.post(token_url, data=data)
            response.raise_for_status()
            payload = response.json()

        access_token = payload.get(oauth.token_field)
        if not access_token:
            raise ValueError(f"OAuth token response missing '{oauth.token_field}'")

        token_type = str(payload.get(oauth.token_type_field, oauth.default_token_type) or oauth.default_token_type)

        expires_in_raw = payload.get(oauth.expires_in_field, 3600)
        try:
            expires_in = int(expires_in_raw)
        except (TypeError, ValueError):
            expires_in = 3600

        expires_at = datetime.now(UTC) + timedelta(seconds=max(expires_in, 1))
        return _OAuthToken(access_token=access_token, token_type=token_type, expires_at=expires_at)


def build_oauth_tool_interceptor(runtime_mcp_config: ExtensionsConfig) -> Any | None:
    """Build a tool interceptor that injects OAuth Authorization headers."""
    token_manager = OAuthTokenManager.from_runtime_config(runtime_mcp_config)
    if not token_manager.has_oauth_servers():
        return None

    async def oauth_interceptor(request: Any, handler: Any) -> Any:
        header = await token_manager.get_authorization_header(request.server_name)
        if not header:
            return await handler(request)

        updated_headers = dict(request.headers or {})
        updated_headers["Authorization"] = header
        return await handler(request.override(headers=updated_headers))

    return oauth_interceptor


async def get_initial_oauth_headers(runtime_mcp_config: ExtensionsConfig) -> dict[str, str]:
    """Get initial OAuth Authorization headers for MCP server connections."""
    token_manager = OAuthTokenManager.from_runtime_config(runtime_mcp_config)
    if not token_manager.has_oauth_servers():
        return {}

    headers: dict[str, str] = {}
    for server_name in token_manager.oauth_server_names():
        headers[server_name] = await token_manager.get_authorization_header(server_name) or ""

    return {name: value for name, value in headers.items() if value}
