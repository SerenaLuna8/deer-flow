from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.automations.errors import AutomationCutover, AutomationMigrationRequired
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


def _legacy_app(guard: _LegacyGuard) -> tuple[FastAPI, AsyncMock]:
    app = FastAPI()
    app.include_router(scheduled_tasks.router)
    dispatch = AsyncMock()
    app.state.automation_cutover_guard = guard
    app.state.scheduled_task_repo = object()
    app.state.scheduled_task_service = type("Service", (), {"dispatch_task": dispatch})()
    return app, dispatch


def test_expand_keeps_legacy_reads_but_freezes_every_mutation() -> None:
    app, dispatch = _legacy_app(_LegacyGuard(expanded=True))
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

    assert read.status_code != 409
    for response in mutations:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "AUTOMATION_MIGRATION_REQUIRED"
    dispatch.assert_not_awaited()


def test_cutover_closes_every_legacy_route_before_repository_or_dispatch() -> None:
    app, dispatch = _legacy_app(_LegacyGuard(cutover=True))
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
    dispatch.assert_not_awaited()
