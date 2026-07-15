from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from app.automations.errors import AutomationError
from app.automations.ownership import AutomationSchedulerOwnership

logger = logging.getLogger(__name__)

SchedulerRuntimeStatus = Literal["stopped", "running", "ownership_lost"]


class ScheduledTaskService:
    """Occurrence-driven scheduler loop for project-private automations."""

    def __init__(
        self,
        *,
        app,
        occurrences,
        dispatcher,
        reconciler,
        poll_interval_seconds: float,
        lease_seconds: int,
        max_concurrent_runs: int,
        ownership: AutomationSchedulerOwnership | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_owner: str | None = None,
    ) -> None:
        if not isinstance(poll_interval_seconds, (int, float)) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if type(max_concurrent_runs) is not int or max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be positive")
        self.app = app
        self._occurrences = occurrences
        self._dispatcher = dispatcher
        self._reconciler = reconciler
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._lease_seconds = lease_seconds
        self._max_concurrent_runs = max_concurrent_runs
        self._ownership = ownership
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease_owner = lease_owner or f"{socket.gethostname()}:{uuid.uuid4().hex}"
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def status(self) -> SchedulerRuntimeStatus:
        if self._ownership is not None and self._ownership.is_lost:
            return "ownership_lost"
        if self._task is not None and not self._task.done():
            return "running"
        return "stopped"

    async def run_once(self, *, now: datetime) -> None:
        if self._ownership is not None:
            await self._ownership.verify()
        await self._occurrences.reserve_due(
            now=now,
            limit=self._max_concurrent_runs,
        )
        for _ in range(self._max_concurrent_runs):
            occurrence = await self._occurrences.claim_next(
                now=now,
                lease_owner=self._lease_owner,
                lease_seconds=self._lease_seconds,
            )
            if occurrence is None:
                break
            try:
                await self._dispatcher.dispatch(occurrence.id, app=self.app)
            except AutomationError as error:
                # Dispatch already persists retry/rejection policy. Keep the
                # poll moving without logging private identifiers or prompts.
                logger.warning(
                    "Automation dispatch did not complete: code=%s",
                    error.code,
                )

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._ownership is not None:
            await self._ownership.verify()
        await self._reconciler.reconcile_restart(self._clock())
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="project-automation-scheduler",
        )

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once(now=self._clock())
            except asyncio.CancelledError:
                raise
            except AutomationError as error:
                if self._ownership is not None and self._ownership.is_lost:
                    logger.error(
                        "Automation scheduler ownership lost; polling stopped: code=%s",
                        error.code,
                    )
                    return
                logger.error(
                    "Automation scheduler poll failed: code=%s",
                    error.code,
                )
            except Exception as error:  # noqa: BLE001 - loop remains available
                logger.error(
                    "Automation scheduler poll failed: error_type=%s",
                    type(error).__name__,
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                pass


__all__ = ["ScheduledTaskService", "SchedulerRuntimeStatus"]
