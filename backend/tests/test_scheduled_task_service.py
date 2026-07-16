from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.automations.errors import AutomationUnavailable
from app.automations.reconciliation import ReconciliationReport
from app.scheduler.service import ScheduledTaskService

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


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

    async def due_definitions(self, **kwargs):
        self.due_calls.append(kwargs)
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
        self.calls: list[tuple[str, object]] = []

    async def admit_occurrence(self, definition, *, scheduled_for):
        self.calls.append((definition.task_id, scheduled_for))
        failure = self.failures.get(definition.task_id)
        if failure is not None:
            raise failure


def _service(
    *,
    occurrences: FakeOccurrences | None = None,
    dispatcher: FakeDispatcher | None = None,
    reconciler=None,
    poll_interval_seconds: float = 60,
    max_concurrent_runs: int = 3,
    ownership=None,
) -> ScheduledTaskService:
    return ScheduledTaskService(
        occurrences=occurrences or FakeOccurrences(),
        dispatcher=dispatcher or FakeDispatcher(),
        reconciler=reconciler or SimpleNamespace(reconcile_restart=AsyncMock(return_value=ReconciliationReport())),
        poll_interval_seconds=poll_interval_seconds,
        max_concurrent_runs=max_concurrent_runs,
        ownership=ownership,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_run_once_lists_due_definitions_then_admits_each_job() -> None:
    occurrences = FakeOccurrences(("occ-1", "occ-2"))
    dispatcher = FakeDispatcher()
    service = _service(occurrences=occurrences, dispatcher=dispatcher)

    await service.run_once(now=NOW)

    assert occurrences.due_calls == [{"now": NOW, "limit": 3, "after": None}]
    assert dispatcher.calls == [
        ("occ-1", NOW),
        ("occ-2", NOW),
    ]


@pytest.mark.asyncio
async def test_run_once_pages_past_mapped_failure_without_starving_later_due_work() -> None:
    occurrences = FakeOccurrences(("occ-1", "occ-2", "occ-3"))
    dispatcher = FakeDispatcher(failures={"occ-1": AutomationUnavailable("request-id")})
    service = _service(
        occurrences=occurrences,
        dispatcher=dispatcher,
        max_concurrent_runs=2,
    )

    await service.run_once(now=NOW)

    assert [call[0] for call in dispatcher.calls] == [
        "occ-1",
        "occ-2",
        "occ-3",
    ]
    assert len(occurrences.due_calls) == 2
    assert occurrences.due_calls[0] == {
        "now": NOW,
        "limit": 2,
        "after": None,
    }
    assert occurrences.due_calls[1]["after"][3] == "occ-2"


@pytest.mark.asyncio
async def test_run_once_max_one_paginates_past_permanent_first_failure() -> None:
    occurrences = FakeOccurrences(("bad", "good"))
    dispatcher = FakeDispatcher(
        failures={"bad": AutomationUnavailable("request-id")},
    )
    service = _service(
        occurrences=occurrences,
        dispatcher=dispatcher,
        max_concurrent_runs=1,
    )

    await service.run_once(now=NOW)

    assert [call[0] for call in dispatcher.calls] == ["bad", "good"]
    assert len(occurrences.due_calls) == 3


@pytest.mark.asyncio
async def test_run_once_reverifies_ownership_before_each_due_page() -> None:
    occurrences = FakeOccurrences(("first", "must-not-admit"))
    dispatcher = FakeDispatcher()
    ownership = SimpleNamespace(
        verify=AsyncMock(
            side_effect=[
                None,
                AutomationUnavailable("ownership-lost"),
            ],
        ),
        is_lost=True,
    )
    service = _service(
        occurrences=occurrences,
        dispatcher=dispatcher,
        max_concurrent_runs=1,
        ownership=ownership,
    )

    with pytest.raises(AutomationUnavailable):
        await service.run_once(now=NOW)

    assert ownership.verify.await_count == 2
    assert [call[0] for call in dispatcher.calls] == ["first"]
    assert len(occurrences.due_calls) == 1


@pytest.mark.asyncio
async def test_run_once_propagates_unexpected_dispatch_failure() -> None:
    occurrences = FakeOccurrences(("occ-1",))
    dispatcher = FakeDispatcher(failures={"occ-1": RuntimeError("bug")})
    service = _service(occurrences=occurrences, dispatcher=dispatcher)

    with pytest.raises(RuntimeError, match="bug"):
        await service.run_once(now=NOW)


@pytest.mark.asyncio
async def test_start_reconciles_before_polling_and_is_idempotent() -> None:
    order: list[str] = []

    class Reconciler:
        async def reconcile_restart(self, now):
            assert now == NOW
            order.append("reconcile")
            return ReconciliationReport()

    service = _service(reconciler=Reconciler(), poll_interval_seconds=60)

    async def blocked_run_once(*, now):
        assert now == NOW
        order.append("poll")
        await asyncio.Event().wait()

    service.run_once = blocked_run_once
    await service.start()
    await asyncio.sleep(0)
    first_task = service.task
    await service.start()

    assert order[:2] == ["reconcile", "poll"]
    assert service.task is first_task
    await service.stop()


@pytest.mark.asyncio
async def test_reconciliation_failure_prevents_poll_task_start() -> None:
    reconciler = SimpleNamespace(reconcile_restart=AsyncMock(side_effect=AutomationUnavailable("request-id")))
    service = _service(reconciler=reconciler)

    with pytest.raises(AutomationUnavailable):
        await service.start()

    assert service.task is None


@pytest.mark.asyncio
async def test_run_once_verifies_ownership_before_reserving() -> None:
    occurrences = FakeOccurrences(("must-not-claim",))
    dispatcher = FakeDispatcher()
    ownership = SimpleNamespace(
        verify=AsyncMock(side_effect=AutomationUnavailable("ownership-lost")),
        is_lost=True,
    )
    service = _service(
        occurrences=occurrences,
        dispatcher=dispatcher,
        ownership=ownership,
    )

    with pytest.raises(AutomationUnavailable):
        await service.run_once(now=NOW)

    ownership.verify.assert_awaited_once_with()
    assert occurrences.due_calls == []
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_background_loop_fail_stops_after_ownership_loss() -> None:
    occurrences = FakeOccurrences(("must-not-claim",))
    dispatcher = FakeDispatcher()
    ownership = SimpleNamespace(
        verify=AsyncMock(
            side_effect=[
                None,
                AutomationUnavailable("ownership-lost"),
            ]
        ),
        is_lost=True,
    )
    service = _service(
        occurrences=occurrences,
        dispatcher=dispatcher,
        ownership=ownership,
        poll_interval_seconds=0.01,
    )

    await service.start()
    assert service.task is not None
    await asyncio.wait_for(service.task, timeout=1)

    assert service.status == "ownership_lost"
    assert ownership.verify.await_count == 2
    assert occurrences.due_calls == []
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_stop_cancels_blocked_poll_promptly_and_is_idempotent() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    service = _service(poll_interval_seconds=60)

    async def blocked_run_once(*, now):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service.run_once = blocked_run_once
    await service.start()
    await asyncio.wait_for(entered.wait(), timeout=1)

    await asyncio.wait_for(service.stop(), timeout=1)
    await service.stop()

    assert cancelled.is_set()
    assert service.task is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("poll_interval_seconds", 0),
        ("max_concurrent_runs", 0),
    ],
)
def test_service_rejects_non_positive_scheduler_settings(field: str, value: int) -> None:
    kwargs = {
        "occurrences": FakeOccurrences(),
        "dispatcher": FakeDispatcher(),
        "reconciler": SimpleNamespace(reconcile_restart=AsyncMock()),
        "poll_interval_seconds": 1,
        "max_concurrent_runs": 1,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=field):
        ScheduledTaskService(**kwargs)
