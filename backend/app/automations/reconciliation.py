from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.automations.errors import AutomationUnavailable
from app.automations.execution_authority import (
    AutomationExecutionAuthority,
    automation_retry_denial,
    lock_automation_execution_authority,
)
from app.automations.occurrences import deterministic_run_id, deterministic_thread_id
from app.private_work.run_repository import PrivateRunRecord, PrivateRunRepository
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.scheduled_task_runs import (
    TERMINAL_OCCURRENCE_STATUSES,
    ScheduledTaskRunRecord,
    ScheduledTaskRunRepository,
    ScheduledTaskRunRow,
)
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskRecord,
    ScheduledTaskRepository,
)
from deerflow.runtime import RunRecord
from deerflow.runtime.private_scope import PrivateResourceScope

logger = logging.getLogger(__name__)

_ACTIVE_RUN_STATUSES = frozenset({"pending", "running"})
_TERMINAL_RUN_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})
_RESTART_ERROR_CODE = "AUTOMATION_GATEWAY_RESTARTED"
_RESTART_RUN_ERROR = "Gateway restarted before automation run completion"
_RESTART_ERROR_MESSAGE = "The automation run was interrupted by a Gateway restart."
_MISSING_RUN_ERROR_CODE = "AUTOMATION_RUN_MISSING"
_MISSING_RUN_ERROR_MESSAGE = "The admitted automation run could not be found."
_RUN_FAILED_ERROR_MESSAGE = "The automation run failed."
_RUN_TIMEOUT_ERROR_MESSAGE = "The automation run timed out."
_RUN_INTERRUPTED_ERROR_MESSAGE = "The automation run was interrupted."


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    requeued: int = 0
    succeeded: int = 0
    failed: int = 0
    interrupted: int = 0
    unchanged: int = 0


@dataclass(frozen=True, slots=True)
class _RunCoordinates:
    run_id: str
    project_id: uuid.UUID
    owner_user_id: str
    thread_id: str
    metadata: dict[str, object]

    @property
    def scope(self) -> PrivateResourceScope:
        return PrivateResourceScope(
            project_id=str(self.project_id),
            owner_user_id=self.owner_user_id,
            membership_version=1,
        )


@dataclass(frozen=True, slots=True)
class _RestartCoordinates:
    occurrence_id: str
    project_id: uuid.UUID
    owner_user_id: str
    task_id: str

    @property
    def scope(self) -> PrivateResourceScope:
        return PrivateResourceScope(
            project_id=str(self.project_id),
            owner_user_id=self.owner_user_id,
            membership_version=1,
        )


@dataclass(frozen=True, slots=True)
class _Outcome:
    occurrence_status: str
    error_code: str | None
    error_message: str | None


class AutomationReconciler:
    """Settle scheduled occurrences from durable M4 Run state.

    Callback metadata and callback scope are deliberately ignored. The Run
    primary key is only a locator; private scope and automation coordinates
    are re-read from PostgreSQL before the documented lock order is acquired.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def handle_run_completion(self, record: RunRecord) -> None:
        if not isinstance(record.run_id, str) or not record.run_id:
            return
        try:
            coordinates = await self._lookup_run_coordinates(record.run_id)
            if coordinates is None:
                return
            async with self._session_factory() as session, session.begin():
                if await self._lock_project_membership(session, coordinates) is None:
                    return
                occurrences = ScheduledTaskRunRepository(session)
                # A committed occurrence FK is the preferred locator. Persisted
                # Run metadata is only the fast-completion fallback for the
                # admission-to-backfill window, and is validated below.
                linked_occurrence = await occurrences.get_by_agent_run_id(
                    coordinates.scope,
                    record.run_id,
                )
                if linked_occurrence is not None:
                    task_id = linked_occurrence.task_id
                    occurrence_id = linked_occurrence.id
                else:
                    task_id, occurrence_id = self._automation_locator(coordinates.metadata)
                    if task_id is None or occurrence_id is None:
                        return
                tasks = ScheduledTaskRepository(session)
                task = await tasks.lock_for_automation_outcome(coordinates.scope, task_id)
                if task is None:
                    return
                occurrence = await occurrences.get(coordinates.scope, occurrence_id, lock=True)
                if occurrence is None or occurrence.task_id != task.id:
                    return
                run = await PrivateRunRepository(session).get(
                    scope=coordinates.scope,
                    run_id=record.run_id,
                    lock=True,
                )
                if run is None or not self._relation_is_valid(task, occurrence, run):
                    return
                outcome = self._outcome_for_run(run)
                if outcome is None:
                    return
                await self._settle(
                    tasks,
                    occurrences,
                    coordinates.scope,
                    task,
                    occurrence,
                    outcome,
                    finished_at=self._validated_now(self._clock()),
                    thread_id=run.thread_id,
                    run_id=run.run_id,
                )
        except (DBAPIError, SATimeoutError):
            raise AutomationUnavailable("automation-completion") from None

    async def reconcile_restart(self, now: datetime) -> ReconciliationReport:
        now = self._validated_now(now)
        try:
            candidates = await self._restart_candidates(now)
            report = ReconciliationReport()
            for candidate in candidates:
                result = await self._reconcile_candidate(candidate, now)
                report = ReconciliationReport(
                    requeued=report.requeued + (result == "requeued"),
                    succeeded=report.succeeded + (result == "succeeded"),
                    failed=report.failed + (result == "failed"),
                    interrupted=report.interrupted + (result == "interrupted"),
                    unchanged=report.unchanged + (result == "unchanged"),
                )
            logger.info(
                "Automation restart reconciliation complete: requeued=%d succeeded=%d failed=%d interrupted=%d unchanged=%d",
                report.requeued,
                report.succeeded,
                report.failed,
                report.interrupted,
                report.unchanged,
            )
            return report
        except (DBAPIError, SATimeoutError):
            raise AutomationUnavailable("automation-restart") from None

    async def _lookup_run_coordinates(self, run_id: str) -> _RunCoordinates | None:
        # Trusted completion-only lookup. It returns coordinates, never content
        # or authority, and every subsequent mutation is project+owner scoped.
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(
                        RunRow.run_id,
                        RunRow.project_id,
                        RunRow.owner_user_id,
                        RunRow.thread_id,
                        RunRow.metadata_json,
                    ).where(RunRow.run_id == run_id)
                )
            ).one_or_none()
        if row is None:
            return None
        return _RunCoordinates(
            run_id=row.run_id,
            project_id=row.project_id,
            owner_user_id=row.owner_user_id,
            thread_id=row.thread_id,
            metadata=dict(row.metadata_json or {}),
        )

    async def _restart_candidates(self, now: datetime) -> tuple[_RestartCoordinates, ...]:
        # Startup-only inventory. Rows carry their database scope into the
        # scoped, lock-ordered transaction below; no mutation is unscoped.
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(
                        ScheduledTaskRunRow.id,
                        ScheduledTaskRunRow.project_id,
                        ScheduledTaskRunRow.owner_user_id,
                        ScheduledTaskRunRow.task_id,
                    )
                    .where(
                        sa.or_(
                            ScheduledTaskRunRow.status == "running",
                            sa.and_(
                                ScheduledTaskRunRow.status == "launching",
                                ScheduledTaskRunRow.lease_expires_at.is_not(None),
                                ScheduledTaskRunRow.lease_expires_at <= now,
                            ),
                        )
                    )
                    .order_by(
                        ScheduledTaskRunRow.project_id,
                        ScheduledTaskRunRow.owner_user_id,
                        ScheduledTaskRunRow.task_id,
                        ScheduledTaskRunRow.id,
                    )
                )
            ).all()
        return tuple(
            _RestartCoordinates(
                occurrence_id=row.id,
                project_id=row.project_id,
                owner_user_id=row.owner_user_id,
                task_id=row.task_id,
            )
            for row in rows
        )

    async def _reconcile_candidate(self, candidate: _RestartCoordinates, now: datetime) -> str:
        async with self._session_factory() as session, session.begin():
            authority = await self._lock_project_membership(session, candidate)
            tasks = ScheduledTaskRepository(session)
            task = await tasks.lock_for_automation_outcome(candidate.scope, candidate.task_id)
            if task is None:
                return "unchanged"
            occurrences = ScheduledTaskRunRepository(session)
            occurrence = await occurrences.get(candidate.scope, candidate.occurrence_id, lock=True)
            if occurrence is None or occurrence.status in TERMINAL_OCCURRENCE_STATUSES:
                return "unchanged"
            if occurrence.status not in {"launching", "running"}:
                return "unchanged"

            run_id = occurrence.run_id or deterministic_run_id(occurrence.id)
            run = await PrivateRunRepository(session).get(scope=candidate.scope, run_id=run_id, lock=True)
            if run is None:
                if occurrence.status == "launching" and occurrence.run_id is None:
                    denial = automation_retry_denial(
                        authority,
                        task,
                        occurrence,
                    )
                    if denial is not None:
                        changed = await occurrences.finish(
                            candidate.scope,
                            occurrence.id,
                            status=denial.occurrence_status,
                            error_code=denial.error_code,
                            error_message=None,
                            finished_at=now,
                        )
                        if not changed:
                            return "unchanged"
                        return "interrupted" if denial.occurrence_status == "cancelled" else "failed"
                    requeued = await occurrences.requeue_launch(
                        candidate.scope,
                        occurrence.id,
                        next_attempt_at=now,
                        error_code=_RESTART_ERROR_CODE,
                        updated_at=now,
                    )
                    return "requeued" if requeued is not None else "unchanged"
                changed = await self._settle(
                    tasks,
                    occurrences,
                    candidate.scope,
                    task,
                    occurrence,
                    _Outcome(
                        "failed",
                        _MISSING_RUN_ERROR_CODE,
                        _MISSING_RUN_ERROR_MESSAGE,
                    ),
                    finished_at=now,
                )
                return "failed" if changed else "unchanged"

            if not self._relation_is_valid(task, occurrence, run):
                return "unchanged"
            outcome = self._outcome_for_run(run)
            if outcome is not None:
                changed = await self._settle(
                    tasks,
                    occurrences,
                    candidate.scope,
                    task,
                    occurrence,
                    outcome,
                    finished_at=now,
                    thread_id=run.thread_id,
                    run_id=run.run_id,
                )
                if not changed:
                    return "unchanged"
                return "succeeded" if outcome.occurrence_status == "success" else ("interrupted" if outcome.occurrence_status == "interrupted" else "failed")

            if run.status not in _ACTIVE_RUN_STATUSES:
                return "unchanged"
            await PrivateRunRepository(session).update_status(
                scope=candidate.scope,
                run_id=run.run_id,
                status="interrupted",
                error=_RESTART_RUN_ERROR,
            )
            changed = await self._settle(
                tasks,
                occurrences,
                candidate.scope,
                task,
                occurrence,
                _Outcome(
                    "interrupted",
                    _RESTART_ERROR_CODE,
                    _RESTART_ERROR_MESSAGE,
                ),
                finished_at=now,
                thread_id=run.thread_id,
                run_id=run.run_id,
            )
            return "interrupted" if changed else "unchanged"

    @staticmethod
    async def _lock_project_membership(
        session: AsyncSession,
        coordinates: _RunCoordinates | _RestartCoordinates,
    ) -> AutomationExecutionAuthority | None:
        return await lock_automation_execution_authority(
            session,
            coordinates.scope,
        )

    @staticmethod
    def _automation_locator(
        metadata: dict[str, object],
    ) -> tuple[str | None, str | None]:
        task_id = metadata.get("scheduled_task_id")
        occurrence_id = metadata.get("scheduled_task_run_id")
        if not isinstance(task_id, str) or not task_id or len(task_id) > 64 or not isinstance(occurrence_id, str) or not occurrence_id or len(occurrence_id) > 64:
            return None, None
        return task_id, occurrence_id

    @staticmethod
    def _relation_is_valid(
        task: ScheduledTaskRecord,
        occurrence: ScheduledTaskRunRecord,
        run: PrivateRunRecord,
    ) -> bool:
        expected_thread_id = deterministic_thread_id(occurrence.id) if task.context_mode == "fresh_thread_per_run" else task.thread_id
        expected_metadata = {
            "scheduled_task_id": task.id,
            "scheduled_task_run_id": occurrence.id,
            "scheduled_trigger": occurrence.trigger,
        }
        return (
            expected_thread_id is not None
            and run.run_id == deterministic_run_id(occurrence.id)
            and run.thread_id == expected_thread_id
            and all(run.metadata.get(key) == value for key, value in expected_metadata.items())
            and (occurrence.thread_id is None or occurrence.thread_id == run.thread_id)
            and (occurrence.run_id is None or occurrence.run_id == run.run_id)
        )

    @staticmethod
    def _outcome_for_run(run: PrivateRunRecord) -> _Outcome | None:
        if run.status not in _TERMINAL_RUN_STATUSES:
            return None
        if run.status == "success":
            return _Outcome("success", None, None)
        if run.status == "error":
            return _Outcome(
                "failed",
                "AUTOMATION_RUN_FAILED",
                _RUN_FAILED_ERROR_MESSAGE,
            )
        if run.status == "timeout":
            return _Outcome(
                "failed",
                "AUTOMATION_RUN_TIMEOUT",
                _RUN_TIMEOUT_ERROR_MESSAGE,
            )
        return _Outcome(
            "interrupted",
            "AUTOMATION_RUN_INTERRUPTED",
            _RUN_INTERRUPTED_ERROR_MESSAGE,
        )

    @staticmethod
    async def _settle(
        tasks: ScheduledTaskRepository,
        occurrences: ScheduledTaskRunRepository,
        scope: PrivateResourceScope,
        task: ScheduledTaskRecord,
        occurrence: ScheduledTaskRunRecord,
        outcome: _Outcome,
        *,
        finished_at: datetime,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> bool:
        changed = await occurrences.finish(
            scope,
            occurrence.id,
            status=outcome.occurrence_status,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
            finished_at=finished_at,
            thread_id=thread_id,
            run_id=run_id,
        )
        if not changed:
            return False
        terminal_status = None
        if task.schedule_type == "once":
            terminal_status = {
                "success": "completed",
                "failed": "failed",
                "interrupted": "cancelled",
                "cancelled": "cancelled",
            }.get(outcome.occurrence_status)
        updated = await tasks.record_automation_outcome(
            scope,
            task.id,
            outcome=outcome.occurrence_status,
            error_code=outcome.error_code,
            occurred_at=finished_at,
            terminal_status=terminal_status,
        )
        if updated is None:
            raise AutomationUnavailable("automation-reconciliation")
        return True

    @staticmethod
    def _validated_now(now: datetime) -> datetime:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return now.astimezone(UTC)


__all__ = ["AutomationReconciler", "ReconciliationReport"]
