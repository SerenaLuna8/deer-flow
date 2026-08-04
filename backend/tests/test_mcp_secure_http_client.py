from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote_plus

import httpx
import pytest

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


@pytest.mark.anyio
async def test_secure_mcp_http_client_suppresses_query_secrets_in_transport_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "query secret/+sentinel"
    encoded_secret = quote_plus(secret)
    endpoint = f"https://mcp.example.test/mcp?key={encoded_secret}"
    factory = make_secure_mcp_http_client_factory(
        proxy_url=None,
        timeout_seconds=9,
    )
    client = factory(None, None, None)
    original_transport = client._transport

    class LoggedResponseStream(httpx.AsyncByteStream):
        def __init__(self, target: str) -> None:
            self._target = target

        async def __aiter__(self):
            logging.getLogger("httpcore.http11").debug(
                "receive_response_body.started target=%s",
                self._target,
            )
            yield b"{}"

    async def respond(request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpcore.http11").debug(
            "send_request_headers.started path=%s",
            request.url.raw_path,
        )
        return httpx.Response(
            200,
            request=request,
            stream=LoggedResponseStream(str(request.url)),
        )

    await original_transport.aclose()
    client._transport = httpx.MockTransport(respond)
    caplog.set_level(logging.DEBUG)
    try:
        async with client.stream("GET", endpoint) as response:
            assert response.status_code == 200
            assert await response.aread() == b"{}"
    finally:
        await client.aclose()

    assert secret not in caplog.text
    assert encoded_secret not in caplog.text
    assert "send_request_headers.started" not in caplog.text
    assert "receive_response_body.started" not in caplog.text
    assert "HTTP Request:" not in caplog.text

    caplog.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as ordinary_client:
        response = await ordinary_client.get("https://observable.example.test/mcp?probe=visible")
        assert response.status_code == 200

    assert "send_request_headers.started" in caplog.text
    assert "HTTP Request: GET https://observable.example.test/mcp?probe=visible" in caplog.text
