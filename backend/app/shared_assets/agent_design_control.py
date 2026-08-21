"""In-process cancellation boundary for Gateway-owned Builder model calls."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field


@dataclass(slots=True)
class _GenerationControlEntry:
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    registered: bool = False


class AgentDesignGenerationControl:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: dict[
            tuple[uuid.UUID, str, uuid.UUID, uuid.UUID],
            _GenerationControlEntry,
        ] = {}

    @staticmethod
    def key(
        project_id: uuid.UUID,
        owner_user_id: str,
        session_id: uuid.UUID,
        operation_id: uuid.UUID,
    ) -> tuple[uuid.UUID, str, uuid.UUID, uuid.UUID]:
        return (project_id, owner_user_id, session_id, operation_id)

    async def register(
        self,
        key: tuple[uuid.UUID, str, uuid.UUID, uuid.UUID],
    ) -> asyncio.Event:
        async with self._lock:
            entry = self._entries.setdefault(key, _GenerationControlEntry())
            entry.registered = True
            return entry.abort

    async def request_stop(
        self,
        key: tuple[uuid.UUID, str, uuid.UUID, uuid.UUID],
    ) -> asyncio.Event | None:
        async with self._lock:
            entry = self._entries.setdefault(key, _GenerationControlEntry())
            entry.abort.set()
            return entry.done if entry.registered else None

    async def complete(
        self,
        key: tuple[uuid.UUID, str, uuid.UUID, uuid.UUID],
    ) -> None:
        async with self._lock:
            entry = self._entries.pop(key, None)
            if entry is not None:
                entry.done.set()


__all__ = ["AgentDesignGenerationControl"]
