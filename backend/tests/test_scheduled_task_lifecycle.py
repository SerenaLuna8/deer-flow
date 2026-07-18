from app.gateway import app as gateway_app
from app.gateway.app import create_app


def test_gateway_app_excludes_global_scheduled_task_router():
    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/api/scheduled-tasks" not in paths


def test_gateway_exposes_no_scheduler_lifecycle_helpers() -> None:
    assert not hasattr(gateway_app, "_start_scheduled_task_service")
    assert not hasattr(gateway_app, "_stop_scheduled_task_service")
