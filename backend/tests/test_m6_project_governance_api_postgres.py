from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from support.m4_private_threads import seed_m4_thread_database

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditOutcome,
    AuditTarget,
    AuditTargetKind,
    AuditUnavailable,
)
from app.audit.service import AuditService, _bind_gateway_audit_process
from app.audit.sinks import OperationalAuditSink
from app.gateway.deps import (
    get_operational_audit_sink,
    get_project_audit_service,
    get_project_quota_service,
    project_session,
)
from app.gateway.routers import project_audit, project_usage
from app.gateway.routers.projects import authenticated_project_identity
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.projects.token_usage import read_project_token_usage_24h
from app.quotas.service import QuotaService
from app.reliability.error_mapping import (
    ReliabilityHTTPException,
    reliability_http_exception_handler,
)
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.quotas.model import ProjectQuotaRow, ProjectUsageLedgerRow
from deerflow.persistence.run.model import RunRow


def _keyring() -> AuditHmacKeyring:
    return AuditHmacKeyring(
        active_key_id="governance-v1",
        _keys={"governance-v1": b"g" * 32},
    )


def _project_context(context) -> ProjectContext:
    return ProjectContext(
        user_id=context.user_id,
        project_id=context.project_id,
        membership_id=context.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=context.membership_version,
        request_id=context.request_id,
    )


def _test_app(seed, quotas: QuotaService, audit: AuditService, sink: object) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(
        ReliabilityHTTPException,
        reliability_http_exception_handler,
    )
    app.include_router(project_usage.router)
    app.include_router(project_audit.router)
    app.state.project_quota_service = quotas
    app.state.project_audit_service = audit
    app.state.operational_audit_sink = sink

    async def request_session():
        async with seed.factory() as session:
            yield session

    async def project_identity(request: Request) -> tuple[uuid.UUID, str]:
        return (
            uuid.UUID(request.headers["x-test-user"]),
            request.headers.get("x-trace-id", "governance-api-test"),
        )

    app.dependency_overrides[project_session] = request_session
    app.dependency_overrides[authenticated_project_identity] = project_identity
    app.dependency_overrides[get_project_quota_service] = lambda: quotas
    app.dependency_overrides[get_project_audit_service] = lambda: audit
    app.dependency_overrides[get_operational_audit_sink] = lambda: sink
    return app


async def _append_run_audit(
    service: AuditService,
    session,
    context,
    target_id: uuid.UUID,
    *,
    occurred_at: datetime,
) -> None:
    await service.append(
        session,
        AuditActor.user(context.user_id),
        AuditAction.RUN_ADMITTED,
        AuditTarget(
            AuditTargetKind.RUN,
            target_id,
            context.project_id,
        ),
        AuditOutcome.SUCCESS,
        {"job_type": "private_run", "non_interactive": False},
        request_id="raw-client-trace-must-not-leak",
        occurred_at=occurred_at,
    )


async def _seed_token_run(
    seed,
    *,
    scope,
    thread_id: str,
    run_id: str,
    completed_at: datetime | None,
    input_tokens: int,
    output_tokens: int,
    status: str = "success",
    created_at: datetime | None = None,
) -> None:
    project_id = uuid.UUID(scope.project_id)
    owner_user_id = str(uuid.UUID(scope.owner_user_id))
    job_id = uuid.uuid4()
    job_status = {
        "success": "succeeded",
        "interrupted": "cancelled",
        "error": "failed",
        "timeout": "failed",
        "pending": "queued",
        "running": "running",
    }[status]
    run_created_at = created_at or (completed_at or datetime.now(UTC)) - timedelta(
        minutes=5,
    )
    run_updated_at = completed_at or run_created_at
    async with seed.factory() as session, session.begin():
        run = RunRow(
            run_id=run_id,
            thread_id=thread_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            status=status,
            total_input_tokens=input_tokens,
            total_output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            lead_agent_tokens=input_tokens + output_tokens,
            token_usage_by_model={
                "test-model": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }
            },
            created_at=run_created_at,
            updated_at=run_updated_at,
        )
        session.add(run)
        await session.flush()
        session.add(
            JobRow(
                id=job_id,
                job_type="private_run",
                project_id=project_id,
                owner_user_id=owner_user_id,
                run_id=run_id,
                idempotency_key=uuid.uuid4().hex + uuid.uuid4().hex,
                status=job_status,
                max_attempts=1,
                completed_at=completed_at,
                updated_at=run_updated_at,
            )
        )
        await session.flush()
        run.job_id = job_id
        await session.flush()


def _assert_public_not_found(response: httpx.Response) -> None:
    assert response.status_code == 404
    assert response.json() == {
        "code": "RELIABILITY_NOT_FOUND",
        "message": "Reliability resource was not found.",
        "request_id": "governance-api-test",
    }


def test_project_governance_routes_are_mounted() -> None:
    from app.gateway.app import app

    paths = {route.path for route in app.routes}
    assert "/api/projects/{project_id}/usage" in paths
    assert "/api/projects/{project_id}/usage/token-series" in paths
    assert "/api/projects/{project_id}/usage/limits" in paths
    assert "/api/projects/{project_id}/audit" in paths


@pytest.mark.postgres
@pytest.mark.anyio
async def test_usage_is_admin_and_project_scoped_with_effective_limits_and_warning_state(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    keyring = _keyring()
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=keyring)
    audit = AuditService(seed.factory, keyring)
    sink = OperationalAuditSink(
        audit,
        process_context=_bind_gateway_audit_process(audit),
    )
    app = _test_app(seed, quotas, audit, sink)
    try:
        await quotas.reserve_new_session(
            seed.owner_a,
            "concurrent_runs",
            3,
            "run:governance-usage",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/projects/{seed.owner_a.project_id}/usage",
                headers={"x-test-user": str(seed.owner_a.user_id)},
            )
            member = await client.get(
                f"/api/projects/{seed.owner_a.project_id}/usage",
                headers={"x-test-user": str(seed.owner_b.user_id)},
            )
            viewer = await client.get(
                f"/api/projects/{seed.owner_a.project_id}/usage",
                headers={"x-test-user": str(seed.viewer.user_id)},
            )
            wrong_project = await client.get(
                f"/api/projects/{seed.project_b_owner_a.project_id}/usage",
                headers={"x-test-user": str(seed.owner_b.user_id)},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["policy"] == {
            "version": 0,
            "configured": {
                "member_limit": None,
                "storage_bytes_limit": None,
                "concurrent_run_limit": None,
                "mcp_calls_daily_limit": None,
            },
            "effective": {
                "member_limit": 20,
                "storage_bytes_limit": 5_368_709_120,
                "concurrent_run_limit": 3,
                "mcp_calls_daily_limit": 10_000,
            },
        }
        dimensions = {item["dimension"]: item for item in body["dimensions"]}
        assert set(dimensions) == {
            "members",
            "storage_bytes",
            "concurrent_runs",
            "mcp_calls_daily",
        }
        assert dimensions["concurrent_runs"] == {
            "dimension": "concurrent_runs",
            "bucket": "lifetime",
            "used": 0,
            "reserved": 3,
            "limit": 3,
            "warning_threshold_reached": True,
        }
        assert dimensions["members"]["warning_threshold_reached"] is False
        for hidden in (member, viewer, wrong_project):
            _assert_public_not_found(hidden)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_token_series_has_24_hour_buckets_and_is_admin_project_scoped(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    keyring = _keyring()
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=keyring)
    audit = AuditService(seed.factory, keyring)
    app = _test_app(
        seed,
        quotas,
        audit,
        OperationalAuditSink(
            audit,
            process_context=_bind_gateway_audit_process(audit),
        ),
    )
    now = datetime.now(UTC)
    owner_a_thread = f"usage-owner-a-{uuid.uuid4()}"
    owner_b_thread = f"usage-owner-b-{uuid.uuid4()}"
    project_b_thread = f"usage-project-b-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            threads = PrivateThreadRepository(session)
            await threads.create(
                scope=seed.owner_a_scope,
                thread_id=owner_a_thread,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await threads.create(
                scope=seed.owner_b_scope,
                thread_id=owner_b_thread,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await threads.create(
                scope=seed.project_b_owner_a_scope,
                thread_id=project_b_thread,
                agent=ThreadAgentRef(seed.project_b_agent_id, "project"),
            )

        await _seed_token_run(
            seed,
            scope=seed.owner_a_scope,
            thread_id=owner_a_thread,
            run_id=f"usage-owner-a-{uuid.uuid4()}",
            completed_at=now - timedelta(minutes=10),
            input_tokens=80,
            output_tokens=20,
        )
        await _seed_token_run(
            seed,
            scope=seed.owner_b_scope,
            thread_id=owner_b_thread,
            run_id=f"usage-owner-b-{uuid.uuid4()}",
            completed_at=now - timedelta(hours=2, minutes=10),
            input_tokens=30,
            output_tokens=10,
            status="interrupted",
        )
        await _seed_token_run(
            seed,
            scope=seed.owner_a_scope,
            thread_id=owner_a_thread,
            run_id=f"usage-old-{uuid.uuid4()}",
            completed_at=now - timedelta(hours=25),
            input_tokens=999,
            output_tokens=1,
        )
        await _seed_token_run(
            seed,
            scope=seed.project_b_owner_a_scope,
            thread_id=project_b_thread,
            run_id=f"usage-project-b-{uuid.uuid4()}",
            completed_at=now - timedelta(minutes=5),
            input_tokens=500,
            output_tokens=500,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/projects/{seed.owner_a.project_id}/usage/token-series",
                headers={"x-test-user": str(seed.owner_a.user_id)},
            )
            member = await client.get(
                f"/api/projects/{seed.owner_a.project_id}/usage/token-series",
                headers={"x-test-user": str(seed.owner_b.user_id)},
            )
            wrong_project = await client.get(
                f"/api/projects/{seed.project_b_owner_a.project_id}/usage/token-series",
                headers={"x-test-user": str(seed.owner_b.user_id)},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["bucket_minutes"] == 60
        assert len(body["points"]) == 24
        assert body["totals"] == {
            "input_tokens": 110,
            "output_tokens": 30,
            "total_tokens": 140,
        }
        assert sum(point["total_tokens"] for point in body["points"]) == 140
        assert sum(point["input_tokens"] for point in body["points"]) == 110
        assert sum(point["output_tokens"] for point in body["points"]) == 30
        bucket_starts = [datetime.fromisoformat(point["bucket_start"]) for point in body["points"]]
        assert all(value.tzinfo is not None for value in bucket_starts)
        assert all(later - earlier == timedelta(hours=1) for earlier, later in zip(bucket_starts, bucket_starts[1:]))
        _assert_public_not_found(member)
        _assert_public_not_found(wrong_project)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_token_series_uses_deterministic_utc_settlement_boundaries_and_zero_fills(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    fixed_now = datetime(2026, 7, 27, 10, 37, 42, tzinfo=UTC)
    current_hour = fixed_now.replace(minute=0, second=0, microsecond=0)
    window_start = current_hour - timedelta(hours=23)
    thread_id = f"usage-boundaries-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )

        await _seed_token_run(
            seed,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=f"usage-at-start-{uuid.uuid4()}",
            completed_at=window_start,
            input_tokens=10,
            output_tokens=1,
        )
        await _seed_token_run(
            seed,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=f"usage-before-start-{uuid.uuid4()}",
            completed_at=window_start - timedelta(microseconds=1),
            input_tokens=900,
            output_tokens=99,
        )
        await _seed_token_run(
            seed,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=f"usage-at-end-{uuid.uuid4()}",
            completed_at=fixed_now,
            input_tokens=20,
            output_tokens=2,
        )
        await _seed_token_run(
            seed,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=f"usage-after-end-{uuid.uuid4()}",
            completed_at=fixed_now + timedelta(microseconds=1),
            input_tokens=800,
            output_tokens=88,
        )
        await _seed_token_run(
            seed,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=f"usage-active-{uuid.uuid4()}",
            completed_at=fixed_now - timedelta(hours=2),
            input_tokens=700,
            output_tokens=77,
            status="running",
        )
        await _seed_token_run(
            seed,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=f"usage-without-completion-{uuid.uuid4()}",
            completed_at=None,
            created_at=fixed_now - timedelta(hours=1),
            input_tokens=600,
            output_tokens=66,
        )

        async with seed.factory() as session, session.begin():
            # The query must pin UTC bucket boundaries independently of the
            # connection's presentation timezone.
            await session.execute(text("SET LOCAL TIME ZONE 'Asia/Kathmandu'"))
            series = await read_project_token_usage_24h(
                session,
                seed.owner_a.project_id,
                now=fixed_now,
            )
            empty = await read_project_token_usage_24h(
                session,
                uuid.uuid4(),
                now=fixed_now,
            )

        expected_bucket_starts = tuple(window_start + timedelta(hours=offset) for offset in range(24))
        assert series.window_start == window_start
        assert series.window_end == fixed_now
        assert series.bucket_minutes == 60
        assert tuple(point.bucket_start for point in series.points) == (expected_bucket_starts)
        assert all(point.bucket_start.utcoffset() == timedelta(0) for point in series.points)
        assert series.points[-1].bucket_start == current_hour
        assert series.points[-1].bucket_start < series.window_end < series.points[-1].bucket_start + timedelta(hours=1)

        assert (
            series.points[0].input_tokens,
            series.points[0].output_tokens,
            series.points[0].total_tokens,
        ) == (10, 1, 11)
        assert (
            series.points[-1].input_tokens,
            series.points[-1].output_tokens,
            series.points[-1].total_tokens,
        ) == (20, 2, 22)
        assert all(point.total_tokens == 0 for point in series.points[1:-1])
        assert (
            series.input_tokens,
            series.output_tokens,
            series.total_tokens,
        ) == (30, 3, 33)

        assert empty.window_start == window_start
        assert empty.window_end == fixed_now
        assert tuple(point.bucket_start for point in empty.points) == (expected_bucket_starts)
        assert all(point.total_tokens == 0 for point in empty.points)
        assert (empty.input_tokens, empty.output_tokens, empty.total_tokens) == (
            0,
            0,
            0,
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_quota_policy_update_is_versioned_tightening_and_audit_atomic(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    keyring = _keyring()
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=keyring)
    audit = AuditService(seed.factory, keyring)
    sink = OperationalAuditSink(
        audit,
        process_context=_bind_gateway_audit_process(audit),
    )
    app = _test_app(seed, quotas, audit, sink)
    payload = {
        "expected_version": 0,
        "limits": {
            "member_limit": 10,
            "storage_bytes_limit": 1_048_576,
            "concurrent_run_limit": 2,
            "mcp_calls_daily_limit": 500,
        },
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            updated = await client.patch(
                f"/api/projects/{seed.owner_a.project_id}/usage/limits",
                headers={"x-test-user": str(seed.owner_a.user_id)},
                json=payload,
            )
            conflict = await client.patch(
                f"/api/projects/{seed.owner_a.project_id}/usage/limits",
                headers={"x-test-user": str(seed.owner_a.user_id)},
                json=payload,
            )
            above_platform = await client.patch(
                f"/api/projects/{seed.owner_a.project_id}/usage/limits",
                headers={"x-test-user": str(seed.owner_a.user_id)},
                json={
                    "expected_version": 1,
                    "limits": {"concurrent_run_limit": 4},
                },
            )
            coerced_scalar = await client.patch(
                f"/api/projects/{seed.owner_a.project_id}/usage/limits",
                headers={"x-test-user": str(seed.owner_a.user_id)},
                json={
                    "expected_version": "1",
                    "limits": {"concurrent_run_limit": "1"},
                },
            )

        assert updated.status_code == 200
        assert updated.json()["version"] == 1
        assert updated.json()["effective"]["concurrent_run_limit"] == 2
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "RELIABILITY_CONFLICT"
        assert above_platform.status_code == 422
        assert above_platform.json()["code"] == "RELIABILITY_INVALID"
        assert coerced_scalar.status_code == 422
        assert coerced_scalar.json()["code"] == "RELIABILITY_INVALID"

        async with seed.factory() as session:
            policy = await session.get(ProjectQuotaRow, seed.owner_a.project_id)
            audit_actions = tuple(
                await session.scalars(
                    select(AuditLogRow.action).where(
                        AuditLogRow.project_id == seed.owner_a.project_id,
                    )
                )
            )
        assert policy is not None
        assert policy.version == 1
        assert audit_actions.count("quota.policy_updated") == 1

        async with seed.factory() as session:
            ledger_before = await session.scalar(
                select(func.count(ProjectUsageLedgerRow.id)).where(
                    ProjectUsageLedgerRow.project_id == seed.owner_a.project_id,
                )
            )

        class FailingAuditSink:
            async def quota_policy_updated(self, *_args, **_kwargs) -> None:
                raise AuditUnavailable()

        failed_app = _test_app(seed, quotas, audit, FailingAuditSink())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=failed_app),
            base_url="http://test",
        ) as client:
            failed = await client.patch(
                f"/api/projects/{seed.owner_a.project_id}/usage/limits",
                headers={"x-test-user": str(seed.owner_a.user_id)},
                json={
                    "expected_version": 1,
                    "limits": {"concurrent_run_limit": 1},
                },
            )
        assert failed.status_code == 503
        assert failed.json()["code"] == "DATABASE_UNAVAILABLE"

        async with seed.factory() as session:
            policy_after = await session.get(ProjectQuotaRow, seed.owner_a.project_id)
            ledger_after = await session.scalar(
                select(func.count(ProjectUsageLedgerRow.id)).where(
                    ProjectUsageLedgerRow.project_id == seed.owner_a.project_id,
                )
            )
        assert policy_after is not None
        assert policy_after.version == 1
        assert policy_after.concurrent_run_limit == 2
        assert ledger_after == ledger_before
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_audit_is_descending_cursor_paginated_scoped_and_privacy_safe(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    keyring = _keyring()
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=keyring)
    audit = AuditService(seed.factory, keyring)
    sink = OperationalAuditSink(
        audit,
        process_context=_bind_gateway_audit_process(audit),
    )
    app = _test_app(seed, quotas, audit, sink)
    old_target = uuid.uuid4()
    new_target = uuid.uuid4()
    other_target = uuid.uuid4()
    now = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            await _append_run_audit(
                audit,
                session,
                seed.owner_a,
                old_target,
                occurred_at=now - timedelta(minutes=1),
            )
            await _append_run_audit(
                audit,
                session,
                seed.owner_a,
                new_target,
                occurred_at=now,
            )
            await _append_run_audit(
                audit,
                session,
                seed.project_b_owner_a,
                other_target,
                occurred_at=now + timedelta(minutes=1),
            )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            first = await client.get(
                f"/api/projects/{seed.owner_a.project_id}/audit?limit=1",
                headers={"x-test-user": str(seed.owner_a.user_id)},
            )
            second = await client.get(
                f"/api/projects/{seed.owner_a.project_id}/audit",
                params={"limit": 1, "cursor": first.json()["next_cursor"]},
                headers={"x-test-user": str(seed.owner_a.user_id)},
            )
            invalid_cursor = await client.get(
                f"/api/projects/{seed.owner_a.project_id}/audit?cursor=not-a-cursor",
                headers={"x-test-user": str(seed.owner_a.user_id)},
            )
            oversized_cursor = await client.get(
                f"/api/projects/{seed.owner_a.project_id}/audit",
                params={"cursor": "a" * 257},
                headers={"x-test-user": str(seed.owner_a.user_id)},
            )
            invalid_limit = await client.get(
                f"/api/projects/{seed.owner_a.project_id}/audit?limit=101",
                headers={"x-test-user": str(seed.owner_a.user_id)},
            )
            hidden = await client.get(
                f"/api/projects/{seed.owner_a.project_id}/audit",
                headers={"x-test-user": str(seed.viewer.user_id)},
            )

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["items"][0]["occurred_at"] > second.json()["items"][0]["occurred_at"]
        assert first.json()["items"][0] == {
            "id": first.json()["items"][0]["id"],
            "occurred_at": first.json()["items"][0]["occurred_at"],
            "actor": "user",
            "action": "run.admitted",
            "target_kind": "run",
            "outcome": "success",
            "public_error_code": None,
            "metadata": {"job_type": "private_run", "non_interactive": False},
        }
        encoded = json.dumps(first.json(), sort_keys=True)
        for private_value in (
            str(new_target),
            str(other_target),
            str(seed.owner_a.user_id),
            "raw-client-trace-must-not-leak",
        ):
            assert private_value not in encoded
        for private_field in (
            "target_ref_hmac",
            "target_ref_key_id",
            "actor_user_id",
            "project_id",
            "request_id",
            "job_id",
            "attempt_id",
            "owner_user_id",
        ):
            assert private_field not in encoded
        assert invalid_cursor.status_code == 400
        assert invalid_cursor.json()["code"] == "INVALID_STREAM_CURSOR"
        assert oversized_cursor.status_code == 422
        assert oversized_cursor.json()["code"] == "RELIABILITY_INVALID"
        assert invalid_limit.status_code == 422
        assert invalid_limit.json()["code"] == "RELIABILITY_INVALID"
        _assert_public_not_found(hidden)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_audit_read_holds_project_and_membership_authority_locks_until_listing_finishes(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    keyring = _keyring()
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=keyring)
    audit = AuditService(seed.factory, keyring)
    list_started = asyncio.Event()
    list_release = asyncio.Event()
    original_list_project = getattr(audit, "list_project", None)

    async def paused_list_project(session, context, **kwargs):
        list_started.set()
        await list_release.wait()
        assert original_list_project is not None
        return await original_list_project(session, context, **kwargs)

    setattr(audit, "list_project", paused_list_project)
    sink = OperationalAuditSink(
        audit,
        process_context=_bind_gateway_audit_process(audit),
    )
    app = _test_app(seed, quotas, audit, sink)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response_task = asyncio.create_task(
                client.get(
                    f"/api/projects/{seed.owner_a.project_id}/audit",
                    headers={"x-test-user": str(seed.owner_a.user_id)},
                )
            )
            await asyncio.wait_for(list_started.wait(), timeout=1)

            for statement in (
                update(ProjectRow).where(ProjectRow.id == seed.owner_a.project_id).values(is_suspended=True),
                update(ProjectMembershipRow).where(ProjectMembershipRow.id == seed.owner_a.membership_id).values(role=ProjectRole.VIEWER.value),
            ):
                async with seed.factory() as competing_session:
                    await competing_session.execute(text("SET lock_timeout = '200ms'"))
                    with pytest.raises(DBAPIError):
                        await competing_session.execute(statement)
                    await competing_session.rollback()

            list_release.set()
            response = await response_task
            assert response.status_code == 200

            async with seed.factory.begin() as session:
                await session.execute(
                    update(ProjectMembershipRow)
                    .where(ProjectMembershipRow.id == seed.owner_a.membership_id)
                    .values(
                        status="removed",
                        version=ProjectMembershipRow.version + 1,
                    )
                )
            hidden = await client.get(
                f"/api/projects/{seed.owner_a.project_id}/audit",
                headers={"x-test-user": str(seed.owner_a.user_id)},
            )
            _assert_public_not_found(hidden)
    finally:
        list_release.set()
        await seed.engine.dispose()
