from __future__ import annotations

import asyncio
import gc
import io
import logging
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus

import httpx
import pytest

from deerflow.mcp import http_security
from deerflow.mcp.config import McpOAuthConfig
from deerflow.mcp.http_security import (
    _PROJECT_MCP_HTTP_LOG_FILTER,
    _install_project_mcp_http_log_filter,
    make_secure_mcp_http_client_factory,
)
from deerflow.mcp.oauth import OAuthTokenManager
from deerflow.mcp_definition_policy import ExactMcpEndpointPolicy, McpDefinitionPolicyError


def _new_transport_logger() -> logging.Logger:
    return logging.getLogger(f"httpx.codex_test_{uuid.uuid4().hex}")


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
async def test_oauth_token_uses_admitted_endpoint_and_secure_client_factory() -> None:
    token_url = "https://identity.example.test/token"
    observed: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "access_token": "access-token",
                "token_type": "Bearer",
                "expires_in": 3600,
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, *, data: dict[str, str]):
            observed["url"] = url
            observed["data"] = data
            return _Response()

    def factory(headers, timeout, auth):
        observed["factory"] = (headers, timeout, auth)
        return _Client()

    manager = OAuthTokenManager(
        {
            "catalog": McpOAuthConfig(
                token_url=token_url,
                client_id="client-id",
                client_secret="client-secret",
            ),
        },
        endpoint_policy=ExactMcpEndpointPolicy(frozenset({token_url})),
        http_client_factory=factory,
    )

    assert await manager.get_authorization_header("catalog") == "Bearer access-token"
    assert observed["url"] == token_url
    assert observed["data"] == {
        "grant_type": "client_credentials",
        "client_id": "client-id",
        "client_secret": "client-secret",
    }
    headers, timeout, auth = observed["factory"]
    assert headers is None
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 15
    assert auth is None


@pytest.mark.anyio
async def test_oauth_token_rejects_unadmitted_endpoint_before_client_creation() -> None:
    called = False

    def factory(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("client must not be created")

    manager = OAuthTokenManager(
        {
            "catalog": McpOAuthConfig(
                token_url="https://identity.example.test/token",
                client_id="client-id",
                client_secret="client-secret",
            ),
        },
        endpoint_policy=ExactMcpEndpointPolicy(
            frozenset({"https://other.example.test/token"}),
        ),
        http_client_factory=factory,
    )

    with pytest.raises(McpDefinitionPolicyError):
        await manager.get_authorization_header("catalog")
    assert called is False


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


def test_filter_covers_root_handler_added_after_initial_install() -> None:
    _install_project_mcp_http_log_filter()
    handler = logging.NullHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        assert _PROJECT_MCP_HTTP_LOG_FILTER not in handler.filters

        _install_project_mcp_http_log_filter()

        assert handler.filters.count(_PROJECT_MCP_HTTP_LOG_FILTER) == 1
    finally:
        root.removeHandler(handler)


def test_filter_covers_new_transport_logger_and_late_handler() -> None:
    _install_project_mcp_http_log_filter()
    logger = _new_transport_logger()
    _install_project_mcp_http_log_filter()
    assert logger.filters.count(_PROJECT_MCP_HTTP_LOG_FILTER) == 1

    handler = logging.NullHandler()
    logger.addHandler(handler)
    try:
        assert _PROJECT_MCP_HTTP_LOG_FILTER not in handler.filters

        _install_project_mcp_http_log_filter()

        assert handler.filters.count(_PROJECT_MCP_HTTP_LOG_FILTER) == 1
    finally:
        logger.removeHandler(handler)


def test_filter_discovers_transport_placeholder_conversion_without_size_change() -> None:
    parent_name = f"httpcore.codex_parent_{uuid.uuid4().hex}"
    child = logging.getLogger(f"{parent_name}.child")
    del child
    _install_project_mcp_http_log_filter()
    size_before = len(logging.Logger.manager.loggerDict)

    parent = logging.getLogger(parent_name)
    assert len(logging.Logger.manager.loggerDict) == size_before
    handler = logging.NullHandler()
    parent.addHandler(handler)
    try:
        _install_project_mcp_http_log_filter()

        assert parent.filters.count(_PROJECT_MCP_HTTP_LOG_FILTER) == 1
        assert handler.filters.count(_PROJECT_MCP_HTTP_LOG_FILTER) == 1
    finally:
        parent.removeHandler(handler)


def test_filter_installation_is_concurrent_and_duplicate_safe() -> None:
    logger = _new_transport_logger()
    handler = logging.NullHandler()
    logger.addHandler(handler)
    try:
        with ThreadPoolExecutor(max_workers=16) as executor:
            tuple(executor.map(lambda _index: _install_project_mcp_http_log_filter(), range(128)))

        assert logger.filters.count(_PROJECT_MCP_HTTP_LOG_FILTER) == 1
        assert handler.filters.count(_PROJECT_MCP_HTTP_LOG_FILTER) == 1
    finally:
        logger.removeHandler(handler)


def test_incremental_filter_cache_does_not_retain_logger_or_handler() -> None:
    logger = _new_transport_logger()
    logger_name = logger.name
    handler = logging.NullHandler()
    logger.addHandler(handler)
    _install_project_mcp_http_log_filter()
    logger.removeHandler(handler)

    handler_reference = weakref.ref(handler)
    logger_reference = weakref.ref(logger)
    logging.Logger.manager.loggerDict.pop(logger_name, None)
    del handler
    del logger
    gc.collect()

    assert handler_reference() is None
    assert logger_reference() is None


def test_hot_path_does_not_rescan_all_unrelated_loggers(monkeypatch) -> None:
    prefix = f"codex_http_filter_bench_{uuid.uuid4().hex}_"
    logger_names = [f"{prefix}{index}" for index in range(10_000)]
    for name in logger_names:
        logging.getLogger(name)
    _install_project_mcp_http_log_filter()

    def unexpected_full_scan(*_args, **_kwargs):
        raise AssertionError("stable logger registry must not be rescanned")

    monkeypatch.setattr(
        http_security,
        "_discover_transport_loggers",
        unexpected_full_scan,
    )
    try:
        for _ in range(100):
            _install_project_mcp_http_log_filter()
    finally:
        for name in logger_names:
            logging.Logger.manager.loggerDict.pop(name, None)


@pytest.mark.anyio
async def test_late_transport_handler_cannot_capture_query_secret() -> None:
    _install_project_mcp_http_log_filter()
    logger = _new_transport_logger()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    logger.addHandler(handler)

    secret = "late-handler-query-secret"

    async def respond(request: httpx.Request) -> httpx.Response:
        logger.debug("transport target=%s", request.url)
        return httpx.Response(200, request=request, json={})

    factory = make_secure_mcp_http_client_factory(
        proxy_url=None,
        timeout_seconds=9,
    )
    client = factory(None, None, None)
    original_transport = client._transport
    await original_transport.aclose()
    client._transport = httpx.MockTransport(respond)
    try:
        response = await client.get(f"https://mcp.example.test/mcp?key={secret}")
        assert response.status_code == 200
        assert secret not in output.getvalue()

        logger.debug("ordinary transport observation")
        assert "ordinary transport observation" in output.getvalue()
    finally:
        await client.aclose()
        logger.removeHandler(handler)
