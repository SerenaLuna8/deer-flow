from __future__ import annotations

import asyncio
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
        self._occurrence_ids = list(occurrence_ids)
        self.reserve_due = AsyncMock(return_value=())
        self.claim_calls: list[dict[str, object]] = []

    async def claim_next(self, **kwargs):
        self.claim_calls.append(kwargs)
        if not self._occurrence_ids:
            return None
        return SimpleNamespace(id=self._occurrence_ids.pop(0))


class FakeDispatcher:
    def __init__(self, *, failures: dict[str, Exception] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[tuple[str, object]] = []

    async def dispatch(self, occurrence_id: str, *, app):
        self.calls.append((occurrence_id, app))
        failure = self.failures.get(occurrence_id)
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
        app=SimpleNamespace(state=SimpleNamespace()),
        occurrences=occurrences or FakeOccurrences(),
        dispatcher=dispatcher or FakeDispatcher(),
        reconciler=reconciler or SimpleNamespace(reconcile_restart=AsyncMock(return_value=ReconciliationReport())),
        poll_interval_seconds=poll_interval_seconds,
        lease_seconds=120,
        max_concurrent_runs=max_concurrent_runs,
        ownership=ownership,
        clock=lambda: NOW,
        lease_owner="scheduler-test",
    )


@pytest.mark.asyncio
async def test_run_once_reserves_then_claims_and_dispatches_each_occurrence() -> None:
    occurrences = FakeOccurrences(("occ-1", "occ-2"))
    dispatcher = FakeDispatcher()
    service = _service(occurrences=occurrences, dispatcher=dispatcher)

    await service.run_once(now=NOW)

    occurrences.reserve_due.assert_awaited_once_with(now=NOW, limit=3)
    assert len(occurrences.claim_calls) == 3
    assert all(call["now"] == NOW for call in occurrences.claim_calls)
    assert all(call["lease_owner"] == "scheduler-test" for call in occurrences.claim_calls)
    assert dispatcher.calls == [
        ("occ-1", service.app),
        ("occ-2", service.app),
    ]


@pytest.mark.asyncio
async def test_run_once_is_bounded_and_continues_after_mapped_dispatch_failure() -> None:
    occurrences = FakeOccurrences(("occ-1", "occ-2", "occ-3"))
    dispatcher = FakeDispatcher(failures={"occ-1": AutomationUnavailable("request-id")})
    service = _service(
        occurrences=occurrences,
        dispatcher=dispatcher,
        max_concurrent_runs=2,
    )

    await service.run_once(now=NOW)

    assert [call[0] for call in dispatcher.calls] == ["occ-1", "occ-2"]
    assert len(occurrences.claim_calls) == 2


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
    occurrences.reserve_due.assert_not_awaited()
    assert occurrences.claim_calls == []
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
    occurrences.reserve_due.assert_not_awaited()
    assert occurrences.claim_calls == []
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
        ("lease_seconds", 0),
        ("max_concurrent_runs", 0),
    ],
)
def test_service_rejects_non_positive_scheduler_settings(field: str, value: int) -> None:
    kwargs = {
        "app": SimpleNamespace(state=SimpleNamespace()),
        "occurrences": FakeOccurrences(),
        "dispatcher": FakeDispatcher(),
        "reconciler": SimpleNamespace(reconcile_restart=AsyncMock()),
        "poll_interval_seconds": 1,
        "lease_seconds": 1,
        "max_concurrent_runs": 1,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=field):
        ScheduledTaskService(**kwargs)
