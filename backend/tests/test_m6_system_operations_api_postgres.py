from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from support.m4_private_threads import seed_m4_thread_database

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditAuthorityRejected,
    AuditOutcome,
    AuditTarget,
    AuditTargetKind,
    AuditUnavailable,
    resolve_system_audit_context,
)
from app.audit.service import AuditService
from app.gateway.deps import (
    get_current_user_from_request,
    get_project_audit_service,
    project_session,
)
from app.gateway.routers import (
    admin_audit,
    admin_jobs,
    admin_operations,
    admin_projects,
)
from app.reliability.error_mapping import (
    ReliabilityHTTPException,
    reliability_http_exception_handler,
)
from app.reliability.models import ReliabilityReadiness
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.jobs.model import DeadJobRow, JobRow
from deerflow.persistence.quotas.model import ProjectUsageCounterRow
from deerflow.persistence.user.model import UserRow

NOW = datetime(2026, 7, 17, 6, tzinfo=UTC)


def _keyring() -> AuditHmacKeyring:
    return AuditHmacKeyring(
        active_key_id="operations-v1",
        _keys={"operations-v1": b"o" * 32},
    )


async def _make_system_admin(seed) -> None:
    async with seed.factory.begin() as session:
        await session.execute(update(UserRow).where(UserRow.id == str(seed.owner_a.user_id)).values(system_role="system_admin"))


def _test_app(
    seed,
    audit: object,
    *,
    readiness: object | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(
        ReliabilityHTTPException,
        reliability_http_exception_handler,
    )
    app.include_router(admin_operations.router)
    app.include_router(admin_projects.router)
    app.include_router(admin_jobs.router)
    app.include_router(admin_audit.router)
    app.state.project_audit_service = audit
    app.state.admin_operations_session_factory = seed.factory
    if readiness is not None:
        app.state.reliability_readiness_service = readiness

    async def request_session():
        async with seed.factory() as session:
            yield session

    async def current_user(request: Request):
        raw = request.headers.get("x-test-user")
        if raw is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "NOT_AUTHENTICATED", "message": "Not authenticated"},
            )
        # Deliberately claim system_admin for every authenticated identity. The
        # operations routes must ignore this stale/untrusted role and lock the
        # current users row in their own transaction.
        return SimpleNamespace(
            id=uuid.UUID(raw),
            system_role="system_admin",
        )

    app.dependency_overrides[project_session] = request_session
    app.dependency_overrides[get_current_user_from_request] = current_user
    app.dependency_overrides[get_project_audit_service] = lambda: audit
    return app


async def _seed_dead_job(
    seed,
    *,
    retry_safety: str,
    public_error_code: str,
    dead_at: datetime,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            JobRow(
                id=job_id,
                job_type="retention_purge",
                project_id=seed.owner_a.project_id,
                owner_user_id=None,
                run_id=None,
                automation_occurrence_id=None,
                predecessor_dead_job_id=None,
                idempotency_key=uuid.uuid4().hex * 2,
                status="dead",
                attempt_count=1,
                max_attempts=1,
                retry_safety=retry_safety,
                public_error_code=public_error_code,
                created_at=dead_at - timedelta(minutes=2),
                started_at=dead_at - timedelta(minutes=1),
                completed_at=dead_at,
                updated_at=dead_at,
            )
        )
        await session.flush()
        session.add(
            DeadJobRow(
                job_id=job_id,
                project_id=seed.owner_a.project_id,
                owner_ref_key_id=None,
                owner_ref_hmac=None,
                job_type="retention_purge",
                attempt_count=1,
                retry_safety=retry_safety,
                public_error_code=public_error_code,
                dead_at=dead_at,
            )
        )
    return job_id


def _assert_public_not_found(response: httpx.Response) -> None:
    assert response.status_code == 404
    assert response.json() == {
        "code": "RELIABILITY_NOT_FOUND",
        "message": "Reliability resource was not found.",
        "request_id": "operations-api-test",
    }


def test_system_operations_routes_are_mounted() -> None:
    from app.gateway.app import app

    paths = {route.path for route in app.routes}
    assert "/api/admin/operations" in paths
    assert "/api/admin/projects" in paths
    assert "/api/admin/jobs" in paths
    assert "/api/admin/jobs/requeue" in paths
    assert "/api/admin/audit" in paths


@pytest.mark.postgres
@pytest.mark.anyio
async def test_operations_requires_current_system_admin_and_returns_only_aggregate_public_health(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    await _make_system_admin(seed)
    audit = AuditService(seed.factory, _keyring())
    app = _test_app(seed, audit)
    try:
        async with seed.factory() as session, session.begin():
            session.add(
                ProjectUsageCounterRow(
                    project_id=seed.owner_a.project_id,
                    dimension="storage_bytes",
                    bucket="lifetime",
                    used=123,
                    reserved=7,
                )
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/admin/operations",
                headers={
                    "x-test-user": str(seed.owner_a.user_id),
                    "x-trace-id": "operations-api-test",
                },
            )
            ordinary = await client.get(
                "/api/admin/operations",
                headers={
                    "x-test-user": str(seed.owner_b.user_id),
                    "x-trace-id": "operations-api-test",
                },
            )
            unauthenticated = await client.get("/api/admin/operations")

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"readiness", "counts", "usage"}
        assert body["readiness"] == {
            "status": "degraded",
            "database": "ready",
            "schema": "ready",
            "worker_fleet": "unavailable",
            "scheduler": "disabled",
            "stream": "unavailable",
            "recovery": "unavailable",
            "quota": "unavailable",
            "audit": "unavailable",
        }
        assert set(body["counts"]) == {
            "projects",
            "suspended_projects",
            "queued_jobs",
            "running_jobs",
            "dead_jobs",
        }
        usage = {item["dimension"]: item for item in body["usage"]}
        assert set(usage) == {
            "members",
            "storage_bytes",
            "concurrent_runs",
            "mcp_calls_daily",
        }
        assert usage["storage_bytes"] == {
            "dimension": "storage_bytes",
            "used": 123,
            "reserved": 7,
        }
        encoded = json.dumps(body, sort_keys=True)
        for private_field in (
            "slug",
            "display_name",
            "description",
            "icon",
            "owner_user_id",
            "created_by_user_id",
            "prompt",
            "message",
        ):
            assert private_field not in encoded
        _assert_public_not_found(ordinary)
        assert unauthenticated.status_code == 401
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_projects_are_descending_cursor_paginated_and_private_fields_are_never_selected(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    await _make_system_admin(seed)
    audit = AuditService(seed.factory, _keyring())
    app = _test_app(seed, audit)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            first = await client.get(
                "/api/admin/projects",
                params={"limit": 1, "status": "active", "suspended": "false"},
                headers={
                    "x-test-user": str(seed.owner_a.user_id),
                    "x-trace-id": "operations-api-test",
                },
            )
            second = await client.get(
                "/api/admin/projects",
                params={"limit": 1, "cursor": first.json()["next_cursor"]},
                headers={
                    "x-test-user": str(seed.owner_a.user_id),
                    "x-trace-id": "operations-api-test",
                },
            )
            oversized = await client.get(
                "/api/admin/projects",
                params={"cursor": "x" * 257},
                headers={
                    "x-test-user": str(seed.owner_a.user_id),
                    "x-trace-id": "operations-api-test",
                },
            )

        assert first.status_code == 200
        assert second.status_code == 200
        assert set(first.json()) == {"items", "next_cursor"}
        assert set(first.json()["items"][0]) == {
            "project_id",
            "status",
            "is_suspended",
        }
        assert first.json()["items"][0]["project_id"] != second.json()["items"][0]["project_id"]
        encoded = json.dumps((first.json(), second.json()), sort_keys=True)
        for private_field in (
            "slug",
            "display_name",
            "description",
            "icon",
            "created_by_user_id",
            "membership",
            "owner",
        ):
            assert private_field not in encoded
        assert oversized.status_code == 422
        assert oversized.json()["code"] == "RELIABILITY_INVALID"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_jobs_are_public_only_and_safe_requeue_is_atomic_and_project_exact(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    await _make_system_admin(seed)
    safe_dead = await _seed_dead_job(
        seed,
        retry_safety="safe",
        public_error_code="PURGE_FAILED",
        dead_at=NOW,
    )
    unsafe_dead = await _seed_dead_job(
        seed,
        retry_safety="unknown",
        public_error_code="SIDE_EFFECT_STATE_UNKNOWN",
        dead_at=NOW - timedelta(minutes=1),
    )
    audit = AuditService(seed.factory, _keyring())
    app = _test_app(seed, audit)
    headers = {
        "x-test-user": str(seed.owner_a.user_id),
        "x-trace-id": "operations-api-test",
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            listed = await client.get(
                "/api/admin/jobs",
                params={
                    "project_id": str(seed.owner_a.project_id),
                    "status": "dead",
                    "type": "retention_purge",
                },
                headers=headers,
            )
            unsafe = await client.post(
                "/api/admin/jobs/requeue",
                headers=headers,
                json={
                    "project_id": str(seed.owner_a.project_id),
                    "dead_job_id": str(unsafe_dead),
                    "idempotency_key": "a" * 64,
                    "max_attempts": 3,
                },
            )
            cross_project = await client.post(
                "/api/admin/jobs/requeue",
                headers=headers,
                json={
                    "project_id": str(seed.project_b_owner_a.project_id),
                    "dead_job_id": str(safe_dead),
                    "idempotency_key": "b" * 64,
                    "max_attempts": 3,
                },
            )
            requeued = await client.post(
                "/api/admin/jobs/requeue",
                headers=headers,
                json={
                    "project_id": str(seed.owner_a.project_id),
                    "dead_job_id": str(safe_dead),
                    "idempotency_key": "c" * 64,
                    "max_attempts": 3,
                },
            )

        assert listed.status_code == 200
        items = listed.json()["items"]
        assert set(items[0]) == {
            "job_id",
            "dead_job_id",
            "project_id",
            "job_type",
            "status",
            "retry_safety",
            "safe_to_requeue",
            "public_error_code",
            "predecessor_dead_job_id",
        }
        encoded = json.dumps(listed.json(), sort_keys=True)
        for forbidden in (
            "owner_user_id",
            "run_id",
            "automation_occurrence_id",
            "thread_id",
            "idempotency_key",
            "lease_owner_id",
            "lease_token_hash",
            "payload",
            "prompt",
            "message",
            "exception",
        ):
            assert forbidden not in encoded
        assert unsafe.status_code == 404, unsafe.text
        _assert_public_not_found(unsafe)
        _assert_public_not_found(cross_project)
        assert requeued.status_code == 201
        assert requeued.json() == {
            "job_id": requeued.json()["job_id"],
            "project_id": str(seed.owner_a.project_id),
            "status": "queued",
            "retry_safety": "safe",
            "attempt_count": 0,
            "predecessor_dead_job_id": str(safe_dead),
        }

        successor_id = uuid.UUID(requeued.json()["job_id"])
        async with seed.factory() as session:
            successor = await session.get(JobRow, successor_id)
            audit_rows = tuple(
                await session.scalars(
                    select(AuditLogRow).where(
                        AuditLogRow.action == "job.requeued",
                    )
                )
            )
        assert successor is not None
        assert successor.predecessor_dead_job_id == safe_dead
        assert len(audit_rows) == 1
        assert audit_rows[0].job_id == successor_id
        assert audit_rows[0].metadata_json == {
            "job_type": "retention_purge",
            "attempt_count": 0,
            "retry_safety": "safe",
        }

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            after_requeue = await client.get(
                "/api/admin/jobs",
                params={
                    "project_id": str(seed.owner_a.project_id),
                    "status": "dead",
                    "type": "retention_purge",
                },
                headers=headers,
            )
        predecessor = next(item for item in after_requeue.json()["items"] if item["job_id"] == str(safe_dead))
        assert predecessor["safe_to_requeue"] is False
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_malformed_admin_requests_revalidate_current_role_before_returning_validation_errors(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    await _make_system_admin(seed)
    app = _test_app(seed, AuditService(seed.factory, _keyring()))
    ordinary_headers = {
        "x-test-user": str(seed.owner_b.user_id),
        "x-trace-id": "operations-api-test",
    }
    admin_headers = {
        "x-test-user": str(seed.owner_a.user_id),
        "x-trace-id": "operations-api-test",
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            unauthenticated = await client.get(
                "/api/admin/projects",
                params={"cursor": "x" * 257},
            )
            ordinary_query = await client.get(
                "/api/admin/jobs",
                params={"cursor": "x" * 257},
                headers=ordinary_headers,
            )
            ordinary_cursor = await client.get(
                "/api/admin/audit",
                params={"cursor": "x" * 257},
                headers=ordinary_headers,
            )
            ordinary_body = await client.post(
                "/api/admin/jobs/requeue",
                headers=ordinary_headers,
                json={
                    "project_id": "not-a-uuid",
                    "dead_job_id": "not-a-uuid",
                    "idempotency_key": "invalid",
                    "max_attempts": 0,
                },
            )
            admin = await client.get(
                "/api/admin/projects",
                params={"cursor": "x" * 257},
                headers=admin_headers,
            )

        assert unauthenticated.status_code == 401
        for response in (ordinary_query, ordinary_cursor, ordinary_body):
            _assert_public_not_found(response)
        assert admin.status_code == 422
        assert admin.json()["code"] == "RELIABILITY_INVALID"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_operations_overview_serializes_injected_closed_component_readiness(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    await _make_system_admin(seed)

    class ClosedReadiness:
        async def read(self) -> ReliabilityReadiness:
            return ReliabilityReadiness(
                status="closed",
                database="unavailable",
                schema="unknown",
                worker_fleet="closed",
                scheduler="closed",
                stream="closed",
                recovery="closed",
                quota="closed",
                audit="closed",
                request_id="operations-api-test",
            )

    app = _test_app(
        seed,
        AuditService(seed.factory, _keyring()),
        readiness=ClosedReadiness(),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/admin/operations",
                headers={
                    "x-test-user": str(seed.owner_a.user_id),
                    "x-trace-id": "operations-api-test",
                },
            )

        assert response.status_code == 200
        assert response.json()["readiness"] == {
            "status": "closed",
            "database": "unavailable",
            "schema": "unknown",
            "worker_fleet": "closed",
            "scheduler": "closed",
            "stream": "closed",
            "recovery": "closed",
            "quota": "closed",
            "audit": "closed",
        }
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_requeue_validation_and_audit_failure_leave_no_successor_or_audit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    await _make_system_admin(seed)
    safe_dead = await _seed_dead_job(
        seed,
        retry_safety="safe",
        public_error_code="PURGE_FAILED",
        dead_at=NOW,
    )
    audit = AuditService(seed.factory, _keyring())
    original_append = audit.append

    async def failing_append(*_args, **_kwargs):
        raise AuditUnavailable()

    audit.append = failing_append  # type: ignore[method-assign]
    app = _test_app(seed, audit)
    headers = {
        "x-test-user": str(seed.owner_a.user_id),
        "x-trace-id": "operations-api-test",
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            invalid = await client.post(
                "/api/admin/jobs/requeue",
                headers=headers,
                json={
                    "project_id": str(seed.owner_a.project_id),
                    "dead_job_id": str(safe_dead),
                    "idempotency_key": "not-a-sha256",
                    "max_attempts": "3",
                },
            )
            failed = await client.post(
                "/api/admin/jobs/requeue",
                headers=headers,
                json={
                    "project_id": str(seed.owner_a.project_id),
                    "dead_job_id": str(safe_dead),
                    "idempotency_key": "d" * 64,
                    "max_attempts": 3,
                },
            )

        assert invalid.status_code == 422
        assert invalid.json()["code"] == "RELIABILITY_INVALID"
        assert failed.status_code == 503, failed.text
        assert failed.json()["code"] == "DATABASE_UNAVAILABLE"
        async with seed.factory() as session:
            successor_count = await session.scalar(
                select(func.count(JobRow.id)).where(
                    JobRow.predecessor_dead_job_id == safe_dead,
                )
            )
            audit_count = await session.scalar(
                select(func.count(AuditLogRow.id)).where(
                    AuditLogRow.action == "job.requeued",
                )
            )
        assert successor_count == 0
        assert audit_count == 0
    finally:
        audit.append = original_append  # type: ignore[method-assign]
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_platform_audit_revalidates_current_role_and_holds_its_lock_through_listing(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    await _make_system_admin(seed)
    audit = AuditService(seed.factory, _keyring())
    context = resolve_system_audit_context(
        SimpleNamespace(
            id=seed.owner_a.user_id,
            system_role="system_admin",
        ),
        request_id="operations-api-test",
    )
    try:
        async with seed.factory.begin() as session:
            await session.execute(update(UserRow).where(UserRow.id == str(seed.owner_a.user_id)).values(system_role="user"))
        async with seed.factory() as session, session.begin():
            with pytest.raises(AuditAuthorityRejected):
                await audit.list_platform(session, context)

        await _make_system_admin(seed)
        list_started = asyncio.Event()
        list_release = asyncio.Event()
        original_list_platform = audit.list_platform

        async def paused_list_platform(session, current_context, **kwargs):
            list_started.set()
            await list_release.wait()
            return await original_list_platform(session, current_context, **kwargs)

        audit.list_platform = paused_list_platform  # type: ignore[method-assign]
        app = _test_app(seed, audit)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response_task = asyncio.create_task(
                client.get(
                    "/api/admin/audit",
                    headers={
                        "x-test-user": str(seed.owner_a.user_id),
                        "x-trace-id": "operations-api-test",
                    },
                )
            )
            await asyncio.wait_for(list_started.wait(), timeout=1)
            async with seed.factory() as competing_session:
                await competing_session.execute(text("SET lock_timeout = '200ms'"))
                with pytest.raises(DBAPIError):
                    await competing_session.execute(update(UserRow).where(UserRow.id == str(seed.owner_a.user_id)).values(system_role="user"))
                await competing_session.rollback()
            list_release.set()
            response = await response_task
            assert response.status_code == 200

        async with seed.factory.begin() as session:
            await session.execute(update(UserRow).where(UserRow.id == str(seed.owner_a.user_id)).values(system_role="user"))
        app = _test_app(seed, audit)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            hidden = await client.get(
                "/api/admin/audit",
                headers={
                    "x-test-user": str(seed.owner_a.user_id),
                    "x-trace-id": "operations-api-test",
                },
            )
        _assert_public_not_found(hidden)
    finally:
        if "list_release" in locals():
            list_release.set()
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_platform_audit_is_descending_cursor_paginated_and_strictly_public(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    await _make_system_admin(seed)
    audit = AuditService(seed.factory, _keyring())
    app = _test_app(seed, audit)
    now = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            for occurred_at in (now - timedelta(minutes=1), now):
                await audit.append(
                    session,
                    AuditActor.user(seed.owner_a.user_id),
                    AuditAction.RUN_ADMITTED,
                    AuditTarget(
                        AuditTargetKind.RUN,
                        uuid.uuid4(),
                        seed.owner_a.project_id,
                    ),
                    AuditOutcome.SUCCESS,
                    {"job_type": "private_run", "non_interactive": False},
                    request_id="raw-trace-must-not-leak",
                    occurred_at=occurred_at,
                )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            first = await client.get(
                "/api/admin/audit",
                params={"limit": 1},
                headers={
                    "x-test-user": str(seed.owner_a.user_id),
                    "x-trace-id": "operations-api-test",
                },
            )
            second = await client.get(
                "/api/admin/audit",
                params={"limit": 1, "cursor": first.json()["next_cursor"]},
                headers={
                    "x-test-user": str(seed.owner_a.user_id),
                    "x-trace-id": "operations-api-test",
                },
            )

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["items"][0]["occurred_at"] > second.json()["items"][0]["occurred_at"]
        assert set(first.json()["items"][0]) == {
            "id",
            "occurred_at",
            "actor",
            "action",
            "target_kind",
            "outcome",
            "public_error_code",
            "metadata",
        }
        encoded = json.dumps((first.json(), second.json()), sort_keys=True)
        for private_field in (
            "actor_user_id",
            "project_id",
            "target_ref_hmac",
            "target_ref_key_id",
            "job_id",
            "attempt_id",
            "request_id",
            "owner_user_id",
            "raw-trace-must-not-leak",
        ):
            assert private_field not in encoded
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_platform_audit_rejects_persisted_unknown_metadata_before_serialization(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    await _make_system_admin(seed)
    app = _test_app(seed, AuditService(seed.factory, _keyring()))
    try:
        async with seed.factory() as session, session.begin():
            session.add(
                AuditLogRow(
                    id=uuid.uuid4(),
                    occurred_at=datetime.now(UTC),
                    actor_user_id=str(seed.owner_a.user_id),
                    actor_process=None,
                    actor_platform_role=None,
                    project_id=seed.owner_a.project_id,
                    action="job.requeued",
                    target_kind="job",
                    target_ref_key_id="operations-v1",
                    target_ref_hmac="f" * 64,
                    outcome="success",
                    public_error_code=None,
                    request_id=None,
                    job_id=None,
                    attempt_id=None,
                    metadata_json={
                        "job_type": "retention_purge",
                        "attempt_count": 0,
                        "retry_safety": "safe",
                        "owner_user_id": str(seed.owner_a.user_id),
                    },
                )
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/admin/audit",
                headers={
                    "x-test-user": str(seed.owner_a.user_id),
                    "x-trace-id": "operations-api-test",
                },
            )

        assert response.status_code == 422
        assert response.json()["code"] == "RELIABILITY_INVALID"
        assert "owner_user_id" not in response.text
    finally:
        await seed.engine.dispose()
