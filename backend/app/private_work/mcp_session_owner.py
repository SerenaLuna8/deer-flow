"""Task-affine lifecycle owner for one MCP adapter session."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger("app.private_work.asset_runtime")

_MCP_CLOSE_TIMEOUT_SECONDS = 1


class RunMcpClientSessionOwner:
    """Enter and exit one adapter session from the same asyncio task."""

    __slots__ = (
        "_close_event",
        "_closed",
        "_load_tools",
        "_owner_task",
        "_ready",
        "_session_context",
    )

    def __init__(
        self,
        session_context: object,
        load_tools: Callable[
            [object],
            Awaitable[tuple[object, ...]],
        ],
    ) -> None:
        loop = asyncio.get_running_loop()
        self._close_event = asyncio.Event()
        self._closed = False
        self._load_tools: (
            Callable[
                [object],
                Awaitable[tuple[object, ...]],
            ]
            | None
        ) = load_tools
        self._ready: asyncio.Future[tuple[object, ...]] = loop.create_future()
        self._session_context: object | None = session_context
        self._owner_task = loop.create_task(self._run())

    @classmethod
    async def open(
        cls,
        session_context: object,
        load_tools: Callable[
            [object],
            Awaitable[tuple[object, ...]],
        ],
    ) -> tuple[RunMcpClientSessionOwner, tuple[object, ...]]:
        owner = cls(session_context, load_tools)
        try:
            tools = await asyncio.shield(owner._ready)
        except BaseException:
            await owner._abort_open()
            raise
        return owner, tools

    async def _run(self) -> None:
        session_context = self._session_context
        load_tools = self._load_tools
        try:
            if session_context is None or load_tools is None:
                raise RuntimeError("MCP session owner is unavailable")
            async with session_context as session:  # type: ignore[attr-defined]
                tools = await load_tools(session)
                if not self._ready.done():
                    self._ready.set_result(tools)
                await self._close_event.wait()
        except BaseException as error:
            if not self._ready.done():
                self._ready.set_exception(error)
            elif not isinstance(error, asyncio.CancelledError):
                logger.debug(
                    "Project MCP session owner stopped unexpectedly",
                    exc_info=True,
                )
        finally:
            # Drop closures containing the materialized URL and headers after
            # transport teardown.
            self._load_tools = None
            self._session_context = None

    async def _abort_open(self) -> None:
        self._closed = True
        self._close_event.set()
        self._owner_task.cancel()
        try:
            async with asyncio.timeout(_MCP_CLOSE_TIMEOUT_SECONDS):
                await asyncio.shield(self._owner_task)
        except BaseException:
            pass

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_event.set()
        await asyncio.shield(self._owner_task)


__all__ = ["RunMcpClientSessionOwner"]
