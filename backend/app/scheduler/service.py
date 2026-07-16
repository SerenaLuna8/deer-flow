from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from app.automations.errors import AutomationConcurrencyLimit, AutomationError
from app.automations.ownership import AutomationSchedulerOwnership

logger = logging.getLogger(__name__)

SchedulerRuntimeStatus = Literal["stopped", "running", "ownership_lost"]


class ScheduledTaskService:
    """Occurrence-driven scheduler loop for project-private automations."""

    def __init__(
        self,
        *,
        occurrences,
        dispatcher,
        reconciler,
        poll_interval_seconds: float,
        max_concurrent_runs: int,
        ownership: AutomationSchedulerOwnership | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(poll_interval_seconds, (int, float)) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if type(max_concurrent_runs) is not int or max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be positive")
        self._occurrences = occurrences
        self._dispatcher = dispatcher
        self._reconciler = reconciler
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._max_concurrent_runs = max_concurrent_runs
        self._ownership = ownership
        self._clock = clock or (lambda: datetime.now(UTC))
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
        cursor = None
        while True:
            if self._ownership is not None:
                await self._ownership.verify()
            definitions = await self._occurrences.due_definitions(
                now=now,
                limit=self._max_concurrent_runs,
                after=cursor,
            )
            if not definitions:
                return
            for definition, scheduled_for in definitions:
                cursor = (
                    scheduled_for,
                    definition.project_id,
                    definition.owner_user_id,
                    definition.task_id,
                )
                try:
                    await self._dispatcher.admit_occurrence(
                        definition,
                        scheduled_for=scheduled_for,
                    )
                except AutomationConcurrencyLimit:
                    # Global active capacity is full. Preserve all later due
                    # definitions for the next poll instead of hot-scanning.
                    return
                except AutomationError as error:
                    # Admission is transactionally all-or-nothing. Keyset
                    # pagination prevents one persistent definition failure
                    # from starving later due work in the same poll.
                    logger.warning(
                        "Automation admission did not complete: code=%s",
                        error.code,
                    )
            if len(definitions) < self._max_concurrent_runs:
                return

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
