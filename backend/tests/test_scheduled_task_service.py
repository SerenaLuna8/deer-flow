from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.automations.errors import AutomationConcurrencyLimit, AutomationUnavailable
from app.automations.reconciliation import ReconciliationReport
from app.scheduler import service as scheduler_service
from app.scheduler.service import AutomationSchedulerService

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def test_scheduler_service_exposes_only_final_automation_operations() -> None:
    service_type = getattr(scheduler_service, "AutomationSchedulerService", None)

    assert service_type is not None
    assert {name for name, value in vars(service_type).items() if not name.startswith("_") and callable(value)} == {"reconcile_admitted_runs", "admit_due_occurrences"}


class FakeOccurrences:
    def __init__(self, occurrence_ids: tuple[str, ...] = ()) -> None:
        self._definitions = [
            SimpleNamespace(
                task_id=value,
                project_id=uuid.UUID(int=index + 1),
                owner_user_id=str(uuid.UUID(int=index + 100)),
            )
            for index, value in enumerate(occurrence_ids)
        ]
        self.due_calls: list[dict[str, object]] = []

    async def due_definitions_in_session(self, session, **kwargs):
        self.due_calls.append({"session": session, **kwargs})
        limit = kwargs["limit"]
        after = kwargs["after"]
        start = 0
        if after is not None:
            start = next(index + 1 for index, definition in enumerate(self._definitions) if definition.task_id == after[3])
        selected = self._definitions[start : start + limit]
        return tuple((definition, NOW) for definition in selected)


class FakeDispatcher:
    def __init__(self, *, failures: dict[str, Exception] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[tuple[object, str, datetime]] = []

    async def admit_occurrence_in_session(
        self,
        session,
        definition,
        *,
        scheduled_for,
    ):
        self.calls.append((session, definition.task_id, scheduled_for))
        failure = self.failures.get(definition.task_id)
        if failure is not None:
            raise failure
        return SimpleNamespace(occurrence=SimpleNamespace(id=definition.task_id))


def _service(
    *,
    occurrences: FakeOccurrences | None = None,
    dispatcher: FakeDispatcher | None = None,
    reconciler=None,
    max_concurrent_runs: int = 3,
    ownership=None,
) -> AutomationSchedulerService:
    return AutomationSchedulerService(
        occurrences=occurrences or FakeOccurrences(),
        dispatcher=dispatcher or FakeDispatcher(),
        reconciler=reconciler
        or SimpleNamespace(
            reconcile_admitted_runs=AsyncMock(
                return_value=ReconciliationReport(),
            )
        ),
        max_concurrent_runs=max_concurrent_runs,
        ownership=ownership,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_admit_due_occurrences_uses_the_caller_session_for_reads_and_writes() -> None:
    session = object()
    occurrences = FakeOccurrences(("occ-1", "occ-2"))
    dispatcher = FakeDispatcher()
    service = _service(occurrences=occurrences, dispatcher=dispatcher)

    admitted = await service.admit_due_occurrences(session, now=NOW)

    assert [item.occurrence.id for item in admitted] == ["occ-1", "occ-2"]
    assert occurrences.due_calls == [{"session": session, "now": NOW, "limit": 3, "after": None}]
    assert dispatcher.calls == [
        (session, "occ-1", NOW),
        (session, "occ-2", NOW),
    ]


@pytest.mark.asyncio
async def test_admit_due_occurrences_pages_past_mapped_failure() -> None:
    session = object()
    occurrences = FakeOccurrences(("bad", "occ-2", "occ-3"))
    dispatcher = FakeDispatcher(
        failures={"bad": AutomationUnavailable("request-id")},
    )
    service = _service(
        occurrences=occurrences,
        dispatcher=dispatcher,
        max_concurrent_runs=2,
    )

    admitted = await service.admit_due_occurrences(session, now=NOW)

    assert [item.occurrence.id for item in admitted] == ["occ-2", "occ-3"]
    assert [call[1] for call in dispatcher.calls] == ["bad", "occ-2", "occ-3"]
    assert len(occurrences.due_calls) == 2
    assert occurrences.due_calls[1]["after"][3] == "occ-2"


@pytest.mark.asyncio
async def test_admit_due_occurrences_stops_at_concurrency_limit() -> None:
    session = object()
    occurrences = FakeOccurrences(("first", "later"))
    dispatcher = FakeDispatcher(
        failures={"first": AutomationConcurrencyLimit("request-id")},
    )

    admitted = await _service(
        occurrences=occurrences,
        dispatcher=dispatcher,
    ).admit_due_occurrences(session, now=NOW)

    assert admitted == ()
    assert [call[1] for call in dispatcher.calls] == ["first"]


@pytest.mark.asyncio
async def test_admit_due_occurrences_reverifies_ownership_before_each_page() -> None:
    occurrences = FakeOccurrences(("first", "must-not-admit"))
    dispatcher = FakeDispatcher()
    ownership = SimpleNamespace(
        verify=AsyncMock(
            side_effect=[None, AutomationUnavailable("ownership-lost")],
        ),
    )

    with pytest.raises(AutomationUnavailable):
        await _service(
            occurrences=occurrences,
            dispatcher=dispatcher,
            max_concurrent_runs=1,
            ownership=ownership,
        ).admit_due_occurrences(object(), now=NOW)

    assert ownership.verify.await_count == 2
    assert [call[1] for call in dispatcher.calls] == ["first"]


@pytest.mark.asyncio
async def test_reconcile_admitted_runs_returns_settled_count_in_caller_session() -> None:
    session = object()
    reconciler = SimpleNamespace(
        reconcile_admitted_runs=AsyncMock(
            return_value=ReconciliationReport(
                succeeded=2,
                failed=1,
                interrupted=3,
                unchanged=4,
            )
        )
    )

    settled = await _service(reconciler=reconciler).reconcile_admitted_runs(session)

    assert settled == 6
    reconciler.reconcile_admitted_runs.assert_awaited_once_with(session, now=NOW)


def test_service_rejects_non_positive_concurrency() -> None:
    with pytest.raises(ValueError, match="max_concurrent_runs"):
        _service(max_concurrent_runs=0)
