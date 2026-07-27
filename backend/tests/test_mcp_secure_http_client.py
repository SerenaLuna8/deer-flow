from __future__ import annotations

import asyncio

import httpx

from deerflow.mcp.http_security import make_secure_mcp_http_client_factory


def test_secure_mcp_http_client_disables_redirects_and_environment_proxies() -> None:
    factory = make_secure_mcp_http_client_factory(
        proxy_url=None,
        timeout_seconds=12,
    )

    client = factory(
        {"X-Test": "value"},
        httpx.Timeout(999),
        None,
    )
    try:
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert client.timeout.connect == 5
        assert client.timeout.read == 12
        assert client.timeout.write == 12
        assert client.timeout.pool == 5
        assert client.headers["X-Test"] == "value"
    finally:
        asyncio.run(client.aclose())


def test_secure_mcp_http_client_preserves_auth_without_using_adapter_timeout() -> None:
    auth = httpx.BasicAuth("operator", "credential")
    factory = make_secure_mcp_http_client_factory(
        proxy_url="http://egress-proxy.internal:3128",
        timeout_seconds=30,
    )

    client = factory(None, httpx.Timeout(3600), auth)
    try:
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert client.timeout.read == 30
        assert client.auth is auth
    finally:
        asyncio.run(client.aclose())


def test_secure_mcp_http_client_accepts_sdk_keyword_arguments() -> None:
    factory = make_secure_mcp_http_client_factory(
        proxy_url=None,
        timeout_seconds=9,
    )

    client = factory(
        headers={"X-Test": "value"},
        timeout=httpx.Timeout(999),
        auth=None,
    )
    try:
        assert client.timeout.read == 9
        assert client.headers["X-Test"] == "value"
    finally:
        asyncio.run(client.aclose())
