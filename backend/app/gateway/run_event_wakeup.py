"""Per-process LISTEN/NOTIFY wakeup for durable private SSE consumers.

One dedicated PostgreSQL connection LISTENs on ``run_events`` and fans each
notification out to the in-process waiters registered for that ``run_id``.
NOTIFY is only an alarm clock (D5): the read path, cursor semantics, and
terminal dedup are untouched. Every failure mode — lost notification, broken
listener connection, no listener at all — leaves consumers on their poll
fallback, so correctness never depends on delivery.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from deerflow.runtime.events.store.db import RUN_EVENTS_NOTIFY_CHANNEL

logger = logging.getLogger(__name__)

_RECONNECT_BACKOFF_SECONDS = 1.0
_CONNECTION_PROBE_SECONDS = 1.0


async def _asyncpg_connect(dsn: str) -> Any:
    import asyncpg

    return await asyncpg.connect(dsn)


class RunEventWakeup:
    """Dispatch table ``run_id -> waiter events`` fed by one LISTEN connection."""

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[[str], Awaitable[Any]] | None = None,
        reconnect_backoff_seconds: float = _RECONNECT_BACKOFF_SECONDS,
        probe_seconds: float = _CONNECTION_PROBE_SECONDS,
    ) -> None:
        self._dsn = dsn
        self._connect = connect if connect is not None else _asyncpg_connect
        self._reconnect_backoff_seconds = reconnect_backoff_seconds
        self._probe_seconds = probe_seconds
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self._listening = False
        self._closed = False
        self._task: asyncio.Task[None] | None = None

    @property
    def listening(self) -> bool:
        """True while the LISTEN connection is believed healthy.

        Consumers use this to pick their idle timeout: a long event wait when
        healthy, the legacy poll cadence while degraded.
        """
        return self._listening

    def subscribe(self, run_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self._waiters.setdefault(run_id, set()).add(event)
        return event

    def unsubscribe(self, run_id: str, event: asyncio.Event) -> None:
        waiters = self._waiters.get(run_id)
        if waiters is None:
            return
        waiters.discard(event)
        if not waiters:
            self._waiters.pop(run_id, None)

    async def start(self) -> None:
        if self._closed or self._task is not None:
            return
        self._task = asyncio.create_task(
            self._listen_forever(),
            name="run-event-wakeup-listener",
        )

    async def aclose(self) -> None:
        self._closed = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # Never leave consumers parked on a listener that no longer exists.
        self._notify_all()

    def _notify(self, run_id: str) -> None:
        for event in self._waiters.get(run_id, ()):
            event.set()

    def _notify_all(self) -> None:
        for waiters in self._waiters.values():
            for event in waiters:
                event.set()

    def _on_notification(
        self,
        _connection: Any,
        _pid: int,
        channel: str,
        payload: str,
    ) -> None:
        if channel == RUN_EVENTS_NOTIFY_CHANNEL and isinstance(payload, str) and payload:
            self._notify(payload)

    async def _listen_forever(self) -> None:
        while not self._closed:
            connection = None
            try:
                connection = await self._connect(self._dsn)
                await connection.add_listener(
                    RUN_EVENTS_NOTIFY_CHANNEL,
                    self._on_notification,
                )
                self._listening = True
                # The (re)connected listener may have missed notifications;
                # wake everyone once so each consumer re-reads its cursor.
                self._notify_all()
                while not self._closed and not connection.is_closed():
                    await asyncio.sleep(self._probe_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "run_events listener degraded; consumers fall back to polling",
                    exc_info=True,
                )
            finally:
                self._listening = False
                if connection is not None:
                    with contextlib.suppress(Exception):
                        connection.terminate()
            if self._closed:
                return
            # Move parked waiters onto the degraded poll cadence right away
            # instead of letting them sit out the long event wait.
            self._notify_all()
            await asyncio.sleep(self._reconnect_backoff_seconds)
