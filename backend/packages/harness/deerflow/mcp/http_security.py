"""Secure HTTP client construction for admitted project MCP servers."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from threading import RLock
from typing import Any
from weakref import WeakKeyDictionary, WeakSet

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

# Logging's manager owns Logger/PlaceHolder lifetimes. These caches must not
# extend them: applications and tests may install short-lived transports and
# handlers. Root handlers are intentionally read directly on every sensitive
# request, while known transport loggers are tracked weakly so a handler added
# after startup is covered before the next request.
_PROJECT_MCP_HTTP_FILTER_LOCK = RLock()
_KNOWN_TRANSPORT_LOGGERS: WeakSet[logging.Logger] = WeakSet()
_KNOWN_TRANSPORT_PLACEHOLDERS: WeakKeyDictionary[object, str] = WeakKeyDictionary()
_KNOWN_TRANSPORT_PLACEHOLDER_COUNT = 0
_LOGGER_REGISTRY_SIZE = -1


def _ensure_filter(target: logging.Filterer) -> None:
    if _PROJECT_MCP_HTTP_LOG_FILTER not in target.filters:
        target.addFilter(_PROJECT_MCP_HTTP_LOG_FILTER)


def _cover_transport_logger(logger: logging.Logger) -> None:
    _KNOWN_TRANSPORT_LOGGERS.add(logger)
    _ensure_filter(logger)
    for handler in tuple(logger.handlers):
        _ensure_filter(handler)


def _discover_transport_loggers(logger_registry: dict[str, object]) -> None:
    """Refresh weak transport caches from one stable registry snapshot."""

    global _KNOWN_TRANSPORT_PLACEHOLDER_COUNT

    _KNOWN_TRANSPORT_PLACEHOLDERS.clear()
    # ``dict.copy`` takes a CPython-GIL-protected snapshot. Logger creation may
    # continue immediately afterwards; the registry-size/placeholder checks on
    # the next sensitive request will discover those additions.
    for name, value in logger_registry.copy().items():
        if not _is_http_transport_logger(name):
            continue
        if isinstance(value, logging.Logger):
            _KNOWN_TRANSPORT_LOGGERS.add(value)
        else:
            try:
                _KNOWN_TRANSPORT_PLACEHOLDERS[value] = name
            except TypeError:
                # A custom logging Manager may use a non-weak-referenceable
                # placeholder. Do not cache it; the size check still covers
                # normal logger creation without retaining the object.
                continue
    _KNOWN_TRANSPORT_PLACEHOLDER_COUNT = len(_KNOWN_TRANSPORT_PLACEHOLDERS)


def _transport_placeholder_changed(logger_registry: dict[str, object]) -> bool:
    if len(_KNOWN_TRANSPORT_PLACEHOLDERS) != _KNOWN_TRANSPORT_PLACEHOLDER_COUNT:
        return True
    return any(logger_registry.get(name) is not placeholder for placeholder, name in tuple(_KNOWN_TRANSPORT_PLACEHOLDERS.items()))


def _install_project_mcp_http_log_filter() -> None:
    """Cover current handlers without rescanning every unrelated logger."""

    global _LOGGER_REGISTRY_SIZE

    with _PROJECT_MCP_HTTP_FILTER_LOCK:
        root = logging.getLogger()
        for handler in tuple(root.handlers):
            _ensure_filter(handler)
        if logging.lastResort is not None:
            _ensure_filter(logging.lastResort)

        # Always materialize and cover the two base loggers. If either replaces
        # a cached PlaceHolder without changing registry size, the identity
        # check below still forces discovery of its descendants.
        _cover_transport_logger(logging.getLogger("httpx"))
        _cover_transport_logger(logging.getLogger("httpcore"))

        logger_registry = logging.Logger.manager.loggerDict
        registry_size = len(logger_registry)
        if registry_size != _LOGGER_REGISTRY_SIZE or _transport_placeholder_changed(logger_registry):
            _discover_transport_loggers(logger_registry)
            _LOGGER_REGISTRY_SIZE = len(logger_registry)

        # This relevant-only walk is required: a library may attach a handler
        # to an existing httpx/httpcore child after process startup. WeakSet
        # avoids retaining dynamically removed loggers.
        for logger in tuple(_KNOWN_TRANSPORT_LOGGERS):
            _cover_transport_logger(logger)


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
