from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.automations.dispatcher import AutomationDefinitionRef
from app.automations.errors import AutomationUnavailable
from app.gateway import deps as gateway_deps_module
from app.scheduler.app import SchedulerApp
from app.scheduler.service import AutomationSchedulerService

NOW = datetime(2026, 7, 16, 13, 30, tzinfo=UTC)


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self):
        return self

    def begin_nested(self):
        return self


def _session_factory():
    return _Session()


@pytest.mark.anyio
async def test_scheduler_poll_only_admits_jobs_in_caller_transaction() -> None:
    definition = AutomationDefinitionRef(
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        task_id="task-atomic",
        membership_version=3,
    )
    occurrences = SimpleNamespace(
        due_definitions_in_session=AsyncMock(
            side_effect=[((definition, NOW),), ()],
        ),
    )
    dispatcher = SimpleNamespace(admit_occurrence_in_session=AsyncMock())
    service = AutomationSchedulerService(
        occurrences=occurrences,
        dispatcher=dispatcher,
        reconciler=SimpleNamespace(reconcile_admitted_runs=AsyncMock()),
        max_concurrent_runs=3,
    )
    session = _Session()

    await service.admit_due_occurrences(session, now=NOW)

    first_poll = occurrences.due_definitions_in_session.await_args_list[0]
    assert first_poll.args == (session,)
    assert first_poll.kwargs == {"now": NOW, "limit": 3, "after": None}
    dispatcher.admit_occurrence_in_session.assert_awaited_once_with(
        session,
        definition,
        scheduled_for=NOW,
    )


@pytest.mark.anyio
async def test_scheduler_app_owns_process_lifetime_lock_and_transaction() -> None:
    entered = asyncio.Event()
    released = asyncio.Event()
    stop = asyncio.Event()

    class Ownership:
        is_lost = False

        def hold(self):
            owner = self

            class Hold:
                async def __aenter__(self):
                    entered.set()
                    return owner

                async def __aexit__(self, *_args):
                    released.set()

            return Hold()

    async def admit(_session, *, now):
        assert now.tzinfo is not None
        stop.set()

    service = SimpleNamespace(
        reconcile_admitted_runs=AsyncMock(return_value=0),
        admit_due_occurrences=AsyncMock(side_effect=admit),
    )
    await SchedulerApp(
        enabled=True,
        ownership=Ownership(),
        service=service,
        session_factory=_session_factory,
        poll_interval_seconds=60,
    ).run(stop)

    assert entered.is_set()
    assert released.is_set()
    service.reconcile_admitted_runs.assert_awaited_once()
    service.admit_due_occurrences.assert_awaited_once()


@pytest.mark.anyio
async def test_disabled_scheduler_process_takes_no_lock_or_transaction() -> None:
    ownership = SimpleNamespace(hold=AsyncMock())
    service = SimpleNamespace(
        reconcile_admitted_runs=AsyncMock(),
        admit_due_occurrences=AsyncMock(),
    )
    app = SchedulerApp(
        enabled=False,
        ownership=ownership,
        service=service,
        session_factory=_session_factory,
        poll_interval_seconds=60,
    )

    await app.run(asyncio.Event())

    ownership.hold.assert_not_called()
    service.reconcile_admitted_runs.assert_not_awaited()
    service.admit_due_occurrences.assert_not_awaited()


@pytest.mark.anyio
async def test_scheduler_process_releases_lock_when_ownership_is_lost() -> None:
    released = asyncio.Event()

    class Ownership:
        is_lost = True

        def hold(self):
            class Hold:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *_args):
                    released.set()

            return Hold()

    service = SimpleNamespace(
        reconcile_admitted_runs=AsyncMock(return_value=0),
        admit_due_occurrences=AsyncMock(
            side_effect=AutomationUnavailable("ownership-lost"),
        ),
    )

    await SchedulerApp(
        enabled=True,
        ownership=Ownership(),
        service=service,
        session_factory=_session_factory,
        poll_interval_seconds=60,
    ).run(asyncio.Event())

    assert released.is_set()


def test_gateway_lifespan_does_not_construct_or_start_scheduler() -> None:
    dependency_source = inspect.getsource(gateway_deps_module)

    assert "AutomationSchedulerService(" not in dependency_source
    assert "AutomationSchedulerOwnership(" not in dependency_source


def test_scheduler_process_does_not_import_gateway_or_worker_executor() -> None:
    from app.scheduler import app as scheduler_app_module
    from app.scheduler import service as scheduler_service_module

    source = inspect.getsource(scheduler_app_module) + inspect.getsource(
        scheduler_service_module,
    )
    assert "app.gateway" not in source
    assert "app.worker" not in source
    assert "PrivateRunJobHandler" not in source
