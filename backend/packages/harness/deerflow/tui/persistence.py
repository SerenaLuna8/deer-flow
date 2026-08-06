"""Explicitly project-scoped Thread metadata wiring for trusted TUI embeddings."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Awaitable
from typing import Any

from deerflow.private_scope import PrivateResourceScope


class _LoopThread:
    """A daemon thread running one asyncio event loop for database work."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run,
            name="deerflow-tui-db",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Awaitable[Any], *, timeout: float = 15.0) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)


class ThreadMetaWriter:
    """Write Thread metadata only with frozen project, owner, and Agent authority."""

    def __init__(
        self,
        loop: _LoopThread,
        store: Any,
        *,
        scope: PrivateResourceScope,
        agent_asset_id: uuid.UUID,
        agent_scope: str,
    ) -> None:
        if type(scope) is not PrivateResourceScope:
            raise TypeError("TUI Thread persistence requires PrivateResourceScope")
        if not isinstance(agent_asset_id, uuid.UUID):
            raise TypeError("TUI Thread persistence requires an Agent asset UUID")
        if agent_scope not in {"system", "project"}:
            raise ValueError("TUI Thread persistence requires a final Agent scope")
        if store is None:
            raise TypeError("TUI Thread persistence requires a scoped store")
        self._loop = loop
        self._store = store
        self._scope = scope
        self._agent_asset_id = agent_asset_id
        self._agent_scope = agent_scope

    @property
    def enabled(self) -> bool:
        return True

    @property
    def user_id(self) -> str:
        return self._scope.owner_user_id

    def ensure_created(
        self,
        thread_id: str,
        *,
        assistant_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        if not thread_id:
            return
        self._loop.run(self._ensure_created(thread_id, assistant_id, metadata))

    async def _ensure_created(
        self,
        thread_id: str,
        assistant_id: str | None,
        metadata: dict | None,
    ) -> None:
        existing = await self._store.get(thread_id, scope=self._scope)
        if existing is None:
            await self._store.create(
                thread_id,
                assistant_id=assistant_id,
                metadata=metadata or {"source": "tui"},
                scope=self._scope,
                agent_asset_id=self._agent_asset_id,
                agent_scope=self._agent_scope,
            )

    def set_title(self, thread_id: str, title: str) -> None:
        if not thread_id or not title:
            return
        self._loop.run(
            self._store.update_display_name(
                thread_id,
                title,
                scope=self._scope,
            )
        )
