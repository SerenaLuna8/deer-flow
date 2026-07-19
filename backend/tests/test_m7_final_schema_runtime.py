from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.final_schema import (
    M7_FINAL_SCHEMA_REVISION,
    FinalSchemaProbe,
    FinalSchemaRequired,
    FinalSchemaState,
    FinalSchemaUnavailable,
)
from app.gateway.automation_schemas import AutomationReadinessResponse
from app.gateway.routers.admin_operations import OperationsReadinessResponse
from app.reliability.models import ReliabilityReadiness
from app.scheduler import app as scheduler_app
from app.worker import app as worker_app


@pytest.mark.asyncio
async def test_final_schema_probe_ignores_cutover_markers() -> None:
    session = SimpleNamespace(
        scalar=AsyncMock(
            side_effect=[
                M7_FINAL_SCHEMA_REVISION,
                ("jobs", "projects", "run_events"),
            ]
        )
    )

    state = await FinalSchemaProbe(
        accepted_revisions=(M7_FINAL_SCHEMA_REVISION,),
        required_relations=("projects", "jobs", "run_events"),
    ).read(session)

    assert state == FinalSchemaState(
        revision=M7_FINAL_SCHEMA_REVISION,
        missing_relations=(),
        ready=True,
    )
    sql = " ".join(str(call.args[0]) for call in session.scalar.await_args_list)
    assert "cutover_state" not in sql
    assert "owner" not in sql
    assert "project_id" not in sql


@pytest.mark.asyncio
async def test_final_schema_probe_reports_wrong_revision_and_missing_relations() -> None:
    session = SimpleNamespace(
        scalar=AsyncMock(
            side_effect=[
                "0014_project_reliability_expand",
                ("projects", "run_events"),
            ]
        )
    )

    state = await FinalSchemaProbe(
        accepted_revisions=(M7_FINAL_SCHEMA_REVISION,),
        required_relations=("projects", "jobs", "run_events"),
    ).read(session)

    assert state == FinalSchemaState(
        revision="0014_project_reliability_expand",
        missing_relations=("jobs",),
        ready=False,
    )


@pytest.mark.asyncio
async def test_final_schema_probe_maps_only_database_failures_to_unavailable() -> None:
    unavailable = SimpleNamespace(scalar=AsyncMock(side_effect=SQLAlchemyError("postgresql://secret@db/private")))
    with pytest.raises(FinalSchemaUnavailable) as captured:
        await FinalSchemaProbe().require_ready(unavailable)
    assert "secret" not in str(captured.value)

    wrong_revision = SimpleNamespace(scalar=AsyncMock(side_effect=["0014_project_reliability_expand", ()]))
    with pytest.raises(FinalSchemaRequired) as captured:
        await FinalSchemaProbe().require_ready(wrong_revision)
    assert captured.value.state.revision == "0014_project_reliability_expand"


def test_public_readiness_contract_has_no_cutover_field() -> None:
    automation_fields = AutomationReadinessResponse.model_fields
    reliability_fields = {field.name for field in fields(ReliabilityReadiness)}

    assert "schema_ready" in automation_fields
    assert "automation_cutover_ready" not in automation_fields
    assert "schema_state" in reliability_fields
    assert "cutover" not in reliability_fields

    common = {
        "status": "closed",
        "database": "ready",
        "schema": "unavailable",
        "worker_fleet": "unavailable",
        "scheduler": "disabled",
        "stream": "closed",
        "recovery": "closed",
        "quota": "closed",
        "audit": "closed",
        "role": "gateway",
        "worker_count": 0,
        "worker_capacity": 0,
        "worker_oldest_heartbeat_age_seconds": None,
        "scheduler_ownership": "disabled",
    }
    assert OperationsReadinessResponse(**common, schema_state="unavailable").schema_state == "unavailable"
    with pytest.raises(ValueError):
        OperationsReadinessResponse(**common, schema_state="migration_required")


@asynccontextmanager
async def _session_context():
    yield object()


def _session_factory():
    return _session_context()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", (worker_app, scheduler_app))
async def test_worker_and_scheduler_fail_fast_before_runtime_services(
    monkeypatch: pytest.MonkeyPatch,
    target,
) -> None:
    started: list[str] = []
    config = SimpleNamespace(
        worker=SimpleNamespace(enabled=True),
        scheduler=SimpleNamespace(enabled=True),
        database=object(),
    )

    async def init_engine(_database) -> None:
        started.append("engine")

    async def close_engine() -> None:
        started.append("closed")

    async def require_ready(_self, _session) -> None:
        raise FinalSchemaRequired(
            FinalSchemaState(
                revision="0014_project_reliability_expand",
                missing_relations=("jobs",),
                ready=False,
            )
        )

    monkeypatch.setattr(target, "get_app_config", lambda: config)
    monkeypatch.setattr(target, "init_engine", init_engine)
    monkeypatch.setattr(target, "close_engine", close_engine)
    monkeypatch.setattr(target, "get_session_factory", lambda: _session_factory)
    monkeypatch.setattr(target.FinalSchemaProbe, "require_ready", require_ready)

    runner = target.run_worker if target is worker_app else target.run_scheduler
    with pytest.raises(FinalSchemaRequired):
        await runner()

    assert started == ["engine", "closed"]
