from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.gateway.deps import project_session
from app.gateway.routers import admin_operations
from app.gateway.routers.admin_operations import overview_response
from app.reliability.models import ReliabilityReadiness
from app.reliability.operations import (
    AggregateUsage,
    OperationsCounts,
    OperationsOverview,
    SystemOperationsRepository,
)


def _readiness() -> ReliabilityReadiness:
    return ReliabilityReadiness(
        status="ready",
        database="ready",
        schema="ready",
        worker_fleet="ready",
        scheduler="disabled",
        stream="ready",
        quota="ready",
        audit="ready",
        request_id="operations-aggregate-contract",
        role="gateway",
        worker_count=4,
        worker_capacity=12,
        worker_oldest_heartbeat_age_seconds=8,
        private_run_worker_fleet="ready",
        private_run_worker_count=2,
        private_run_worker_capacity=7,
        scheduler_ownership="disabled",
        schema_state="ready",
        run_skill_writer_mode="legacy_v3",
        run_skill_writer_artifact_version="run-skill-snapshot-writer-v2",
        run_skill_legacy_policy_digest=("e01a816a3f20a4ecf088e2f0d37b92ba16634e5969860b900a14924312edb6e8"),
        run_skill_writer_ready=True,
    )


def test_admin_overview_exposes_private_run_fleet_and_queue_aggregates() -> None:
    readiness = _readiness()
    overview = OperationsOverview(
        counts=OperationsCounts(
            projects=3,
            suspended_projects=1,
            queued_jobs=5,
            running_jobs=2,
            dead_jobs=1,
            ready_jobs=4,
            oldest_ready_job_age_seconds=17,
            stale_leases=2,
            waiting_for_worker_runs=3,
            waiting_for_terminalization_runs=1,
        ),
        usage=(),
    )

    payload = overview_response(overview, readiness).model_dump(
        by_alias=True,
        mode="json",
    )

    assert payload["readiness"] == {
        "status": "ready",
        "database": "ready",
        "schema": "ready",
        "worker_fleet": "ready",
        "scheduler": "disabled",
        "stream": "ready",
        "quota": "ready",
        "audit": "ready",
        "knowledge": "disabled",
        "role": "gateway",
        "worker_count": 4,
        "worker_capacity": 12,
        "worker_oldest_heartbeat_age_seconds": 8,
        "private_run_worker_fleet": "ready",
        "private_run_worker_count": 2,
        "private_run_worker_capacity": 7,
        "scheduler_ownership": "disabled",
        "schema_state": "ready",
        "run_skill_writer_mode": "legacy_v3",
        "run_skill_writer_artifact_version": "run-skill-snapshot-writer-v2",
        "run_skill_legacy_policy_digest": ("e01a816a3f20a4ecf088e2f0d37b92ba16634e5969860b900a14924312edb6e8"),
        "run_skill_writer_ready": True,
    }
    assert payload["counts"] == {
        "projects": 3,
        "suspended_projects": 1,
        "queued_jobs": 5,
        "running_jobs": 2,
        "dead_jobs": 1,
        "ready_jobs": 4,
        "oldest_ready_job_age_seconds": 17,
        "stale_leases": 2,
        "waiting_for_worker_runs": 3,
        "waiting_for_terminalization_runs": 1,
    }


class _Result:
    def __init__(self, *, one: object | None = None) -> None:
        self._one = one

    def one(self) -> object:
        assert self._one is not None
        return self._one

    def all(self) -> list[object]:
        return []


class _Session:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement) -> _Result:
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.statements.append(sql)
        if "FROM projects" in sql:
            return _Result(one=(3, 1))
        if "JOIN jobs" in sql:
            return _Result(
                one=SimpleNamespace(
                    queued_jobs=5,
                    running_jobs=2,
                    dead_jobs=1,
                    ready_jobs=4,
                    oldest_ready_job_age_seconds=17,
                    stale_leases=2,
                    waiting_for_worker_runs=3,
                    waiting_for_terminalization_runs=1,
                )
            )
        return _Result()


@pytest.mark.asyncio
async def test_operations_repository_uses_database_clock_for_queue_aggregates() -> None:
    session = _Session()

    overview = await SystemOperationsRepository(
        session,  # type: ignore[arg-type]
    ).overview(
        now=datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        worker_fresh_for_seconds=60,
    )

    assert overview.counts.ready_jobs == 4
    assert overview.counts.oldest_ready_job_age_seconds == 17
    assert overview.counts.stale_leases == 2
    assert overview.counts.waiting_for_worker_runs == 3
    assert overview.counts.waiting_for_terminalization_runs == 1
    job_statement = next(sql for sql in session.statements if "JOIN jobs" in sql)
    assert "clock_timestamp()" in job_statement
    assert "worker_nodes" in job_statement


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _RouteSession:
    def begin(self) -> _Transaction:
        return _Transaction()


def test_admin_overview_route_passes_the_readiness_freshness_policy_to_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_freshness: list[int] = []
    readiness_freshness: list[int] = []

    async def resolve_context(*_args: object) -> None:
        return None

    async def read_readiness(
        *_args: object,
        worker_fresh_for_seconds: int,
        **_kwargs: object,
    ):
        readiness_freshness.append(worker_fresh_for_seconds)
        return _readiness()

    async def read_channels():
        return ()

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def overview(
            self,
            *,
            worker_fresh_for_seconds: int,
        ) -> OperationsOverview:
            observed_freshness.append(worker_fresh_for_seconds)
            return OperationsOverview(
                counts=OperationsCounts(
                    projects=0,
                    suspended_projects=0,
                    queued_jobs=0,
                    running_jobs=0,
                    dead_jobs=0,
                    ready_jobs=0,
                    oldest_ready_job_age_seconds=None,
                    stale_leases=0,
                    waiting_for_worker_runs=0,
                    waiting_for_terminalization_runs=0,
                ),
                usage=tuple(
                    AggregateUsage(dimension=dimension, used=0, reserved=0)
                    for dimension in (
                        "members",
                        "storage_bytes",
                        "concurrent_runs",
                        "mcp_calls_daily",
                    )
                ),
            )

    monkeypatch.setattr(
        admin_operations,
        "resolve_current_system_audit_context",
        resolve_context,
    )
    monkeypatch.setattr(
        admin_operations,
        "current_reliability_readiness",
        read_readiness,
    )
    monkeypatch.setattr(
        admin_operations,
        "current_channel_provider_health",
        read_channels,
    )
    monkeypatch.setattr(
        admin_operations,
        "SystemOperationsRepository",
        Repository,
    )
    monkeypatch.setattr(
        admin_operations,
        "get_app_config",
        lambda: SimpleNamespace(worker=SimpleNamespace(heartbeat_seconds=20)),
    )

    app = FastAPI()
    app.include_router(admin_operations.router)
    app.dependency_overrides[admin_operations.authenticated_system_identity] = lambda: (
        uuid.UUID("00000000-0000-4000-8000-000000000001"),
        "operations-route",
    )

    async def session_override():
        yield _RouteSession()

    app.dependency_overrides[project_session] = session_override

    response = TestClient(app).get("/api/admin/operations")

    assert response.status_code == 200, response.text
    assert readiness_freshness == [60]
    assert observed_freshness == [60]
