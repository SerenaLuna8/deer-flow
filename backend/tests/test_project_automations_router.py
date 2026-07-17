from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import func, select
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.automations.dispatcher import AutomationDispatcher
from app.automations.error_mapping import automation_http_exception
from app.automations.errors import (
    AutomationConcurrencyLimit,
    AutomationForbidden,
    AutomationNotFound,
    AutomationUnavailable,
    AutomationVersionConflict,
)
from app.automations.models import AutomationRunView, AutomationView
from app.automations.occurrences import AutomationOccurrenceService
from app.automations.service import ProjectAutomationService
from app.gateway.automation_schemas import (
    AutomationCreateRequest,
    AutomationListQuery,
    AutomationPatchRequest,
    AutomationRunResponse,
    AutomationVersionRequest,
)
from app.gateway.deps import (
    automation_context,
    get_automation_dispatcher,
    get_automation_occurrence_service,
    get_automation_readiness_service,
    get_automation_scheduler_enabled,
    get_automation_service,
    project_session,
    require_project_automation_open,
)
from app.gateway.routers import project_automations
from app.gateway.routers.project_automations import create_automation
from app.gateway.trace_middleware import TraceMiddleware
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _context(role: ProjectRole = ProjectRole.ADMIN) -> PrivateWorkContext:
    project = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="request-automation-router",
    )
    return PrivateWorkContext.from_project(project)


def _automation_view(*, task_id: str = "task-1") -> AutomationView:
    return AutomationView(
        id=task_id,
        thread_id=None,
        context_mode="fresh_thread_per_run",
        agent_asset_id=uuid.uuid4(),
        agent_scope="project",
        title="Daily report",
        prompt="Summarize private work",
        schedule_type="cron",
        schedule_spec={"cron": "0 9 * * *"},
        timezone="UTC",
        status="enabled",
        next_run_at=NOW,
        last_run_at=None,
        last_outcome=None,
        last_error_code=None,
        run_count=0,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _run_view(*, occurrence_id: str = "occurrence-1", status: str = "running") -> AutomationRunView:
    return AutomationRunView(
        id=occurrence_id,
        automation_id="task-1",
        automation_version=1,
        scheduled_for=NOW,
        trigger="manual",
        status=status,  # type: ignore[arg-type]
        thread_id=str(uuid.uuid4()) if status == "running" else None,
        run_id=str(uuid.uuid4()) if status == "running" else None,
        error_code=None,
        started_at=NOW if status in {"launching", "running"} else None,
        finished_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _test_app(
    *,
    context: PrivateWorkContext | None = None,
    service: object | None = None,
    occurrences: object | None = None,
    dispatcher: object | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(project_automations.readiness_router)
    app.include_router(project_automations.router)
    context = context or _context()
    service = service or SimpleNamespace(
        create=AsyncMock(return_value=_automation_view()),
        list=AsyncMock(return_value=(_automation_view(),)),
        get=AsyncMock(return_value=_automation_view()),
        update=AsyncMock(return_value=_automation_view()),
        delete=AsyncMock(return_value=None),
        pause=AsyncMock(return_value=_automation_view()),
        resume=AsyncMock(return_value=_automation_view()),
    )
    occurrences = occurrences or SimpleNamespace(
        get=AsyncMock(return_value=_run_view()),
        list=AsyncMock(return_value=(_run_view(),)),
    )
    dispatcher = dispatcher or SimpleNamespace(admit_manual=AsyncMock())

    async def override_context() -> PrivateWorkContext:
        return context

    async def override_project_open() -> None:
        return None

    async def override_session():
        yield object()

    app.dependency_overrides[automation_context] = override_context
    app.dependency_overrides[require_project_automation_open] = override_project_open
    app.dependency_overrides[project_session] = override_session
    app.dependency_overrides[get_automation_service] = lambda: service
    app.dependency_overrides[get_automation_occurrence_service] = lambda: occurrences
    app.dependency_overrides[get_automation_dispatcher] = lambda: dispatcher
    app.dependency_overrides[get_automation_scheduler_enabled] = lambda: False
    app.dependency_overrides[get_automation_readiness_service] = lambda: SimpleNamespace(
        read=AsyncMock(
            return_value=SimpleNamespace(
                status="ready",
                code="AUTOMATION_READY",
                scheduler_enabled=False,
                scheduler_status="disabled",
                project_private_work_ready=True,
                automation_cutover_ready=True,
                request_id=context.request_id,
            )
        )
    )
    return app


def test_project_automation_routes_are_mounted() -> None:
    from app.gateway.app import app

    paths = {route.path for route in app.routes}
    assert "/api/projects/{project_id}/automations/readiness" in paths
    assert "/api/projects/{project_id}/automations" in paths
    assert "/api/projects/{project_id}/automations/{task_id}/trigger" in paths
    assert "/api/projects/{project_id}/automations/{task_id}/runs" in paths
    assert "/api/projects/{project_id}/automations/threads/{thread_id}" in paths


@pytest.mark.anyio
async def test_missing_automation_audit_composition_returns_stable_http_envelope() -> None:
    context = _context()
    app = FastAPI()
    app.add_middleware(TraceMiddleware, enabled=True)
    app.include_router(project_automations.router)
    app.state.automation_service = object()

    async def override_context() -> PrivateWorkContext:
        return context

    async def override_project_open() -> None:
        return None

    app.dependency_overrides[automation_context] = override_context
    app.dependency_overrides[require_project_automation_open] = override_project_open
    request_id = "automation-audit-composition-missing"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/projects/{context.project_id}/automations",
            headers={"X-Trace-Id": request_id},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "AUTOMATION_UNAVAILABLE",
        "message": AutomationUnavailable.public_message,
        "request_id": request_id,
    }


def test_readiness_is_mounted_before_dynamic_task_route_and_skips_open_guard() -> None:
    from app.gateway.app import app

    project_routes = [route for route in app.routes if route.path.startswith("/api/projects/{project_id}/automations")]
    readiness_index = next(index for index, route in enumerate(project_routes) if route.path.endswith("/readiness"))
    task_index = next(index for index, route in enumerate(project_routes) if route.path.endswith("/{task_id}"))
    assert readiness_index < task_index

    readiness = project_routes[readiness_index]
    dependency_names = {dependency.call.__name__ for dependency in readiness.dependant.dependencies if dependency.call is not None}
    assert "require_project_automation_open" not in dependency_names

    data_routes = [route for route in project_routes if not route.path.endswith("/readiness")]
    assert data_routes
    for route in data_routes:
        names = {dependency.call.__name__ for dependency in route.dependant.dependencies if dependency.call is not None}
        assert "require_project_automation_open" in names


@pytest.mark.parametrize(
    "model,payload",
    [
        (
            AutomationCreateRequest,
            {
                "title": "Daily report",
                "prompt": "Summarize",
                "context_mode": "fresh_thread_per_run",
                "agent_asset_id": str(uuid.uuid4()),
                "agent_scope": "system",
                "schedule_type": "cron",
                "schedule_spec": {"cron": "0 9 * * *"},
                "timezone": "UTC",
                "owner_user_id": str(uuid.uuid4()),
            },
        ),
        (AutomationPatchRequest, {"expected_version": 1, "title": "Changed", "project_id": str(uuid.uuid4())}),
        (AutomationVersionRequest, {"expected_version": 1, "non_interactive": True}),
        (AutomationListQuery, {"limit": 50, "offset": 0, "capabilities": ["automation.manage_own"]}),
    ],
)
def test_request_models_forbid_client_authority(model, payload) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_public_run_response_has_no_internal_authority_or_lease_fields() -> None:
    assert set(AutomationRunResponse.model_fields).isdisjoint(
        {
            "project_id",
            "owner_user_id",
            "membership_id",
            "membership_version",
            "lease_owner",
            "lease_expires_at",
            "manual_idempotency_hash",
            "occurrence_key",
            "resolved_membership_id",
            "resolved_membership_version",
            "error_message",
        }
    )


@pytest.mark.asyncio
async def test_create_uses_only_server_context() -> None:
    context = _context()
    body = AutomationCreateRequest(
        title="Daily report",
        prompt="Summarize my private work",
        context_mode="fresh_thread_per_run",
        agent_asset_id=uuid.uuid4(),
        agent_scope="system",
        schedule_type="cron",
        schedule_spec={"cron": "0 9 * * *"},
        timezone="Asia/Shanghai",
    )
    service = SimpleNamespace(create=AsyncMock(return_value=_automation_view()))

    result = await create_automation.__wrapped__(
        body=body,
        context=context,
        service=service,
    )

    assert result.title == "Daily report"
    service.create.assert_awaited_once_with(context, body.to_command())


@pytest.mark.anyio
async def test_http_validation_is_strict_for_body_query_and_idempotency_header() -> None:
    app = _test_app()
    project_id = uuid.uuid4()
    task_id = "task-1"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        invalid_body = await client.post(
            f"/api/projects/{project_id}/automations",
            json={
                "title": "Daily report",
                "prompt": "Summarize",
                "context_mode": "fresh_thread_per_run",
                "agent_asset_id": str(uuid.uuid4()),
                "agent_scope": "project",
                "schedule_type": "cron",
                "schedule_spec": {"cron": "0 9 * * *"},
                "timezone": "UTC",
                "owner_user_id": str(uuid.uuid4()),
            },
        )
        invalid_query = await client.get(
            f"/api/projects/{project_id}/automations",
            params={"limit": 50, "offset": 0, "owner_user_id": str(uuid.uuid4())},
        )
        invalid_header = await client.post(
            f"/api/projects/{project_id}/automations/{task_id}/trigger",
            headers={"Idempotency-Key": "not-a-uuid"},
        )

    for response in (invalid_body, invalid_query, invalid_header):
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "AUTOMATION_INVALID"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (AutomationNotFound("req"), 404, "AUTOMATION_NOT_FOUND"),
        (AutomationForbidden("req"), 403, "AUTOMATION_FORBIDDEN"),
        (AutomationVersionConflict("req"), 409, "AUTOMATION_VERSION_CONFLICT"),
        (AutomationConcurrencyLimit("req"), 429, "AUTOMATION_CONCURRENCY_LIMIT"),
        (AutomationUnavailable("req"), 503, "AUTOMATION_UNAVAILABLE"),
    ],
)
async def test_http_maps_only_public_automation_errors(error, status: int, code: str) -> None:
    service = SimpleNamespace(get=AsyncMock(side_effect=error))
    app = _test_app(service=service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/api/projects/{uuid.uuid4()}/automations/task-1")

    assert response.status_code == status
    assert response.json()["detail"] == {
        "code": code,
        "message": type(error).public_message,
        "request_id": "req",
    }


@pytest.mark.anyio
async def test_manual_trigger_atomically_admits_without_scheduler_gate() -> None:
    context = _context()
    occurrence_record = SimpleNamespace(id="occurrence-1", status="queued")
    occurrences = SimpleNamespace(
        get=AsyncMock(return_value=_run_view()),
    )
    dispatcher = SimpleNamespace(admit_manual=AsyncMock(return_value=SimpleNamespace(occurrence=occurrence_record)))
    app = _test_app(
        context=context,
        occurrences=occurrences,
        dispatcher=dispatcher,
    )
    key = uuid.uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{context.project_id}/automations/task-1/trigger",
            headers={"Idempotency-Key": str(key)},
        )

    assert response.status_code == 200
    dispatcher.admit_manual.assert_awaited_once()
    admission = dispatcher.admit_manual.await_args
    assert admission.args == (context, "task-1", key)
    assert admission.kwargs["scheduled_for"].tzinfo is UTC
    occurrences.get.assert_awaited_once_with(context, "occurrence-1")


@pytest_asyncio.fixture()
async def postgres_seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield seed
    finally:
        await seed.engine.dispose()


def _postgres_app(
    seed: M4ThreadSeed,
    identity: dict[str, uuid.UUID],
) -> tuple[FastAPI, AsyncMock]:
    app = FastAPI()
    app.include_router(project_automations.readiness_router)
    app.include_router(project_automations.router)

    async def request_session():
        async with seed.factory() as session:
            yield session

    async def current_context():
        selected = {
            seed.owner_a.user_id: seed.owner_a,
            seed.viewer.user_id: seed.viewer,
            seed.owner_b.user_id: seed.owner_b,
        }.get(identity["user_id"])
        if selected is None or selected.project_id != seed.owner_a.project_id:
            raise automation_http_exception(AutomationNotFound("postgres-api"))
        return selected

    async def project_open() -> None:
        return None

    real_dispatcher = AutomationDispatcher(seed.factory)
    dispatcher = AsyncMock(side_effect=real_dispatcher.admit_manual)
    app.dependency_overrides[project_session] = request_session
    app.dependency_overrides[automation_context] = current_context
    app.dependency_overrides[require_project_automation_open] = project_open
    app.dependency_overrides[get_automation_service] = lambda: ProjectAutomationService(
        seed.factory,
        clock=lambda: NOW,
    )
    app.dependency_overrides[get_automation_occurrence_service] = lambda: AutomationOccurrenceService(
        seed.factory,
        max_concurrent_runs=3,
    )
    app.dependency_overrides[get_automation_dispatcher] = lambda: SimpleNamespace(admit_manual=dispatcher)
    app.dependency_overrides[get_automation_scheduler_enabled] = lambda: False
    return app, dispatcher


@pytest.mark.postgres
@pytest.mark.anyio
async def test_real_postgres_api_enforces_owner_viewer_scope_and_manual_history(
    postgres_seed: M4ThreadSeed,
) -> None:
    seed = postgres_seed
    identity = {"user_id": seed.owner_a.user_id}
    app, dispatcher = _postgres_app(seed, identity)
    project_id = seed.owner_a.project_id
    create_payload = {
        "title": "Daily project report",
        "prompt": "Summarize private project work",
        "context_mode": "fresh_thread_per_run",
        "agent_asset_id": str(seed.project_agent_id),
        "agent_scope": "project",
        "schedule_type": "cron",
        "schedule_spec": {"cron": "0 9 * * *"},
        "timezone": "UTC",
    }
    key = uuid.uuid4()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            f"/api/projects/{project_id}/automations",
            json=create_payload,
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]

        owner_list = await client.get(f"/api/projects/{project_id}/automations")
        assert owner_list.status_code == 200
        assert [item["id"] for item in owner_list.json()["items"]] == [task_id]

        triggered = await client.post(
            f"/api/projects/{project_id}/automations/{task_id}/trigger",
            headers={"Idempotency-Key": str(key)},
        )
        assert triggered.status_code == 200, triggered.text
        assert triggered.json()["status"] == "running"
        assert not {
            "owner_user_id",
            "lease_owner",
            "manual_idempotency_hash",
            "occurrence_key",
        } & set(triggered.json())

        replay = await client.post(
            f"/api/projects/{project_id}/automations/{task_id}/trigger",
            headers={"Idempotency-Key": str(key)},
        )
        assert replay.status_code == 200
        assert replay.json()["id"] == triggered.json()["id"]

        history = await client.get(f"/api/projects/{project_id}/automations/{task_id}/runs")
        assert history.status_code == 200
        assert [item["id"] for item in history.json()["items"]] == [triggered.json()["id"]]

        identity["user_id"] = seed.viewer.user_id
        viewer_list = await client.get(f"/api/projects/{project_id}/automations")
        assert viewer_list.status_code == 200
        assert viewer_list.json()["items"] == []
        viewer_create = await client.post(
            f"/api/projects/{project_id}/automations",
            json=create_payload,
        )
        assert viewer_create.status_code == 403
        assert viewer_create.json()["detail"]["code"] == "AUTOMATION_FORBIDDEN"

        identity["user_id"] = uuid.uuid4()
        outsider = await client.get(f"/api/projects/{project_id}/automations/{task_id}")
        assert outsider.status_code == 404

    assert dispatcher.await_count == 2
    async with seed.factory() as session:
        count = await session.scalar(select(func.count()).select_from(ScheduledTaskRunRow).where(ScheduledTaskRunRow.task_id == task_id))
    assert count == 1
