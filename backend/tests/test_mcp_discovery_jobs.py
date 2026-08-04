from __future__ import annotations

import uuid

import pytest

from app.shared_assets.mcp_discovery_repository import (
    McpToolDiscoveryAttemptRecord,
    McpToolDiscoveryAttemptRepository,
)
from app.worker.service import JobOutcome, WorkerService
from deerflow.config.worker_config import WorkerConfig
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import EnqueueJob, JobScope
from deerflow.persistence.shared_assets import McpToolDiscoveryAttemptRow


def test_mcp_discovery_job_requires_exact_non_run_owner_authority() -> None:
    scope = JobScope(uuid.uuid4(), str(uuid.uuid4()))
    request = EnqueueJob(
        job_type="mcp_discovery",
        scope=scope,
        idempotency_key="a" * 64,
        run_id=None,
        occurrence_id=None,
        origin_trace_id=None,
        max_attempts=1,
        retry_safety="unsafe",
    )

    assert request.job_type == "mcp_discovery"
    assert request.scope.owner_user_id == scope.owner_user_id
    with pytest.raises(ValueError, match="mcp_discovery"):
        EnqueueJob(
            job_type="mcp_discovery",
            scope=JobScope(scope.project_id, None),
            idempotency_key="b" * 64,
            run_id=None,
            occurrence_id=None,
            max_attempts=1,
            retry_safety="unsafe",
        )
    with pytest.raises(ValueError, match="mcp_discovery"):
        EnqueueJob(
            job_type="mcp_discovery",
            scope=scope,
            idempotency_key="c" * 64,
            run_id="forged-run",
            occurrence_id=None,
            max_attempts=1,
            retry_safety="unsafe",
        )


def test_mcp_discovery_attempt_orm_is_one_to_one_and_closed() -> None:
    table = McpToolDiscoveryAttemptRow.__table__

    assert table.c.job_id.primary_key is True
    assert table.c.job_id.foreign_keys
    assert {column.name for column in table.columns} == {
        "job_id",
        "project_id",
        "mcp_server_id",
        "mcp_server_version_id",
        "requested_by_user_id",
        "trigger",
        "payload_checksum",
        "grant_digest",
        "result_status",
        "public_error_code",
        "requested_at",
        "revision",
    }
    constraint_sql = " ".join(str(constraint.sqltext) for constraint in table.constraints if hasattr(constraint, "sqltext"))
    assert "'auto'" in constraint_sql and "'manual'" in constraint_sql
    assert "'succeeded'" in constraint_sql
    assert "'failed'" in constraint_sql
    assert "'cancelled'" in constraint_sql
    assert "mcp_discovery_unavailable" in constraint_sql
    assert "mcp_catalog_invalid" in constraint_sql

    jobs_constraint_sql = " ".join(str(constraint.sqltext) for constraint in JobRow.__table__.constraints if hasattr(constraint, "sqltext"))
    assert "mcp_discovery" in jobs_constraint_sql


def test_mcp_discovery_repository_public_contract_is_stable() -> None:
    assert McpToolDiscoveryAttemptRepository.enqueue
    assert McpToolDiscoveryAttemptRepository.get
    assert McpToolDiscoveryAttemptRepository.latest_for_version
    assert McpToolDiscoveryAttemptRepository.active_for_closure
    assert McpToolDiscoveryAttemptRepository.mark_result
    assert tuple(McpToolDiscoveryAttemptRecord.__dataclass_fields__) == (
        "attempt_id",
        "project_id",
        "mcp_server_id",
        "mcp_server_version_id",
        "requested_by_user_id",
        "trigger",
        "payload_checksum",
        "grant_digest",
        "status",
        "requested_at",
        "started_at",
        "completed_at",
        "public_error_code",
        "revision",
    )


def test_worker_service_accepts_mcp_discovery_handler_type() -> None:
    async def handler(_claim, _authority):
        return JobOutcome.succeeded()

    service = WorkerService(
        lambda: None,
        object(),
        {"mcp_discovery": handler},
        WorkerConfig(),
    )

    assert set(service._handlers) == {"mcp_discovery"}
