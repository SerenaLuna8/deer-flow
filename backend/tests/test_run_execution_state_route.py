from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as SATimeoutError

from app.gateway.deps import (
    get_config,
    private_work_context,
    project_session,
    require_project_private_open,
)
from app.gateway.routers import private_work as legacy
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole

PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OWNER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
MEMBERSHIP_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
THREAD_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
RUN_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
JOB_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
WORKER_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")
ATTEMPT_ID = uuid.UUID("88888888-8888-4888-8888-888888888888")
OBSERVED_AT = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=OWNER_ID,
            project_id=PROJECT_ID,
            membership_id=MEMBERSHIP_ID,
            role=ProjectRole.RUNNER,
            capabilities=frozenset({Capability.PRIVATE_WORK_READ_OWN}),
            membership_version=5,
            request_id="run-execution-state-route",
        )
    )


def _executing_row(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "observed_at": OBSERVED_AT,
        "run_job_id": JOB_ID,
        "run_status": "running",
        "run_execution_started_at": OBSERVED_AT - timedelta(seconds=12),
        "run_execution_lease_token_hash": "a" * 64,
        "run_execution_lease_expires_at": OBSERVED_AT + timedelta(seconds=45),
        "job_id": JOB_ID,
        "job_status": "running",
        "job_created_at": OBSERVED_AT - timedelta(seconds=15),
        "job_updated_at": OBSERVED_AT - timedelta(seconds=12),
        "job_available_at": OBSERVED_AT - timedelta(seconds=15),
        "job_completed_at": None,
        "job_attempt_count": 1,
        "job_max_attempts": 3,
        "job_retry_safety": "safe",
        "job_cancel_requested_at": None,
        "job_lease_owner_id": WORKER_ID,
        "job_lease_token_hash": "a" * 64,
        "job_lease_expires_at": OBSERVED_AT + timedelta(seconds=45),
        "active_attempt_id": ATTEMPT_ID,
        "active_attempt_number": 1,
        "active_attempt_worker_id": WORKER_ID,
        "active_attempt_lease_token_hash": "a" * 64,
        "active_attempt_started_at": OBSERVED_AT - timedelta(seconds=13),
        "active_attempt_execution_started_at": OBSERVED_AT - timedelta(seconds=12),
        "active_attempt_finished_at": None,
        "active_attempt_outcome": None,
        "latest_attempt_id": None,
        "latest_attempt_number": None,
        "latest_attempt_outcome": None,
        "latest_attempt_finished_at": None,
        "lease_worker_id": WORKER_ID,
        "lease_worker_heartbeat_at": OBSERVED_AT - timedelta(seconds=5),
        "eligible_worker_exists": True,
    }
    values.update(changes)
    return SimpleNamespace(**values)


class _Result:
    def __init__(
        self,
        *,
        rows: tuple[object, ...] = (),
        mapping: object | None = None,
    ) -> None:
        self._rows = rows
        self._mapping = mapping

    def all(self) -> list[object]:
        return list(self._rows)

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> object | None:
        return self._mapping


class _Session:
    def __init__(
        self,
        projection_row: object | None,
        *,
        fail_reader: bool = False,
    ) -> None:
        self._projection_row = projection_row
        self._fail_reader = fail_reader
        self.calls = 0

    async def execute(self, _statement) -> _Result:
        self.calls += 1
        if self.calls == 1:
            return _Result(
                rows=(
                    SimpleNamespace(
                        project_id=PROJECT_ID,
                        membership_id=MEMBERSHIP_ID,
                        role="runner",
                        membership_version=5,
                    ),
                ),
            )
        if self._fail_reader:
            raise SATimeoutError("execution state timeout")
        return _Result(mapping=self._projection_row)


def _client(session: _Session) -> TestClient:
    app = FastAPI()
    app.include_router(legacy.router)
    context = _context()
    app.dependency_overrides[private_work_context] = lambda: context
    app.dependency_overrides[require_project_private_open] = lambda: None
    app.dependency_overrides[get_config] = lambda: SimpleNamespace(
        worker=SimpleNamespace(heartbeat_seconds=20),
    )

    async def session_override():
        yield session

    app.dependency_overrides[project_session] = session_override
    return TestClient(app)


def _url() -> str:
    return f"/api/projects/{PROJECT_ID}/private-work/threads/{THREAD_ID}/runs/{RUN_ID}/execution-state"


def test_execution_state_route_is_registered_at_exact_scoped_path() -> None:
    paths = {route.path for route in legacy.router.routes if "execution-state" in route.path}

    assert paths == {
        "/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}/execution-state",
    }


def test_execution_state_route_returns_only_six_public_fields() -> None:
    response = _client(_Session(_executing_row())).get(_url())

    assert response.status_code == 200
    assert response.json() == {
        "phase": "executing",
        "observed_at": "2026-08-25T10:00:00Z",
        "phase_started_at": "2026-08-25T09:59:48Z",
        "execution_started_at": "2026-08-25T09:59:48Z",
        "retry_at": None,
        "run_status": "running",
    }
    serialized = response.text.lower()
    assert "worker" not in serialized
    assert "lease" not in serialized
    assert "affinity" not in serialized
    assert "hash" not in serialized


def test_execution_state_route_hides_wrong_scope_as_not_found() -> None:
    response = _client(_Session(None)).get(_url())

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_NOT_FOUND"


def test_execution_state_route_maps_identity_mismatch_to_503() -> None:
    response = _client(
        _Session(
            _executing_row(active_attempt_lease_token_hash="b" * 64),
        )
    ).get(_url())

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_UNAVAILABLE"


def test_execution_state_route_maps_database_failure_to_retryable_503() -> None:
    response = _client(_Session(None, fail_reader=True)).get(_url())

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["detail"] == {
        "code": "PRIVATE_WORK_UNAVAILABLE",
        "message": "Private work is unavailable.",
        "request_id": "run-execution-state-route",
    }
