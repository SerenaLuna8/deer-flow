"""Tests for MCP OAuth support."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from deerflow.mcp.config import ExtensionsConfig
from deerflow.mcp.oauth import (
    OAuthTokenManager,
    _OAuthToken,
    build_oauth_tool_interceptor,
    get_initial_oauth_headers,
)


class _MockResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _MockAsyncClient:
    def __init__(self, payload: dict[str, Any], post_calls: list[dict[str, Any]], **kwargs):
        self._payload = payload
        self._post_calls = post_calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, data: dict[str, Any]):
        self._post_calls.append({"url": url, "data": data})
        return _MockResponse(self._payload)


def test_oauth_token_manager_fetches_and_caches_token(monkeypatch):
    post_calls: list[dict[str, Any]] = []

    def _client_factory(*args, **kwargs):
        return _MockAsyncClient(
            payload={
                "access_token": "token-123",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
            post_calls=post_calls,
            **kwargs,
        )

    monkeypatch.setattr("httpx.AsyncClient", _client_factory)

    config = ExtensionsConfig.model_validate(
        {
            "mcpServers": {
                "secure-http": {
                    "enabled": True,
                    "type": "http",
                    "url": "https://api.example.com/mcp",
                    "oauth": {
                        "enabled": True,
                        "token_url": "https://auth.example.com/oauth/token",
                        "grant_type": "client_credentials",
                        "client_id": "client-id",
                        "client_secret": "client-secret",
                    },
                }
            }
        }
    )

    manager = OAuthTokenManager.from_runtime_config(config)

    first = asyncio.run(manager.get_authorization_header("secure-http"))
    second = asyncio.run(manager.get_authorization_header("secure-http"))

    assert first == "Bearer token-123"
    assert second == "Bearer token-123"
    assert len(post_calls) == 1
    assert post_calls[0]["url"] == "https://auth.example.com/oauth/token"
    assert post_calls[0]["data"]["grant_type"] == "client_credentials"


def test_oauth_token_manager_cancelled_same_loop_waiter_does_not_leak_lock():
    config = ExtensionsConfig.model_validate(
        {
            "mcpServers": {
                "secure-http": {
                    "enabled": True,
                    "type": "http",
                    "url": "https://api.example.com/mcp",
                    "oauth": {
                        "enabled": True,
                        "token_url": "https://auth.example.com/oauth/token",
                        "grant_type": "client_credentials",
                        "client_id": "client-id",
                        "client_secret": "client-secret",
                    },
                }
            }
        }
    )
    manager = OAuthTokenManager.from_runtime_config(config)
    fetch_started = asyncio.Event()
    allow_fetch = asyncio.Event()
    fetch_calls = 0

    async def fetch_token(_oauth):
        nonlocal fetch_calls
        fetch_calls += 1
        fetch_started.set()
        await allow_fetch.wait()
        return _OAuthToken(
            access_token="same-loop-token",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    manager._fetch_token = fetch_token  # type: ignore[method-assign]

    async def scenario() -> None:
        owner = asyncio.create_task(
            manager.get_authorization_header("secure-http"),
        )
        await fetch_started.wait()
        cancelled_waiter = asyncio.create_task(
            manager.get_authorization_header("secure-http"),
        )
        await asyncio.sleep(0)
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter
        allow_fetch.set()
        assert await owner == "Bearer same-loop-token"
        assert await manager.get_authorization_header("secure-http") == "Bearer same-loop-token"

    asyncio.run(scenario())

    assert fetch_calls == 1


def test_build_oauth_interceptor_injects_authorization_header(monkeypatch):
    post_calls: list[dict[str, Any]] = []

    def _client_factory(*args, **kwargs):
        return _MockAsyncClient(
            payload={
                "access_token": "token-abc",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
            post_calls=post_calls,
            **kwargs,
        )

    monkeypatch.setattr("httpx.AsyncClient", _client_factory)

    config = ExtensionsConfig.model_validate(
        {
            "mcpServers": {
                "secure-sse": {
                    "enabled": True,
                    "type": "sse",
                    "url": "https://api.example.com/mcp",
                    "oauth": {
                        "enabled": True,
                        "token_url": "https://auth.example.com/oauth/token",
                        "grant_type": "client_credentials",
                        "client_id": "client-id",
                        "client_secret": "client-secret",
                    },
                }
            }
        }
    )

    interceptor = build_oauth_tool_interceptor(config)
    assert interceptor is not None

    class _Request:
        def __init__(self):
            self.server_name = "secure-sse"
            self.headers = {"X-Test": "1"}

        def override(self, **kwargs):
            updated = _Request()
            updated.server_name = self.server_name
            updated.headers = kwargs.get("headers")
            return updated

    captured: dict[str, Any] = {}

    async def _handler(request):
        captured["headers"] = request.headers
        return "ok"

    result = asyncio.run(interceptor(_Request(), _handler))

    assert result == "ok"
    assert captured["headers"]["Authorization"] == "Bearer token-abc"
    assert captured["headers"]["X-Test"] == "1"


def test_get_initial_oauth_headers(monkeypatch):
    post_calls: list[dict[str, Any]] = []

    def _client_factory(*args, **kwargs):
        return _MockAsyncClient(
            payload={
                "access_token": "token-initial",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
            post_calls=post_calls,
            **kwargs,
        )

    monkeypatch.setattr("httpx.AsyncClient", _client_factory)

    config = ExtensionsConfig.model_validate(
        {
            "mcpServers": {
                "secure-http": {
                    "enabled": True,
                    "type": "http",
                    "url": "https://api.example.com/mcp",
                    "oauth": {
                        "enabled": True,
                        "token_url": "https://auth.example.com/oauth/token",
                        "grant_type": "client_credentials",
                        "client_id": "client-id",
                        "client_secret": "client-secret",
                    },
                },
                "no-oauth": {
                    "enabled": True,
                    "type": "http",
                    "url": "https://example.com/mcp",
                },
            }
        }
    )

    headers = asyncio.run(get_initial_oauth_headers(config))

    assert headers == {"secure-http": "Bearer token-initial"}
    assert len(post_calls) == 1
