"""Run-scoped reuse of initialized project MCP sessions (U3).

Project MCP tool calls historically opened a fresh ``http``/``sse`` client per
call: connect, ``initialize``, ``tools/list``, one ``tools/call``, close. This
cache keeps one initialized session per exact snapshot closure for the life of
the Run, anchored to the ``PrivateAgentRuntime`` materialized scope.

Security boundaries are unchanged by design:

- every call still revalidates the persisted snapshot and grants in
  PostgreSQL and re-materializes secrets before dispatch
  (``invoke_with_mcp_material``);
- the cache key is ``(version_id, payload_checksum, grant_closure_digest)``,
  so any closure drift builds a fresh session instead of reusing one;
- MCP sessions are not concurrency-safe, so calls on one session are
  serialized through a per-session lock;
- the Run-end ``aclose`` closes every client and clears the derived-secret
  closures; an idle session is proactively closed after five minutes.

Transport failures discard the session and rebuild exactly once before the
existing public error path takes over. Application-level outcomes
(``PrivateWorkError``, ``AuthorizationRevoked``, ``ToolException``) never
tear down the transport.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import ToolException

from app.private_work.errors import PrivateWorkError, PrivateWorkUnavailable
from deerflow.sandbox.sandbox import AuthorizationRevoked

logger = logging.getLogger(__name__)

MCP_RUN_SESSION_IDLE_CLOSE_SECONDS = 300.0
_CLOSE_TIMEOUT_SECONDS = 1.0

# (version_id, payload_checksum, grant_closure_digest)
McpRunSessionKey = tuple[uuid.UUID, str, str]
# (client, tools, live derived-secrets list shared with OAuth interceptors)
McpRunSession = tuple[object, tuple[object, ...], list[str]]


@dataclass
class _SessionHolder:
    """One cache slot; ``lock`` serializes build and every tool call."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    session: McpRunSession | None = None
    last_used_at: float = 0.0
    idle_task: asyncio.Task | None = None


class McpRunSessionCache:
    """Per-Run cache of initialized project MCP sessions."""

    def __init__(self, *, idle_close_seconds: float = MCP_RUN_SESSION_IDLE_CLOSE_SECONDS) -> None:
        self._idle_close_seconds = idle_close_seconds
        self._holders: dict[McpRunSessionKey, _SessionHolder] = {}
        self._registry_lock = asyncio.Lock()
        self._closed = False

    @property
    def active_session_count(self) -> int:
        return sum(1 for holder in self._holders.values() if holder.session is not None)

    async def call(
        self,
        key: McpRunSessionKey,
        open_session: Callable[[], Awaitable[McpRunSession]],
        operation: Callable[[tuple[object, ...], list[str]], Awaitable[Any]],
        *,
        call_timeout_seconds: float | None = None,
    ) -> Any:
        """Run ``operation`` on the cached session, building it on first use."""

        holder = await self._holder_for(key)
        async with holder.lock:
            self._cancel_idle_close(holder)
            rebuilt_once = False
            while True:
                if self._closed:
                    raise PrivateWorkUnavailable("unknown")
                if holder.session is None:
                    holder.session = await open_session()
                _client, tools, derived_secrets = holder.session
                try:
                    if call_timeout_seconds is None:
                        result = await operation(tools, derived_secrets)
                    else:
                        async with asyncio.timeout(call_timeout_seconds):
                            result = await operation(tools, derived_secrets)
                except (PrivateWorkError, AuthorizationRevoked, ToolException):
                    # Application-level outcome: the transport stays healthy.
                    self._mark_used(key, holder)
                    raise
                except TimeoutError:
                    # A hung call leaves the stream undrained; the session is gone.
                    await self._close_session(holder)
                    raise
                except asyncio.CancelledError:
                    await self._close_session(holder)
                    raise
                except Exception:
                    # Transport error: discard, rebuild once, then give up to
                    # the caller's existing public error path.
                    await self._close_session(holder)
                    if rebuilt_once:
                        raise
                    rebuilt_once = True
                    continue
                self._mark_used(key, holder)
                return result

    async def aclose(self) -> None:
        """Close every session and refuse further reuse; safe to call twice."""

        async with self._registry_lock:
            if self._closed:
                return
            self._closed = True
            holders = list(self._holders.values())
            self._holders.clear()
        for holder in holders:
            self._cancel_idle_close(holder)
            await self._close_session(holder)

    async def _holder_for(self, key: McpRunSessionKey) -> _SessionHolder:
        async with self._registry_lock:
            if self._closed:
                raise PrivateWorkUnavailable("unknown")
            holder = self._holders.get(key)
            if holder is None:
                holder = _SessionHolder()
                self._holders[key] = holder
            return holder

    def _mark_used(self, key: McpRunSessionKey, holder: _SessionHolder) -> None:
        holder.last_used_at = time.monotonic()
        if self._idle_close_seconds <= 0 or self._closed or holder.session is None:
            return
        marker = holder.last_used_at
        holder.idle_task = asyncio.create_task(self._idle_close(key, holder, marker))

    def _cancel_idle_close(self, holder: _SessionHolder) -> None:
        task, holder.idle_task = holder.idle_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _idle_close(self, key: McpRunSessionKey, holder: _SessionHolder, marker: float) -> None:
        try:
            await asyncio.sleep(self._idle_close_seconds)
            async with holder.lock:
                if holder.last_used_at != marker or holder.session is None:
                    return
                await self._close_session(holder)
            async with self._registry_lock:
                if self._holders.get(key) is holder and holder.session is None:
                    self._holders.pop(key, None)
        except asyncio.CancelledError:
            pass

    @staticmethod
    async def _close_session(holder: _SessionHolder) -> None:
        session, holder.session = holder.session, None
        if session is None:
            return
        client, _tools, derived_secrets = session
        try:
            close = getattr(client, "aclose", None)
            if callable(close):
                async with asyncio.timeout(_CLOSE_TIMEOUT_SECONDS):
                    await close()
        except Exception:
            logger.debug("Closing a project MCP run session failed", exc_info=True)
        finally:
            derived_secrets.clear()


__all__ = [
    "MCP_RUN_SESSION_IDLE_CLOSE_SECONDS",
    "McpRunSession",
    "McpRunSessionCache",
    "McpRunSessionKey",
]
