from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.automations.dispatcher import (
    AdmittedAutomationOccurrence,
    SkippedAutomationOccurrence,
)
from app.automations.errors import (
    AutomationConcurrencyLimit,
    AutomationError,
    AutomationUnavailable,
)
from app.automations.ownership import AutomationSchedulerOwnership
from app.automations.system_policy import (
    AutomationsPolicyPort,
    AutomationsPolicyUnavailable,
    current_automations_policy,
)
from app.system_runtime_settings import AutomationsPolicyValue

logger = logging.getLogger(__name__)

AutomationOccurrence = AdmittedAutomationOccurrence | SkippedAutomationOccurrence


class AutomationSchedulerService:
    """Caller-transaction scheduler operations for project Automation."""

    def __init__(
        self,
        *,
        occurrences,
        dispatcher,
        reconciler,
        max_concurrent_runs: int,
        ownership: AutomationSchedulerOwnership | None = None,
        clock: Callable[[], datetime] | None = None,
        policy_reader: AutomationsPolicyPort | None = None,
    ) -> None:
        if type(max_concurrent_runs) is not int or max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be positive")
        self._occurrences = occurrences
        self._dispatcher = dispatcher
        self._reconciler = reconciler
        self._fallback_policy = AutomationsPolicyValue(
            max_concurrent_runs=max_concurrent_runs,
        )
        self._policy_reader = policy_reader
        self._ownership = ownership
        self._clock = clock or (lambda: datetime.now(UTC))

    async def reconcile_admitted_runs(self, session: AsyncSession) -> int:
        """Settle terminal admitted Runs without committing the session."""

        report = await self._reconciler.reconcile_admitted_runs(
            session,
            now=self._clock(),
        )
        return report.succeeded + report.failed + report.interrupted

    async def admit_due_occurrences(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> tuple[AutomationOccurrence, ...]:
        """Admit due occurrence/Run/job triples in one caller transaction."""

        cursor = None
        admitted: list[AutomationOccurrence] = []
        try:
            policy = await current_automations_policy(
                session,
                self._policy_reader,
                fallback=self._fallback_policy,
            )
        except AutomationsPolicyUnavailable as error:
            raise AutomationUnavailable("scheduler") from error
        max_concurrent_runs = policy.max_concurrent_runs
        while True:
            if self._ownership is not None:
                await self._ownership.verify()
            definitions = await self._occurrences.due_definitions_in_session(
                session,
                now=now,
                limit=max_concurrent_runs,
                after=cursor,
            )
            if not definitions:
                return tuple(admitted)
            for definition, scheduled_for in definitions:
                cursor = (
                    scheduled_for,
                    definition.project_id,
                    definition.owner_user_id,
                    definition.task_id,
                )
                try:
                    async with session.begin_nested():
                        result = await self._dispatcher.admit_occurrence_in_session(
                            session,
                            definition,
                            scheduled_for=scheduled_for,
                        )
                except AutomationConcurrencyLimit:
                    return tuple(admitted)
                except AutomationError as error:
                    logger.warning(
                        "Automation admission did not complete: code=%s",
                        error.code,
                    )
                else:
                    admitted.append(result)
            if len(definitions) < max_concurrent_runs:
                return tuple(admitted)


__all__ = ["AutomationOccurrence", "AutomationSchedulerService"]
