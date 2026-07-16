from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from app.automations.errors import AutomationCutover
from app.gateway.app import (
    _start_scheduled_task_service,
    _stop_scheduled_task_service,
)
from app.scheduler.service import ScheduledTaskService


def _scheduler_app() -> tuple[FastAPI, ScheduledTaskService]:
    app = FastAPI()
    ownership = SimpleNamespace(
        is_acquired=True,
        is_lost=False,
        verify=AsyncMock(),
    )
    service = ScheduledTaskService(
        app=app,
        occurrences=SimpleNamespace(
            reserve_due=AsyncMock(),
            claim_next=AsyncMock(return_value=None),
        ),
        dispatcher=SimpleNamespace(dispatch=AsyncMock()),
        reconciler=SimpleNamespace(reconcile_restart=AsyncMock()),
        poll_interval_seconds=60,
        lease_seconds=120,
        max_concurrent_runs=3,
        ownership=ownership,
    )
    app.state.automation_dispatcher = service._dispatcher
    app.state.automation_scheduler = service
    app.state.scheduled_task_service = service
    app.state.automation_cutover_guard = SimpleNamespace(
        require_project_open=AsyncMock(),
    )
    app.state.automation_scheduler_ownership = ownership
    app.state.automation_scheduler_task = None
    return app, service


@asynccontextmanager
async def _scheduler_lifespan(app: FastAPI):
    await _start_scheduled_task_service(app, enabled=True)
    try:
        yield
    finally:
        await _stop_scheduled_task_service(app)


@pytest.mark.asyncio
async def test_disabled_scheduler_keeps_manual_dispatcher() -> None:
    app, service = _scheduler_app()

    started = await _start_scheduled_task_service(app, enabled=False)

    assert started is False
    assert app.state.automation_dispatcher is not None
    assert app.state.automation_scheduler is service
    assert app.state.automation_scheduler_task is None
    assert service.task is None


@pytest.mark.asyncio
async def test_each_app_starts_at_most_one_poll_task_and_shutdown_awaits_it() -> None:
    for _ in range(2):
        app, service = _scheduler_app()

        async with _scheduler_lifespan(app):
            first_task = app.state.automation_scheduler_task
            assert first_task is service.task
            assert isinstance(first_task, asyncio.Task)

            assert await _start_scheduled_task_service(app, enabled=True) is True
            assert app.state.automation_scheduler_task is first_task
            assert service.task is first_task

        assert app.state.automation_scheduler_task is None
        assert service.task is None
        assert first_task.done()


@pytest.mark.asyncio
async def test_migration_required_keeps_gateway_manual_dependencies_open() -> None:
    app, service = _scheduler_app()
    app.state.automation_cutover_guard.require_project_open = AsyncMock(side_effect=AutomationCutover("task-14-migration-required"))

    assert await _start_scheduled_task_service(app, enabled=True) is False

    assert app.state.automation_dispatcher is not None
    assert app.state.automation_scheduler_task is None
    assert service.task is None


@pytest.mark.asyncio
async def test_multi_worker_gate_keeps_manual_dispatcher_without_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_WORKERS", "2")
    app, service = _scheduler_app()

    assert await _start_scheduled_task_service(app, enabled=True) is False

    assert app.state.automation_dispatcher is not None
    assert app.state.automation_scheduler_task is None
    assert service.task is None
