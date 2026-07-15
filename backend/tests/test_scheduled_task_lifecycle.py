from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.automations.errors import AutomationCutover
from app.gateway import deps
from app.gateway.app import (
    _start_scheduled_task_service,
    _stop_scheduled_task_service,
    create_app,
)


def test_gateway_app_includes_scheduled_task_router():
    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/api/scheduled-tasks" in paths


def test_run_context_completion_hook_uses_authoritative_reconciler(monkeypatch) -> None:
    reconciler = SimpleNamespace(handle_run_completion=AsyncMock())
    app = SimpleNamespace(
        state=SimpleNamespace(
            automation_reconciler=reconciler,
            scheduled_task_service=SimpleNamespace(handle_run_completion=AsyncMock()),
            run_events_config=None,
        )
    )
    request = SimpleNamespace(app=app)
    monkeypatch.setattr(deps, "get_checkpointer", lambda _request: object())
    monkeypatch.setattr(deps, "get_store", lambda _request: object())
    monkeypatch.setattr(deps, "get_run_event_store", lambda _request: object())
    monkeypatch.setattr(deps, "get_thread_store", lambda _request: object())
    monkeypatch.setattr(deps, "get_config", lambda: object())

    context = deps.get_run_context(request)

    assert context.on_run_completed == reconciler.handle_run_completion


@pytest.mark.asyncio
async def test_disabled_scheduler_remains_initialized_but_does_not_start() -> None:
    service = SimpleNamespace(start=AsyncMock())
    guard = SimpleNamespace(require_project_open=AsyncMock())
    app = SimpleNamespace(
        state=SimpleNamespace(
            scheduled_task_service=service,
            automation_cutover_guard=guard,
        )
    )

    started = await _start_scheduled_task_service(app, enabled=False)

    assert started is False
    service.start.assert_not_awaited()
    guard.require_project_open.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_start_fails_closed_when_cutover_is_incomplete() -> None:
    service = SimpleNamespace(start=AsyncMock())
    guard = SimpleNamespace(require_project_open=AsyncMock(side_effect=AutomationCutover("scheduler-start")))
    app = SimpleNamespace(
        state=SimpleNamespace(
            scheduled_task_service=service,
            automation_cutover_guard=guard,
            automation_scheduler_ownership=SimpleNamespace(is_acquired=True),
        )
    )

    started = await _start_scheduled_task_service(app, enabled=True)

    assert started is False
    guard.require_project_open.assert_awaited_once_with()
    service.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_start_checks_cutover_before_reconciliation_and_poll() -> None:
    order: list[str] = []

    async def require_project_open():
        order.append("guard")

    async def start():
        order.append("service")

    app = SimpleNamespace(
        state=SimpleNamespace(
            scheduled_task_service=SimpleNamespace(start=start),
            automation_cutover_guard=SimpleNamespace(require_project_open=require_project_open),
            automation_scheduler_ownership=SimpleNamespace(is_acquired=True),
        )
    )

    assert await _start_scheduled_task_service(app, enabled=True) is True
    assert order == ["guard", "service"]


@pytest.mark.asyncio
async def test_scheduler_does_not_start_in_unsupported_multi_worker_mode(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GATEWAY_WORKERS", "2")
    service = SimpleNamespace(start=AsyncMock())
    guard = SimpleNamespace(require_project_open=AsyncMock())
    app = SimpleNamespace(
        state=SimpleNamespace(
            scheduled_task_service=service,
            automation_cutover_guard=guard,
            automation_scheduler_ownership=SimpleNamespace(is_acquired=True),
        )
    )

    assert await _start_scheduled_task_service(app, enabled=True) is False
    guard.require_project_open.assert_not_awaited()
    service.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_start_requires_database_lifetime_ownership() -> None:
    service = SimpleNamespace(start=AsyncMock())
    guard = SimpleNamespace(require_project_open=AsyncMock())
    app = SimpleNamespace(
        state=SimpleNamespace(
            scheduled_task_service=service,
            automation_cutover_guard=guard,
            automation_scheduler_ownership=SimpleNamespace(is_acquired=False),
        )
    )

    assert await _start_scheduled_task_service(app, enabled=True) is False
    guard.require_project_open.assert_not_awaited()
    service.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_shutdown_is_explicit_and_idempotent() -> None:
    service = SimpleNamespace(stop=AsyncMock())
    app = SimpleNamespace(state=SimpleNamespace(scheduled_task_service=service))

    await _stop_scheduled_task_service(app)

    service.stop.assert_awaited_once_with()
