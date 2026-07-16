from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.automations.errors import AutomationCutover, AutomationMigrationRequired
from app.gateway.auth_disabled import AUTH_SOURCE_SESSION
from app.gateway.routers import scheduled_tasks


def test_router_registers_list_endpoint():
    app = FastAPI()
    app.include_router(scheduled_tasks.router)
    client = TestClient(app)
    response = client.get("/api/scheduled-tasks")
    assert response.status_code != 404


def test_router_registers_trigger_route():
    app = FastAPI()
    app.include_router(scheduled_tasks.router)
    client = TestClient(app)
    response = client.post("/api/scheduled-tasks/task-1/trigger")
    assert response.status_code != 404


def test_router_registers_create_route():
    app = FastAPI()
    app.include_router(scheduled_tasks.router)
    client = TestClient(app)
    response = client.post(
        "/api/scheduled-tasks",
        json={
            "thread_id": "thread-1",
            "title": "Daily summary",
            "prompt": "Summarize thread",
            "schedule_type": "cron",
            "schedule_spec": {"cron": "0 9 * * *"},
            "timezone": "UTC",
        },
    )
    assert response.status_code != 404


class _LegacyGuard:
    def __init__(self, *, cutover: bool = False, expanded: bool = False) -> None:
        self.cutover = cutover
        self.expanded = expanded
        self.legacy_calls = 0
        self.mutation_calls = 0

    async def require_legacy_open(self) -> None:
        self.legacy_calls += 1
        if self.cutover:
            raise AutomationCutover("legacy-router")

    async def require_legacy_mutation_open(self) -> None:
        self.mutation_calls += 1
        if self.cutover:
            raise AutomationCutover("legacy-router")
        if self.expanded:
            raise AutomationMigrationRequired("legacy-router")

    async def require_project_open(self) -> None:
        return None


def _legacy_app(
    guard: _LegacyGuard,
    *,
    install_repositories: bool = True,
) -> tuple[FastAPI, AsyncMock, SimpleNamespace, SimpleNamespace]:
    app = FastAPI()
    app.include_router(scheduled_tasks.router)
    dispatch = AsyncMock()
    task_repo = SimpleNamespace(
        list_by_user=AsyncMock(return_value=[]),
        get=AsyncMock(return_value=None),
        list_by_user_and_thread=AsyncMock(return_value=[]),
    )
    run_repo = SimpleNamespace(list_by_task=AsyncMock(return_value=[]))
    app.state.automation_cutover_guard = guard
    if install_repositories:
        app.state.scheduled_task_repo = task_repo
        app.state.scheduled_task_run_repo = run_repo
    app.state.scheduled_task_service = type("Service", (), {"dispatch_task": dispatch})()
    app.state.thread_store = SimpleNamespace(check_access=AsyncMock(return_value=True))

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.user = SimpleNamespace(id="00000000-0000-0000-0000-000000000001")
        request.state.auth_source = AUTH_SOURCE_SESSION
        return await call_next(request)

    return app, dispatch, task_repo, run_repo


def test_expand_keeps_legacy_reads_but_freezes_every_mutation() -> None:
    app, dispatch, task_repo, run_repo = _legacy_app(_LegacyGuard(expanded=True))
    client = TestClient(app, raise_server_exceptions=False)

    read = client.get("/api/scheduled-tasks")
    mutations = [
        client.post("/api/scheduled-tasks", json={}),
        client.patch("/api/scheduled-tasks/task-1", json={}),
        client.post("/api/scheduled-tasks/task-1/pause"),
        client.post("/api/scheduled-tasks/task-1/resume"),
        client.post("/api/scheduled-tasks/task-1/trigger"),
        client.delete("/api/scheduled-tasks/task-1"),
    ]

    assert read.status_code == 200
    assert read.json() == []
    task_repo.list_by_user.assert_awaited_once_with("00000000-0000-0000-0000-000000000001")
    for response in mutations:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "AUTOMATION_MIGRATION_REQUIRED"
    task_repo.get.assert_not_awaited()
    task_repo.list_by_user_and_thread.assert_not_awaited()
    run_repo.list_by_task.assert_not_awaited()
    dispatch.assert_not_awaited()


def test_cutover_closes_every_legacy_route_before_repository_or_dispatch() -> None:
    app, dispatch, task_repo, run_repo = _legacy_app(_LegacyGuard(cutover=True))
    client = TestClient(app, raise_server_exceptions=False)

    responses = [
        client.get("/api/scheduled-tasks"),
        client.get("/api/scheduled-tasks/task-1"),
        client.get("/api/scheduled-tasks/task-1/runs"),
        client.get("/api/threads/thread-1/scheduled-tasks"),
        client.post("/api/scheduled-tasks", json={}),
        client.patch("/api/scheduled-tasks/task-1", json={}),
        client.post("/api/scheduled-tasks/task-1/pause"),
        client.post("/api/scheduled-tasks/task-1/resume"),
        client.post("/api/scheduled-tasks/task-1/trigger"),
        client.delete("/api/scheduled-tasks/task-1"),
    ]

    for response in responses:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "AUTOMATION_CUTOVER"
    task_repo.list_by_user.assert_not_awaited()
    task_repo.get.assert_not_awaited()
    task_repo.list_by_user_and_thread.assert_not_awaited()
    run_repo.list_by_task.assert_not_awaited()
    dispatch.assert_not_awaited()


def test_expand_missing_legacy_read_adapter_is_explicit_misconfiguration() -> None:
    app, _, _, _ = _legacy_app(
        _LegacyGuard(expanded=True),
        install_repositories=False,
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/api/scheduled-tasks")

    assert response.status_code == 503
    assert response.json() == {"detail": "Scheduled task repo not available"}


def test_expand_missing_legacy_run_adapter_is_explicit_misconfiguration() -> None:
    app, _, task_repo, _ = _legacy_app(
        _LegacyGuard(expanded=True),
        install_repositories=False,
    )
    app.state.scheduled_task_repo = task_repo
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/api/scheduled-tasks/task-1/runs")

    assert response.status_code == 503
    assert response.json() == {"detail": "Scheduled task run repo not available"}


def test_legacy_adapter_exposes_no_mutation_lease_or_dispatch_authority() -> None:
    from app.automations.legacy_reads import LegacyAutomationReadAdapter

    adapter = LegacyAutomationReadAdapter(lambda: None)

    for method in (
        "create",
        "update",
        "delete",
        "claim_due_tasks",
        "claim_next",
        "update_after_launch",
        "update_status",
        "dispatch_task",
    ):
        assert not hasattr(adapter, method)
