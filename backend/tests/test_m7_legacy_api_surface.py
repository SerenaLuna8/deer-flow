from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

from app.gateway import private_work_schemas
from app.gateway.app import create_app
from app.gateway.deps import gateway_platform_runtime
from app.gateway.routers import private_work

LEGACY_PREFIXES = (
    "/api/threads",
    "/api/runs",
    "/api/assistants",
    "/api/memory",
)

DELETED_MODULES = (
    "app.gateway.routers.artifacts",
    "app.gateway.routers.assistants_compat",
    "app.gateway.routers.feedback",
    "app.gateway.routers.memory",
    "app.gateway.routers.runs",
    "app.gateway.routers.suggestions",
    "app.gateway.routers.thread_runs",
    "app.gateway.routers.threads",
    "app.gateway.routers.uploads",
)


def test_gateway_openapi_has_no_global_private_routes() -> None:
    paths = create_app().openapi()["paths"]

    assert all(not path.startswith(prefix) for path in paths for prefix in LEGACY_PREFIXES)


def test_gateway_openapi_keeps_legacy_scheduled_tasks_until_task_4() -> None:
    paths = create_app().openapi()["paths"]

    assert "/api/scheduled-tasks" in paths


def test_gateway_openapi_keeps_project_private_routes() -> None:
    paths = create_app().openapi()["paths"]
    prefix = "/api/projects/{project_id}/private-work"

    assert f"{prefix}/threads/{{thread_id}}/runs" in paths
    assert f"{prefix}/threads/{{thread_id}}/runs/stream" in paths
    assert f"{prefix}/threads/{{thread_id}}/runs/{{run_id}}/stream" in paths
    assert f"{prefix}/threads/{{thread_id}}/uploads" in paths
    assert f"{prefix}/artifacts/{{artifact_id}}" in paths
    assert f"{prefix}/threads/{{thread_id}}/runs/{{run_id}}/feedback" in paths


def test_gateway_runtime_has_no_legacy_execution_singletons() -> None:
    source = inspect.getsource(gateway_platform_runtime)

    for forbidden in (
        "RunManager",
        "make_stream_bridge",
        "make_run_event_store",
        "_migrate_orphaned_threads",
        "ScheduledTaskRepository",
        "ScheduledTaskRunRepository",
        "ScheduledTaskService",
        "scheduled_task_repo",
        "scheduled_task_run_repo",
        "scheduled_task_service",
    ):
        assert forbidden not in source


def test_project_private_runtime_and_schemas_are_project_owned() -> None:
    assert importlib.util.find_spec("app.private_work.http_runtime") is not None
    assert hasattr(private_work_schemas, "PrivateRunCreateRequest")
    assert hasattr(private_work_schemas, "PrivateThreadTokenUsageResponse")
    assert private_work.PrivateRunCreateRequest is private_work_schemas.PrivateRunCreateRequest
    assert private_work.PrivateThreadTokenUsageResponse is private_work_schemas.PrivateThreadTokenUsageResponse


def test_deleted_legacy_router_modules_cannot_be_imported() -> None:
    for module_name in DELETED_MODULES:
        assert importlib.util.find_spec(module_name) is None


def test_nginx_and_launchers_have_no_langgraph_compat_rewrite() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    paths = (
        "docker/nginx/nginx.conf",
        "docker/nginx/nginx.local.conf",
        "scripts/serve.sh",
        "scripts/docker.sh",
        "scripts/deploy.sh",
    )

    for relative_path in paths:
        source = (repository_root / relative_path).read_text(encoding="utf-8")
        assert "/api/langgraph" not in source
