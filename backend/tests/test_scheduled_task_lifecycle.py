from types import SimpleNamespace

from app.gateway import app as gateway_app
from app.gateway import deps
from app.gateway.app import create_app


def test_gateway_app_includes_scheduled_task_router():
    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/api/scheduled-tasks" in paths


def test_gateway_run_context_has_no_automation_completion_callback(
    monkeypatch,
) -> None:
    app = SimpleNamespace(state=SimpleNamespace(run_events_config=None))
    request = SimpleNamespace(app=app)
    monkeypatch.setattr(deps, "get_checkpointer", lambda _request: object())
    monkeypatch.setattr(deps, "get_store", lambda _request: object())
    monkeypatch.setattr(deps, "get_run_event_store", lambda _request: object())
    monkeypatch.setattr(deps, "get_thread_store", lambda _request: object())
    monkeypatch.setattr(deps, "get_config", lambda: object())

    context = deps.get_run_context(request)

    assert context.on_run_completed is None


def test_gateway_exposes_no_scheduler_lifecycle_helpers() -> None:
    assert not hasattr(gateway_app, "_start_scheduled_task_service")
    assert not hasattr(gateway_app, "_stop_scheduled_task_service")
