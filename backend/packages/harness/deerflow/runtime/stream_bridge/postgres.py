"""PostgreSQL-backed durable stream bridge over the unified Run event log."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.models.run_event import RunEventRow
from deerflow.runtime.events.models import (
    StoredStreamFrame,
    StreamClosed,
    StreamCursorOutOfRange,
    StreamFrame,
    StreamLeaseProof,
    StreamScopeNotFound,
    StreamScopeRequired,
    StreamWriteAuthorityRequired,
)
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.stream_bridge.base import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    StreamBridge,
    StreamEvent,
)

logger = logging.getLogger(__name__)
_CANONICAL_CURSOR = re.compile(r"0|[1-9][0-9]*")
_MAX_STREAM_CURSOR = (1 << 63) - 1
_MAX_STREAM_CURSOR_DIGITS = len(str(_MAX_STREAM_CURSOR))


def parse_stream_cursor(value: str) -> int:
    """Parse one canonical cursor that is safe for PostgreSQL BIGINT binds."""

    if len(value) > _MAX_STREAM_CURSOR_DIGITS or _CANONICAL_CURSOR.fullmatch(value) is None:
        raise ValueError(
            "durable stream cursor must be canonical ASCII decimal within PostgreSQL BIGINT range",
        )
    cursor = int(value)
    if cursor > _MAX_STREAM_CURSOR:
        raise ValueError(
            "durable stream cursor must be canonical ASCII decimal within PostgreSQL BIGINT range",
        )
    return cursor


class StreamNotifier(Protocol):
    async def best_effort_notify(
        self,
        thread_id: str,
        event_id: str,
    ) -> None: ...


class _NoopNotifier:
    async def best_effort_notify(
        self,
        thread_id: str,
        event_id: str,
    ) -> None:
        return None


class PostgresStreamBridge(StreamBridge):
    """Store-first stream transport; notification only lowers latency."""

    supports_cross_process = True

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        notifier: StreamNotifier | None = None,
    ) -> None:
        self._sessions = session_factory
        self._events = DbRunEventStore(session_factory)
        self._notifier = notifier or _NoopNotifier()

    async def _notify(self, frame: StoredStreamFrame) -> None:
        if not frame.created:
            return
        try:
            await self._notifier.best_effort_notify(frame.thread_id, frame.id)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - notification is non-authoritative
            logger.warning(
                "Durable stream notification failed after commit: error_type=%s",
                type(error).__name__,
            )

    async def publish_frame(
        self,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
        frame: StreamFrame,
        *,
        lease: StreamLeaseProof | None = None,
    ) -> StoredStreamFrame:
        async with self._sessions() as session, session.begin():
            stored = await self._events.append_stream_frame(
                session,
                scope=scope,
                thread_id=thread_id,
                run_id=run_id,
                frame=frame,
                lease=lease,
            )
        await self._notify(stored)
        return stored

    async def publish(
        self,
        run_id: str,
        event: str,
        data: Any,
    ) -> StoredStreamFrame:
        del run_id, event, data
        raise StreamScopeRequired(
            "durable private stream publish requires project, owner, and thread scope",
        )

    async def publish_terminal(
        self,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
        *,
        status: str,
        lease: StreamLeaseProof | None = None,
    ) -> StoredStreamFrame:
        return await self.publish_frame(
            scope,
            thread_id,
            run_id,
            StreamFrame.end(status=status),
            lease=lease,
        )

    async def ensure_settled_terminal(
        self,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
        *,
        status: str,
    ) -> StoredStreamFrame:
        """Repair a missing terminal frame from settled database authority."""

        async with self._sessions() as session, session.begin():
            stored = await self._events.ensure_settled_stream_terminal(
                session,
                scope=scope,
                thread_id=thread_id,
                run_id=run_id,
                status=status,
            )
        await self._notify(stored)
        return stored

    async def publish_end(self, run_id: str) -> StoredStreamFrame:
        del run_id
        raise StreamScopeRequired(
            "durable private stream terminal requires project, owner, and thread scope",
        )

    async def read_after(
        self,
        scope: PrivateResourceScope,
        thread_id: str,
        *,
        cursor: int,
        limit: int,
        run_id: str | None = None,
    ) -> tuple[StoredStreamFrame, ...]:
        async with self._sessions() as session:
            return await self._events.list_stream_frames(
                session,
                scope=scope,
                thread_id=thread_id,
                cursor=cursor,
                limit=limit,
                run_id=run_id,
            )

    async def stream_exists_scoped(
        self,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
    ) -> bool:
        project_id, owner_user_id = self._events._coordinates(scope)
        async with self._sessions() as session:
            return bool(
                await session.scalar(
                    sa.select(RunEventRow.id).where(
                        RunEventRow.project_id == project_id,
                        RunEventRow.owner_user_id == owner_user_id,
                        RunEventRow.thread_id == thread_id,
                        RunEventRow.run_id == run_id,
                        RunEventRow.category == "stream",
                    )
                )
            )

    async def stream_exists(self, run_id: str) -> bool:
        del run_id
        raise StreamScopeRequired(
            "durable private stream lookup requires project, owner, and thread scope",
        )

    def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        del run_id, last_event_id, heartbeat_interval
        raise StreamScopeRequired(
            "durable private stream subscription requires project, owner, and thread scope",
        )

    async def subscribe_scoped(
        self,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        cursor = 0 if last_event_id is None else parse_stream_cursor(last_event_id)
        while True:
            frames = await self.read_after(
                scope,
                thread_id,
                cursor=cursor,
                limit=100,
                run_id=run_id,
            )
            if frames:
                for frame in frames:
                    cursor = int(frame.id)
                    if frame.terminal:
                        yield END_SENTINEL
                        return
                    yield StreamEvent(
                        id=frame.id,
                        event=frame.event,
                        data=frame.data,
                    )
                continue
            await asyncio.sleep(max(0.001, heartbeat_interval))
            yield HEARTBEAT_SENTINEL

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        # Durable frames are event facts and are removed only by retention.
        if delay > 0:
            await asyncio.sleep(delay)


__all__ = [
    "PostgresStreamBridge",
    "StreamClosed",
    "StreamCursorOutOfRange",
    "StreamScopeRequired",
    "StreamScopeNotFound",
    "StreamWriteAuthorityRequired",
]
