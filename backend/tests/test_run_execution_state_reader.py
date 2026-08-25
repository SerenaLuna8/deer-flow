from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkNotFound
from app.private_work.run_execution_state import (
    RunExecutionState,
    RunExecutionStatePolicy,
    RunExecutionStateUnavailable,
    read_run_execution_state,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole

PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OWNER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
MEMBERSHIP_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
THREAD_ID = "44444444-4444-4444-8444-444444444444"
RUN_ID = "55555555-5555-4555-8555-555555555555"
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
            request_id="run-execution-state-reader",
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


class _MappingResult:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def mappings(self) -> _MappingResult:
        return self

    def one_or_none(self) -> object | None:
        return self._row


class _Session:
    def __init__(self, row: object | None) -> None:
        self.row = row
        self.statements: list[str] = []

    async def execute(self, statement) -> _MappingResult:
        self.statements.append(
            str(statement.compile(dialect=postgresql.dialect())),
        )
        return _MappingResult(self.row)


@pytest.mark.asyncio
async def test_reader_projects_exact_running_authority_with_one_scoped_query() -> None:
    session = _Session(_executing_row())

    result = await read_run_execution_state(
        session,  # type: ignore[arg-type]
        _context(),
        THREAD_ID,
        RUN_ID,
        RunExecutionStatePolicy(worker_fresh_for_seconds=60),
    )

    assert result == RunExecutionState(
        phase="executing",
        observed_at=OBSERVED_AT,
        phase_started_at=OBSERVED_AT - timedelta(seconds=12),
        execution_started_at=OBSERVED_AT - timedelta(seconds=12),
        retry_at=None,
        run_status="running",
    )
    assert len(session.statements) == 1
    assert "clock_timestamp()" in session.statements[0]
    assert "threads_meta" in session.statements[0]
    assert "worker_nodes" in session.statements[0]


@pytest.mark.asyncio
async def test_reader_returns_typed_unavailable_for_lease_identity_mismatch() -> None:
    session = _Session(
        _executing_row(active_attempt_lease_token_hash="b" * 64),
    )

    result = await read_run_execution_state(
        session,  # type: ignore[arg-type]
        _context(),
        THREAD_ID,
        RUN_ID,
        RunExecutionStatePolicy(worker_fresh_for_seconds=60),
    )

    assert result == RunExecutionStateUnavailable(observed_at=OBSERVED_AT)
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_reader_hides_missing_scoped_run_as_not_found() -> None:
    session = _Session(None)

    with pytest.raises(PrivateWorkNotFound):
        await read_run_execution_state(
            session,  # type: ignore[arg-type]
            _context(),
            THREAD_ID,
            RUN_ID,
            RunExecutionStatePolicy(worker_fresh_for_seconds=60),
        )

    assert len(session.statements) == 1
