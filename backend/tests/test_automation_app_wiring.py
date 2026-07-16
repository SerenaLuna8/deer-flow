from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from app.automations.errors import AutomationCutover
from app.channels import service as channel_service_module
from app.gateway import app as gateway_app_module
from app.gateway.app import (
    _start_scheduled_task_service,
    create_app,
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


def _production_lifespan_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stop_error: BaseException | None = None,
) -> tuple[FastAPI, list[ScheduledTaskService], list[str]]:
    app = create_app()
    services: list[ScheduledTaskService] = []
    order: list[str] = []
    startup_config = SimpleNamespace(
        log_level="INFO",
        memory=SimpleNamespace(token_counting="char"),
        scheduler=SimpleNamespace(enabled=True),
    )

    @asynccontextmanager
    async def runtime_lifespan(runtime_app: FastAPI, _startup_config):
        _, service = _scheduler_app()
        service.app = runtime_app
        runtime_app.state.automation_dispatcher = service._dispatcher
        runtime_app.state.automation_scheduler = service
        runtime_app.state.scheduled_task_service = service
        runtime_app.state.automation_cutover_guard = SimpleNamespace(
            require_project_open=AsyncMock(),
        )
        runtime_app.state.automation_scheduler_ownership = service._ownership
        runtime_app.state.automation_scheduler_task = None
        original_stop = service.stop

        async def recording_stop() -> None:
            order.append("scheduler-stop")
            if stop_error is not None:
                raise stop_error
            await original_stop()

        service.stop = AsyncMock(side_effect=recording_stop)  # type: ignore[method-assign]
        services.append(service)
        try:
            yield
        finally:
            order.append("ownership-exit")
            order.append("runtime-exit")

    monkeypatch.setenv("GATEWAY_WORKERS", "1")
    monkeypatch.setattr(gateway_app_module, "get_app_config", lambda: startup_config)
    monkeypatch.setattr(
        gateway_app_module,
        "get_gateway_config",
        lambda: SimpleNamespace(host="test", port=0, enable_docs=False),
    )
    monkeypatch.setattr(gateway_app_module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(gateway_app_module, "warn_if_auth_disabled_enabled", lambda: None)
    monkeypatch.setattr(gateway_app_module, "cleanup_stale_upload_staging_files", lambda: 0)
    monkeypatch.setattr(gateway_app_module, "_gateway_runtime_lifespan", runtime_lifespan)
    monkeypatch.setattr(gateway_app_module, "_ensure_admin_user", AsyncMock())
    monkeypatch.setattr(gateway_app_module.auth, "close_oidc_service", AsyncMock())
    monkeypatch.setattr(
        channel_service_module,
        "start_channel_service",
        AsyncMock(return_value=SimpleNamespace(get_status=lambda: {})),
    )
    monkeypatch.setattr(channel_service_module, "stop_channel_service", AsyncMock())
    return app, services, order


async def _stop_leaked_service(service: ScheduledTaskService) -> None:
    task = service.task
    if task is not None and not task.done():
        await ScheduledTaskService.stop(service)


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
async def test_each_app_starts_at_most_one_poll_task_and_shutdown_awaits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for _ in range(2):
        app, services, order = _production_lifespan_app(monkeypatch)

        async with app.router.lifespan_context(app):
            service = services[0]
            first_task = app.state.automation_scheduler_task
            assert first_task is service.task
            assert isinstance(first_task, asyncio.Task)

            assert await _start_scheduled_task_service(app, enabled=True) is True
            assert app.state.automation_scheduler_task is first_task
            assert service.task is first_task

        assert app.state.automation_scheduler_task is None
        assert app.state.automation_scheduler is None
        assert app.state.scheduled_task_service is None
        assert service.task is None
        assert first_task.done()
        assert order == ["scheduler-stop", "ownership-exit", "runtime-exit"]


@pytest.mark.asyncio
async def test_production_lifespan_stops_scheduler_before_runtime_on_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, services, order = _production_lifespan_app(
        monkeypatch,
        stop_error=RuntimeError("scheduler stop failed"),
    )

    try:
        with pytest.raises(RuntimeError, match="lifespan body failed"):
            async with app.router.lifespan_context(app):
                service = services[0]
                scheduler_task = app.state.automation_scheduler_task
                raise RuntimeError("lifespan body failed")

        service.stop.assert_awaited_once_with()  # type: ignore[attr-defined]
        assert scheduler_task.done()
        assert order == ["scheduler-stop", "ownership-exit", "runtime-exit"]
        assert app.state.automation_scheduler_task is None
        assert app.state.automation_scheduler is None
        assert app.state.scheduled_task_service is None
    finally:
        if services:
            await _stop_leaked_service(services[0])


@pytest.mark.asyncio
async def test_production_lifespan_preserves_body_error_when_scheduler_cleanup_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, services, order = _production_lifespan_app(
        monkeypatch,
        stop_error=asyncio.CancelledError("scheduler stop cancelled"),
    )

    try:
        with pytest.raises(BaseExceptionGroup) as raised:
            async with app.router.lifespan_context(app):
                service = services[0]
                scheduler_task = app.state.automation_scheduler_task
                raise RuntimeError("lifespan body failed")

        assert {type(error) for error in raised.value.exceptions} == {
            RuntimeError,
            asyncio.CancelledError,
        }
        assert str(raised.value.exceptions[0]) == "lifespan body failed"
        service.stop.assert_awaited_once_with()  # type: ignore[attr-defined]
        assert scheduler_task.done()
        assert order == ["scheduler-stop", "ownership-exit", "runtime-exit"]
        assert app.state.automation_scheduler_task is None
        assert app.state.automation_scheduler is None
        assert app.state.scheduled_task_service is None
    finally:
        if services:
            await _stop_leaked_service(services[0])


@pytest.mark.asyncio
async def test_production_lifespan_stops_scheduler_before_runtime_on_body_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, services, order = _production_lifespan_app(monkeypatch)
    entered = asyncio.Event()

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            entered.set()
            await asyncio.Future()

    lifespan_task = asyncio.create_task(run_lifespan())
    await entered.wait()
    service = services[0]
    scheduler_task = app.state.automation_scheduler_task

    try:
        lifespan_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await lifespan_task

        service.stop.assert_awaited_once_with()  # type: ignore[attr-defined]
        assert scheduler_task.done()
        assert order == ["scheduler-stop", "ownership-exit", "runtime-exit"]
        assert app.state.automation_scheduler_task is None
        assert app.state.automation_scheduler is None
        assert app.state.scheduled_task_service is None
    finally:
        await _stop_leaked_service(service)


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
