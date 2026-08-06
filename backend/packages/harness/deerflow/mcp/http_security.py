"""Secure HTTP client construction for admitted project MCP servers."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import httpx

SecureMcpHttpClientFactory = Callable[
    [dict[str, str] | None, httpx.Timeout | None, httpx.Auth | None],
    httpx.AsyncClient,
]

_SUPPRESS_PROJECT_MCP_HTTP_LOGS: ContextVar[bool] = ContextVar(
    "suppress_project_mcp_http_logs",
    default=False,
)


def _is_http_transport_logger(name: str) -> bool:
    return name in {"httpx", "httpcore"} or name.startswith(("httpx.", "httpcore."))


class _ProjectMcpHttpLogFilter(logging.Filter):
    """Suppress transport records only while a project MCP request is active.

    httpx logs the complete request URL at INFO, and httpcore may log the
    request target at DEBUG. Query Credential values are intentionally present
    in that one-shot URL, so neither record is safe to emit. A ContextVar keeps
    the suppression scoped to this client and preserves concurrent, unrelated
    HTTP observability.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not (_SUPPRESS_PROJECT_MCP_HTTP_LOGS.get() and _is_http_transport_logger(record.name))


_PROJECT_MCP_HTTP_LOG_FILTER = _ProjectMcpHttpLogFilter()


def _install_project_mcp_http_log_filter() -> None:
    """Cover the configured root handlers and any library-owned handlers."""

    handlers: set[logging.Handler] = set(logging.getLogger().handlers)
    loggers: set[logging.Logger] = {
        logging.getLogger("httpx"),
        logging.getLogger("httpcore"),
    }
    for value in logging.Logger.manager.loggerDict.values():
        if isinstance(value, logging.Logger) and _is_http_transport_logger(value.name):
            loggers.add(value)
    for logger in loggers:
        handlers.update(logger.handlers)
        if _PROJECT_MCP_HTTP_LOG_FILTER not in logger.filters:
            logger.addFilter(_PROJECT_MCP_HTTP_LOG_FILTER)
    for handler in handlers:
        if _PROJECT_MCP_HTTP_LOG_FILTER not in handler.filters:
            handler.addFilter(_PROJECT_MCP_HTTP_LOG_FILTER)


class _ProjectMcpAsyncClient(httpx.AsyncClient):
    """AsyncClient whose transport logs cannot serialize query Credentials."""

    async def send(
        self,
        request: httpx.Request,
        *,
        stream: bool = False,
        auth: Any = httpx.USE_CLIENT_DEFAULT,
        follow_redirects: Any = httpx.USE_CLIENT_DEFAULT,
    ) -> httpx.Response:
        _install_project_mcp_http_log_filter()
        token = _SUPPRESS_PROJECT_MCP_HTTP_LOGS.set(True)
        try:
            return await super().send(
                request,
                stream=stream,
                auth=auth,
                follow_redirects=follow_redirects,
            )
        finally:
            _SUPPRESS_PROJECT_MCP_HTTP_LOGS.reset(token)

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: httpx.URL | str,
        **kwargs: Any,
    ) -> AsyncIterator[httpx.Response]:
        _install_project_mcp_http_log_filter()
        token = _SUPPRESS_PROJECT_MCP_HTTP_LOGS.set(True)
        try:
            async with super().stream(method, url, **kwargs) as response:
                yield response
        finally:
            _SUPPRESS_PROJECT_MCP_HTTP_LOGS.reset(token)


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
        return _ProjectMcpAsyncClient(
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
