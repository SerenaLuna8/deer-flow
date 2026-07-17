from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import func, select
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
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.quotas.service import QuotaService
from app.reliability.error_mapping import (
    ReliabilityHTTPException,
    reliability_http_exception_handler,
)
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.quotas.model import ProjectQuotaRow, ProjectUsageLedgerRow


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
        assert invalid_limit.status_code == 422
        assert invalid_limit.json()["code"] == "RELIABILITY_INVALID"
        _assert_public_not_found(hidden)
    finally:
        await seed.engine.dispose()
