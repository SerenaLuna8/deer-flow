"""Secure HTTP client construction for admitted project MCP servers."""

from __future__ import annotations

from collections.abc import Callable

import httpx

SecureMcpHttpClientFactory = Callable[
    [dict[str, str] | None, httpx.Timeout | None, httpx.Auth | None],
    httpx.AsyncClient,
]


def make_secure_mcp_http_client_factory(
    *,
    proxy_url: str | None,
    timeout_seconds: int,
) -> SecureMcpHttpClientFactory:
    """Return an MCP-compatible client factory with operator hard limits.

    The MCP SDK's default client follows redirects and trusts environment proxy
    variables. Both behaviours bypass the project endpoint policy, so private
    runtime connections always override them. The adapter-provided timeout is
    deliberately ignored in favour of the operator-owned ceiling.
    """

    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
        raise ValueError("invalid MCP HTTP timeout")
    connect_timeout = min(5, timeout_seconds)

    def create_client(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        del timeout
        return httpx.AsyncClient(
            auth=auth,
            headers=headers,
            proxy=proxy_url,
            timeout=httpx.Timeout(
                timeout_seconds,
                connect=connect_timeout,
                read=timeout_seconds,
                write=timeout_seconds,
                pool=connect_timeout,
            ),
            follow_redirects=False,
            trust_env=False,
        )

    return create_client
