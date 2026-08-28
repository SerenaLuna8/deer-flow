from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from support.private_thread_seed import (
    TEST_MODEL_REF,
    seed_private_thread_database,
)
from support.run_closure import add_sealed_test_run

from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.reliability.run_execution.settlement import PrivateRunJobTerminalPort
from deerflow.persistence.jobs.model import (
    DeadJobRow,
    JobAttemptRow,
    JobRow,
    WorkerNodeRow,
)
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.persistence.run.model import RunRow
from deerflow.runtime.events.models import (
    StreamLeaseProof,
    StreamTerminalCandidate,
)
from deerflow.runtime.events.store.db import DbRunEventStore


def _run_row(
    *,
    project_id: uuid.UUID,
    owner_user_id: str,
    thread_id: str,
    assistant_id: uuid.UUID,
    run_id: str,
    origin_trace_id: str,
    status: str,
    asset_closure_sealed: bool,
    authorization_revoked_at: datetime | None = None,
    lease_token_hash: str | None = None,
    lease_expires_at: datetime | None = None,
) -> RunRow:
    return RunRow(
        run_id=run_id,
        thread_id=thread_id,
        assistant_id=str(assistant_id),
        owner_user_id=owner_user_id,
        status=status,
        model_name=TEST_MODEL_REF,
        multitask_strategy="reject",
        metadata_json={},
        kwargs_json={},
        origin_trace_id=origin_trace_id,
        project_id=project_id,
        finalization_status="pending",
        asset_closure_sealed=asset_closure_sealed,
        authorization_cancel_requested_at=authorization_revoked_at,
        authorization_cancel_reason=(None if authorization_revoked_at is None else "authorization_revoked"),
        execution_lease_token_hash=lease_token_hash,
        execution_lease_expires_at=lease_expires_at,
        execution_heartbeat_at=(None if lease_token_hash is None else datetime.now(UTC)),
        execution_started_at=(None if lease_token_hash is None else datetime.now(UTC)),
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_dead_terminal_reconciliation_advances_past_ineligible_page(
    migrated_postgres_database_url: str,
) -> None:
    """A bounded fallback eventually reaches proof beyond its first page."""

    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    worker_id = uuid.uuid4()
    owner_user_id = str(seed.owner_a.user_id)
    project_id = seed.owner_a.project_id
    old_dead_at = datetime(1900, 1, 1, tzinfo=UTC)
    scan_at = datetime(1990, 1, 1, tzinfo=UTC)
    lease_token = "fair-dead-terminal-reconciliation"
    lease_token_hash = hashlib.sha256(lease_token.encode()).hexdigest()
    lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    eligible_run_id = str(uuid.uuid4())
    eligible_trace_id = uuid.uuid4().hex
    eligible_run = _run_row(
        project_id=project_id,
        owner_user_id=owner_user_id,
        thread_id=thread_id,
        assistant_id=seed.project_agent_id,
        run_id=eligible_run_id,
        origin_trace_id=eligible_trace_id,
        status="running",
        asset_closure_sealed=False,
        lease_token_hash=lease_token_hash,
        lease_expires_at=lease_expires_at,
    )

    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="dead-terminal-fair-scan",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=1,
                )
            )

            blocker_runs: list[RunRow] = []
            for index in range(100):
                run_id = str(uuid.uuid4())
                blocker_runs.append(
                    _run_row(
                        project_id=project_id,
                        owner_user_id=owner_user_id,
                        thread_id=thread_id,
                        assistant_id=seed.project_agent_id,
                        run_id=run_id,
                        origin_trace_id=uuid.uuid4().hex,
                        status="interrupted",
                        asset_closure_sealed=True,
                        authorization_revoked_at=(old_dead_at + timedelta(seconds=index)),
                    )
                )
            session.add_all(blocker_runs)
            await add_sealed_test_run(session, eligible_run)

            blocker_jobs: list[JobRow] = []
            for index, run in enumerate(blocker_runs):
                dead_at = old_dead_at + timedelta(seconds=index)
                blocker_jobs.append(
                    JobRow(
                        job_type="private_run",
                        project_id=project_id,
                        owner_user_id=owner_user_id,
                        owner_private_generation=1,
                        run_id=run.run_id,
                        origin_trace_id=run.origin_trace_id,
                        idempotency_key=hashlib.sha256(
                            f"fair-dead-blocker:{run.run_id}".encode(),
                        ).hexdigest(),
                        status="dead",
                        attempt_count=1,
                        max_attempts=3,
                        retry_safety="unknown",
                        public_error_code="SIDE_EFFECT_STATE_UNKNOWN",
                        available_at=dead_at,
                        completed_at=dead_at,
                        created_at=dead_at,
                        updated_at=dead_at,
                    )
                )
            eligible_job = JobRow(
                job_type="private_run",
                project_id=project_id,
                owner_user_id=owner_user_id,
                owner_private_generation=1,
                run_id=eligible_run_id,
                origin_trace_id=eligible_trace_id,
                idempotency_key=hashlib.sha256(
                    f"fair-dead-eligible:{eligible_run_id}".encode(),
                ).hexdigest(),
                status="running",
                attempt_count=1,
                max_attempts=3,
                retry_safety="unknown",
                available_at=old_dead_at,
                lease_owner_id=worker_id,
                lease_token_hash=lease_token_hash,
                lease_expires_at=lease_expires_at,
                heartbeat_at=datetime.now(UTC),
                started_at=datetime.now(UTC),
            )
            session.add_all([*blocker_jobs, eligible_job])
            await session.flush()

            for index, (run, job) in enumerate(
                zip(blocker_runs, blocker_jobs, strict=True),
            ):
                dead_at = old_dead_at + timedelta(seconds=index)
                run.job_id = job.id
                session.add(
                    DeadJobRow(
                        job_id=job.id,
                        project_id=project_id,
                        owner_ref_key_id="test",
                        owner_ref_hmac="a" * 64,
                        job_type="private_run",
                        attempt_count=1,
                        retry_safety="unknown",
                        public_error_code="SIDE_EFFECT_STATE_UNKNOWN",
                        dead_at=dead_at,
                    )
                )
            eligible_run.job_id = eligible_job.id
            session.add(
                JobAttemptRow(
                    job_id=eligible_job.id,
                    attempt_number=1,
                    worker_id=worker_id,
                    lease_token_hash=lease_token_hash,
                    started_at=datetime.now(UTC),
                    heartbeat_at=datetime.now(UTC),
                )
            )

        events = DbRunEventStore(
            seed.factory,
            run_event_notify_enabled=False,
        )
        terminal_candidate = StreamTerminalCandidate(
            status="error",
            error_code="MODEL_OUTPUT_LIMIT",
            authority="durable_response",
            precedence="preempts_ordinary_stop",
        )
        async with seed.factory() as session, session.begin():
            await events.append_stream_terminal_candidate(
                session,
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                run_id=eligible_run_id,
                candidate=terminal_candidate,
                lease=StreamLeaseProof(
                    job_id=eligible_job.id,
                    lease_token=lease_token,
                ),
            )

        eligible_dead_at = old_dead_at + timedelta(seconds=100)
        async with seed.factory() as session, session.begin():
            job = await session.get(JobRow, eligible_job.id, with_for_update=True)
            run = await session.get(RunRow, eligible_run_id, with_for_update=True)
            attempt = await session.scalar(
                sa.select(JobAttemptRow)
                .where(
                    JobAttemptRow.job_id == eligible_job.id,
                    JobAttemptRow.attempt_number == 1,
                )
                .with_for_update(of=JobAttemptRow)
            )
            assert job is not None and run is not None and attempt is not None
            job.status = "dead"
            job.public_error_code = "SIDE_EFFECT_STATE_UNKNOWN"
            job.lease_owner_id = None
            job.lease_token_hash = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.completed_at = eligible_dead_at
            job.updated_at = eligible_dead_at
            attempt.outcome = "dead"
            attempt.public_error_code = "SIDE_EFFECT_STATE_UNKNOWN"
            attempt.finished_at = eligible_dead_at
            run.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.add(
                DeadJobRow(
                    job_id=job.id,
                    project_id=project_id,
                    owner_ref_key_id="test",
                    owner_ref_hmac="b" * 64,
                    job_type="private_run",
                    attempt_count=1,
                    retry_safety="unknown",
                    public_error_code="SIDE_EFFECT_STATE_UNKNOWN",
                    dead_at=eligible_dead_at,
                )
            )

        terminal_port = PrivateRunJobTerminalPort(event_store=events)
        async with seed.factory() as session, session.begin():
            first_claim = await JobRepository(
                session,
                terminal_port=terminal_port,
            ).claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
                now=scan_at,
            )
        assert first_claim is None

        async with seed.factory() as session, session.begin():
            successor_claim = await JobRepository(
                session,
                terminal_port=terminal_port,
            ).claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
                now=scan_at,
            )

        assert successor_claim is not None
        assert successor_claim.predecessor_dead_job_id == eligible_job.id
        assert successor_claim.settlement_only is True

        async with seed.factory() as session:
            predecessor = await session.get(JobRow, eligible_job.id)
            dead = await session.get(DeadJobRow, eligible_job.id)
            run = await session.get(RunRow, eligible_run_id)
            blocker_successors = await session.scalar(
                sa.select(sa.func.count())
                .select_from(JobRow)
                .where(
                    JobRow.predecessor_dead_job_id.in_(
                        [job.id for job in blocker_jobs],
                    )
                )
            )
            dead_lineage_count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(DeadJobRow)
                .where(
                    DeadJobRow.job_id.in_(
                        [job.id for job in blocker_jobs] + [eligible_job.id],
                    )
                )
            )
            stored_candidate = await events.get_stream_terminal_candidate(
                session,
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                run_id=eligible_run_id,
            )
            assert predecessor is not None and predecessor.status == "dead"
            assert dead is not None and dead.dead_at == eligible_dead_at
            assert run is not None and run.job_id == successor_claim.job_id
            assert blocker_successors == 0
            assert dead_lineage_count == 101
            assert stored_candidate == terminal_candidate
    finally:
        await seed.engine.dispose()
