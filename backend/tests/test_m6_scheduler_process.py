from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.automations.dispatcher import AutomationDefinitionRef
from app.gateway import app as gateway_app_module
from app.gateway import deps as gateway_deps_module
from app.scheduler.app import SchedulerApp
from app.scheduler.service import ScheduledTaskService

NOW = datetime(2026, 7, 16, 13, 30, tzinfo=UTC)


@pytest.mark.anyio
async def test_scheduler_poll_only_admits_jobs() -> None:
    definition = AutomationDefinitionRef(
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        task_id="task-atomic",
        membership_version=3,
    )
    occurrences = SimpleNamespace(
        due_definitions=AsyncMock(return_value=((definition, NOW),)),
    )
    dispatcher = SimpleNamespace(admit_occurrence=AsyncMock())
    service = ScheduledTaskService(
        occurrences=occurrences,
        dispatcher=dispatcher,
        reconciler=SimpleNamespace(reconcile_restart=AsyncMock()),
        poll_interval_seconds=60,
        max_concurrent_runs=3,
    )

    await service.run_once(now=NOW)

    assert occurrences.due_definitions.await_args_list[0].kwargs == {
        "now": NOW,
        "limit": 3,
        "after": None,
    }
    assert occurrences.due_definitions.await_count == 1
    dispatcher.admit_occurrence.assert_awaited_once_with(
        definition,
        scheduled_for=NOW,
    )


@pytest.mark.anyio
async def test_scheduler_app_owns_process_lifetime_lock_and_stops_service() -> None:
    entered = asyncio.Event()
    released = asyncio.Event()

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

    service = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        task=None,
    )
    stop = asyncio.Event()
    app = SchedulerApp(
        enabled=True,
        ownership=Ownership(),
        service=service,
    )

    task = asyncio.create_task(app.run(stop))
    await entered.wait()
    service.start.assert_awaited_once_with()
    assert not released.is_set()
    stop.set()
    await task

    service.stop.assert_awaited_once_with()
    assert released.is_set()


@pytest.mark.anyio
async def test_disabled_scheduler_process_takes_no_lock_or_poll_task() -> None:
    ownership = SimpleNamespace(hold=AsyncMock())
    service = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), task=None)
    app = SchedulerApp(
        enabled=False,
        ownership=ownership,
        service=service,
    )

    await app.run(asyncio.Event())

    ownership.hold.assert_not_called()
    service.start.assert_not_awaited()
    service.stop.assert_not_awaited()


@pytest.mark.anyio
async def test_scheduler_process_releases_lock_when_poller_fail_stops() -> None:
    released = asyncio.Event()

    class Ownership:
        def hold(self):
            class Hold:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *_args):
                    released.set()

            return Hold()

    poll_task = asyncio.create_task(asyncio.sleep(0))
    service = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        task=poll_task,
    )

    await SchedulerApp(
        enabled=True,
        ownership=Ownership(),
        service=service,
    ).run(asyncio.Event())

    service.stop.assert_awaited_once_with()
    assert released.is_set()


def test_gateway_lifespan_does_not_construct_or_start_scheduler() -> None:
    gateway_source = inspect.getsource(gateway_app_module)
    dependency_source = inspect.getsource(gateway_deps_module)

    assert "_start_scheduled_task_service" not in gateway_source
    assert "ScheduledTaskService(" not in dependency_source
    assert "AutomationSchedulerOwnership(" not in dependency_source


def test_scheduler_process_does_not_import_gateway_or_worker_executor() -> None:
    from app.scheduler import app as scheduler_app_module
    from app.scheduler import service as scheduler_service_module

    source = inspect.getsource(scheduler_app_module) + inspect.getsource(scheduler_service_module)
    assert "app.gateway" not in source
    assert "app.worker" not in source
    assert "PrivateRunJobHandler" not in source
