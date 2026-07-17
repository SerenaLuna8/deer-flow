from __future__ import annotations

import inspect
from types import SimpleNamespace

from fastapi import FastAPI

from app.automations.dispatcher import AutomationDispatcher
from app.gateway import app as gateway_app_module
from app.gateway import deps as gateway_deps_module
from app.gateway.deps import (
    get_automation_dispatcher,
    get_automation_scheduler_enabled,
)


def test_gateway_exposes_manual_automation_admission_without_scheduler() -> None:
    app = FastAPI()
    dispatcher = object()
    app.state.automation_dispatcher = dispatcher
    app.state.operational_audit_sink = object()
    app.state.automation_scheduler_enabled = True
    request = SimpleNamespace(app=app)

    assert get_automation_dispatcher(request) is dispatcher
    assert get_automation_scheduler_enabled(request) is True


def test_gateway_runtime_does_not_construct_scheduler_or_ownership() -> None:
    app_source = inspect.getsource(gateway_app_module)
    deps_source = inspect.getsource(gateway_deps_module)

    assert "_start_scheduled_task_service" not in app_source
    assert "_stop_scheduled_task_service" not in app_source
    assert "ScheduledTaskService(" not in deps_source
    assert "AutomationSchedulerOwnership(" not in deps_source
    assert "reconcile_restart(" not in deps_source


def test_gateway_dispatcher_defaults_to_job_admission_only() -> None:
    signature = inspect.signature(AutomationDispatcher)

    assert signature.parameters["thread_service"].default is None
    assert signature.parameters["launch_private_run"].default is None
