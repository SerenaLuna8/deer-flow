from __future__ import annotations

from importlib.util import find_spec
from types import MappingProxyType

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.final_schema import FinalSchemaRequired, FinalSchemaState, FinalSchemaUnavailable
from app.reliability.cutover import ReliabilityCutoverGuard
from app.reliability.error_mapping import (
    ReliabilityHTTPException,
    reliability_http_exception,
    reliability_http_exception_handler,
)
from app.reliability.errors import (
    ReliabilityConflict,
    ReliabilityCutover,
    ReliabilityDatabaseUnavailable,
    ReliabilityError,
    ReliabilityForbidden,
    ReliabilityInvalid,
    ReliabilityInvalidStreamCursor,
    ReliabilityMigrationRequired,
    ReliabilityNotFound,
    ReliabilityQuotaExceeded,
    ReliabilityWorkerUnavailable,
)
from app.reliability.process_readiness import ProcessReadinessSnapshot
from app.reliability.readiness import ReliabilityReadinessService
from deerflow.persistence.revisions import RevisionAncestry


def test_m6_reliability_contract_modules_exist() -> None:
    for module in (
        "app.reliability.models",
        "app.reliability.errors",
        "app.reliability.error_mapping",
        "app.reliability.cutover",
        "app.reliability.readiness",
    ):
        assert find_spec(module) is not None


def test_reliability_errors_have_exact_safe_http_mapping() -> None:
    cases = (
        (ReliabilityInvalidStreamCursor, 400),
        (ReliabilityNotFound, 404),
        (ReliabilityForbidden, 403),
        (ReliabilityConflict, 409),
        (ReliabilityInvalid, 422),
        (ReliabilityMigrationRequired, 409),
        (ReliabilityQuotaExceeded, 429),
        (ReliabilityCutover, 503),
        (ReliabilityWorkerUnavailable, 503),
        (ReliabilityDatabaseUnavailable, 503),
    )
    for error_type, expected_status in cases:
        error = error_type("request-1")
        mapped = reliability_http_exception(error)
        assert mapped.status_code == expected_status
        assert mapped.detail == {
            "code": error_type.code,
            "message": error_type.public_message,
            "request_id": "request-1",
        }
        if expected_status in {429, 503}:
            assert mapped.headers == {"Retry-After": "1"}
        else:
            assert mapped.headers is None

    class UnapprovedReliabilityError(ReliabilityError):
        pass

    with pytest.raises(TypeError, match="unsupported reliability error"):
        reliability_http_exception(UnapprovedReliabilityError("request-2"))


@pytest.mark.asyncio
async def test_reliability_http_handler_serializes_top_level_contract_only() -> None:
    app = FastAPI()
    app.add_exception_handler(ReliabilityHTTPException, reliability_http_exception_handler)

    @app.get("/failure")
    async def failure() -> None:
        raise reliability_http_exception(ReliabilityWorkerUnavailable("request-wire"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/failure")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json() == {
        "code": "WORKER_UNAVAILABLE",
        "message": "No Worker is currently available.",
        "request_id": "request-wire",
    }


def test_gateway_registers_the_reliability_http_handler() -> None:
    from app.gateway.app import create_app

    app = create_app()
    assert app.exception_handlers[ReliabilityHTTPException] is reliability_http_exception_handler


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_fresh_install_opens_m6_paths_and_closes_legacy_execution(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        guard = ReliabilityCutoverGuard(factory, request_id="fresh")
        await guard.require_queue_open()
        await guard.require_gateway_open()
        await guard.require_worker_open()
        with pytest.raises(ReliabilityCutover) as caught:
            await guard.require_legacy_execution_open()
        assert caught.value.request_id == "fresh"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_guard_reads_all_markers_each_time_and_fails_closed(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    guard = ReliabilityCutoverGuard(factory, request_id="marker")
    try:
        await guard.require_gateway_open()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE reliability_cutover_state
                    SET stage='migration_ready',cutover_at=NULL,updated_at=now()
                    WHERE id=1"""
                )
            )
        for require_open in (
            guard.require_queue_open,
            guard.require_gateway_open,
            guard.require_worker_open,
        ):
            with pytest.raises(ReliabilityCutover):
                await require_open()
        await guard.require_legacy_execution_open()

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE automation_cutover_state
                    SET stage='empty_install',final_schema_probe_complete=false,
                        cutover_at=NULL,updated_at=now()
                    WHERE id=1"""
                )
            )
        with pytest.raises(ReliabilityCutover):
            await guard.require_gateway_open()
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_guard_requires_final_revision_and_accepts_descendant(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE alembic_version SET version_num='0014_project_reliability_expand'"))
        with pytest.raises(ReliabilityCutover):
            await ReliabilityCutoverGuard(factory, request_id="expand").require_gateway_open()

        revisions = RevisionAncestry(
            MappingProxyType(
                {
                    "0016_future": frozenset(
                        {
                            "0016_future",
                            "0015_project_reliability_finalize",
                        }
                    )
                }
            )
        )
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE alembic_version SET version_num='0016_future'"))
        await ReliabilityCutoverGuard(
            factory,
            request_id="future",
            revisions=revisions,
        ).require_worker_open()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_guard_maps_database_errors_without_leaking_details() -> None:
    class UnavailableSession:
        async def scalar(self, *_args, **_kwargs):
            raise SQLAlchemyError("postgresql://secret@database/private")

    guard = ReliabilityCutoverGuard.for_session(
        UnavailableSession(),  # type: ignore[arg-type]
        request_id="safe-request",
    )
    with pytest.raises(ReliabilityDatabaseUnavailable) as captured:
        await guard.require_gateway_open()
    assert captured.value.request_id == "safe-request"
    assert "secret" not in str(captured.value)


class _OpenGuard:
    async def require_ready(self, _session) -> None:
        return None


@pytest.mark.asyncio
async def test_readiness_reports_each_component_and_degrades_independently() -> None:
    providers = {
        "worker_fleet": lambda: "ready",
        "scheduler": lambda: "disabled",
        "stream": lambda: "ready",
        "recovery": lambda: "ready",
        "quota": lambda: "ready",
        "audit": lambda: "ready",
    }
    ready = await ReliabilityReadinessService(_OpenGuard(), object(), "ready-request", **providers).read()
    assert ready.status == "ready"
    assert ready.database == ready.schema == "ready"
    assert ready.scheduler == "disabled"
    assert ready.request_id == "ready-request"

    providers["stream"] = lambda: "polling"
    degraded = await ReliabilityReadinessService(_OpenGuard(), object(), "ready-request", **providers).read()
    assert degraded.status == "degraded"
    assert degraded.stream == "polling"
    assert degraded.worker_fleet == "ready"


@pytest.mark.asyncio
async def test_readiness_closes_on_schema_or_database_failure() -> None:
    class ClosedGuard(_OpenGuard):
        async def require_ready(self, _session) -> None:
            raise FinalSchemaRequired(FinalSchemaState("old-revision", (), False))

    closed = await ReliabilityReadinessService(ClosedGuard(), object(), "schema-request").read()
    assert closed.status == "closed"
    assert closed.database == "ready"
    assert closed.schema == "unavailable"

    class UnavailableGuard(_OpenGuard):
        async def require_ready(self, _session) -> None:
            raise FinalSchemaUnavailable()

    unavailable = await ReliabilityReadinessService(UnavailableGuard(), object(), "db-request").read()
    assert unavailable.status == "closed"
    assert unavailable.database == "unavailable"
    assert unavailable.schema == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scheduler", "ownership"),
    (
        ("disabled", "disabled"),
        ("unavailable", "unowned"),
        ("unavailable", "ownership_lost"),
    ),
)
async def test_closed_readiness_preserves_public_process_snapshot_without_opening_business_domains(
    scheduler: str,
    ownership: str,
) -> None:
    class ClosedGuard(_OpenGuard):
        async def require_ready(self, _session) -> None:
            raise FinalSchemaRequired(FinalSchemaState("old-revision", (), False))

    process = ProcessReadinessSnapshot(
        ready=False,
        role="gateway",
        worker_fleet="ready",
        worker_count=2,
        worker_capacity=6,
        worker_oldest_heartbeat_age_seconds=4,
        scheduler=scheduler,
        scheduler_ownership=ownership,
        schema_state="unavailable",
    )

    closed = await ReliabilityReadinessService(ClosedGuard(), object(), "schema-request", process=process).read()

    assert closed.status == "closed"
    assert closed.database == "ready"
    assert closed.schema == "unavailable"
    assert closed.role == "gateway"
    assert closed.worker_fleet == "ready"
    assert closed.worker_count == 2
    assert closed.worker_capacity == 6
    assert closed.worker_oldest_heartbeat_age_seconds == 4
    assert closed.scheduler == scheduler
    assert closed.scheduler_ownership == ownership
    assert closed.schema_state == "unavailable"
    assert (closed.stream, closed.recovery, closed.quota, closed.audit) == (
        "closed",
        "closed",
        "closed",
        "closed",
    )
    serialized = str(closed).lower()
    for forbidden in ("pid", "hostname", "lock_key", "postgresql://", "token"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_readiness_preserves_the_explicit_failure_request_id() -> None:
    class RequiredProbe:
        async def require_ready(self, _session) -> None:
            raise FinalSchemaRequired(FinalSchemaState("old-revision", (), False))

    readiness = await ReliabilityReadinessService(RequiredProbe(), object(), "failure-request").read()
    assert readiness.request_id == "failure-request"
