from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from support.private_thread_seed import seed_private_thread_database
from support.run_closure import add_sealed_test_run

from app.reliability import process_readiness
from app.reliability.operations import SystemOperationsRepository
from app.reliability.process_readiness import (
    ProcessReadinessSnapshot,
    read_process_readiness,
)
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow


def test_process_readiness_keeps_global_and_private_run_fleet_aggregates_distinct() -> None:
    snapshot = ProcessReadinessSnapshot(
        ready=True,
        role="gateway",
        worker_fleet="ready",
        worker_count=4,
        worker_capacity=12,
        worker_oldest_heartbeat_age_seconds=9,
        private_run_worker_fleet="ready",
        private_run_worker_count=2,
        private_run_worker_capacity=7,
        scheduler="disabled",
        scheduler_ownership="disabled",
        schema_state="ready",
    )

    assert snapshot.as_public_dict() == {
        "ready": True,
        "role": "gateway",
        "worker_fleet": "ready",
        "worker_count": 4,
        "worker_capacity": 12,
        "worker_oldest_heartbeat_age_seconds": 9,
        "private_run_worker_fleet": "ready",
        "private_run_worker_count": 2,
        "private_run_worker_capacity": 7,
        "scheduler": "disabled",
        "scheduler_ownership": "disabled",
        "schema_state": "ready",
    }


class _Probe:
    async def read(self, _session: object) -> SimpleNamespace:
        return SimpleNamespace(ready=True)


class _WorkerResult:
    def one(self) -> SimpleNamespace:
        return SimpleNamespace(
            worker_count=3,
            capacity=10,
            private_run_worker_count=1,
            private_run_capacity=4,
            oldest=datetime(2026, 8, 25, 9, 59, 50, tzinfo=UTC),
        )


class _Session:
    def __init__(self) -> None:
        self.worker_sql = ""
        self.params: dict[str, object] = {}

    async def scalar(self, _statement: object) -> bool:
        return True

    async def execute(
        self,
        statement: object,
        params: dict[str, object],
    ) -> _WorkerResult:
        self.worker_sql = str(statement)
        self.params = params
        return _WorkerResult()


@pytest.mark.asyncio
async def test_process_readiness_scopes_private_run_without_changing_global_fleet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    observed_at = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(process_readiness, "FinalSchemaProbe", lambda: _Probe())

    snapshot = await read_process_readiness(
        session,  # type: ignore[arg-type]
        role="gateway",
        scheduler_enabled=False,
        worker_fresh_for_seconds=60,
        now=observed_at,
    )

    assert snapshot.worker_count == 3
    assert snapshot.worker_capacity == 10
    assert snapshot.private_run_worker_count == 1
    assert snapshot.private_run_worker_capacity == 4
    assert snapshot.private_run_worker_fleet == "unavailable"
    assert snapshot.ready is False
    assert session.params == {"cutoff": observed_at - timedelta(seconds=60)}
    assert "capabilities_json::jsonb ? 'private_run'" in session.worker_sql
    assert "execution_domain_affinity" not in session.worker_sql


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_worker_fleet_and_queue_convergence_aggregates(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    engine = seed.engine
    factory = seed.factory
    observed_at = datetime.now(UTC)
    private_worker_id = uuid.uuid4()
    affinity = "a" * 64
    missing_affinity = "b" * 64
    try:
        async with factory() as session, session.begin():
            session.add_all(
                [
                    WorkerNodeRow(
                        id=private_worker_id,
                        version="private-run",
                        capabilities_json=["private_run"],
                        max_concurrent_jobs=3,
                        execution_domain_affinity=affinity,
                        draining=False,
                        started_at=observed_at,
                        heartbeat_at=observed_at,
                    ),
                    WorkerNodeRow(
                        version="other-work",
                        capabilities_json=["memory_seal"],
                        max_concurrent_jobs=5,
                        execution_domain_affinity=None,
                        draining=False,
                        started_at=observed_at,
                        heartbeat_at=observed_at,
                    ),
                    WorkerNodeRow(
                        version="stale-private-run",
                        capabilities_json=["private_run"],
                        max_concurrent_jobs=7,
                        execution_domain_affinity=None,
                        draining=False,
                        started_at=observed_at - timedelta(seconds=120),
                        heartbeat_at=observed_at - timedelta(seconds=120),
                    ),
                    WorkerNodeRow(
                        version="draining-private-run",
                        capabilities_json=["private_run"],
                        max_concurrent_jobs=11,
                        execution_domain_affinity=None,
                        draining=True,
                        started_at=observed_at,
                        heartbeat_at=observed_at,
                    ),
                ]
            )
            await session.flush()

            queued_thread_id = str(uuid.uuid4())
            terminalizing_thread_id = str(uuid.uuid4())
            session.add_all(
                [
                    ThreadMetaRow(
                        thread_id=queued_thread_id,
                        owner_user_id=str(seed.owner_a.user_id),
                        project_id=seed.owner_a.project_id,
                        agent_asset_id=seed.project_agent_id,
                        agent_scope="project",
                        thread_kind="chat",
                        status="idle",
                        metadata_json={},
                        version=1,
                        created_at=observed_at - timedelta(seconds=30),
                        updated_at=observed_at - timedelta(seconds=30),
                    ),
                    ThreadMetaRow(
                        thread_id=terminalizing_thread_id,
                        owner_user_id=str(seed.owner_a.user_id),
                        project_id=seed.owner_a.project_id,
                        agent_asset_id=seed.project_agent_id,
                        agent_scope="project",
                        thread_kind="chat",
                        status="idle",
                        metadata_json={},
                        version=1,
                        created_at=observed_at - timedelta(seconds=50),
                        updated_at=observed_at - timedelta(seconds=50),
                    ),
                ]
            )
            await session.flush()

            queued_run = RunRow(
                run_id=str(uuid.uuid4()),
                thread_id=queued_thread_id,
                assistant_id=str(seed.project_agent_id),
                owner_user_id=str(seed.owner_a.user_id),
                project_id=seed.owner_a.project_id,
                status="pending",
                origin_trace_id=f"operations-queued-{uuid.uuid4().hex}",
                metadata_json={},
                kwargs_json={},
                created_at=observed_at - timedelta(seconds=30),
                updated_at=observed_at - timedelta(seconds=30),
            )
            terminalizing_run = RunRow(
                run_id=str(uuid.uuid4()),
                thread_id=terminalizing_thread_id,
                assistant_id=str(seed.project_agent_id),
                owner_user_id=str(seed.owner_a.user_id),
                project_id=seed.owner_a.project_id,
                status="running",
                origin_trace_id=(f"operations-terminalizing-{uuid.uuid4().hex}"),
                metadata_json={},
                kwargs_json={},
                created_at=observed_at - timedelta(seconds=50),
                updated_at=observed_at - timedelta(seconds=50),
                execution_started_at=observed_at - timedelta(seconds=45),
                execution_lease_token_hash="c" * 64,
                execution_lease_expires_at=observed_at - timedelta(seconds=10),
            )
            await add_sealed_test_run(session, queued_run)
            await add_sealed_test_run(session, terminalizing_run)

            queued_job = JobRow(
                job_type="private_run",
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                owner_private_generation=1,
                run_id=queued_run.run_id,
                origin_trace_id=queued_run.origin_trace_id,
                idempotency_key=uuid.uuid4().hex * 2,
                status="queued",
                available_at=observed_at - timedelta(seconds=30),
                attempt_count=0,
                max_attempts=3,
                retry_safety="safe",
                execution_domain_affinity=missing_affinity,
                created_at=observed_at - timedelta(seconds=30),
                updated_at=observed_at - timedelta(seconds=30),
            )
            terminalizing_job = JobRow(
                job_type="private_run",
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                owner_private_generation=1,
                run_id=terminalizing_run.run_id,
                origin_trace_id=terminalizing_run.origin_trace_id,
                idempotency_key=uuid.uuid4().hex * 2,
                status="running",
                available_at=observed_at - timedelta(seconds=50),
                attempt_count=1,
                max_attempts=3,
                lease_owner_id=private_worker_id,
                lease_token_hash="c" * 64,
                lease_expires_at=observed_at - timedelta(seconds=10),
                heartbeat_at=observed_at - timedelta(seconds=20),
                retry_safety="unsafe",
                execution_domain_affinity=affinity,
                created_at=observed_at - timedelta(seconds=50),
                started_at=observed_at - timedelta(seconds=48),
                updated_at=observed_at - timedelta(seconds=20),
            )
            session.add_all([queued_job, terminalizing_job])
            await session.flush()
            queued_run.job_id = queued_job.id
            terminalizing_run.job_id = terminalizing_job.id
            session.add(
                JobAttemptRow(
                    job_id=terminalizing_job.id,
                    attempt_number=1,
                    worker_id=private_worker_id,
                    lease_token_hash="c" * 64,
                    started_at=observed_at - timedelta(seconds=48),
                    execution_started_at=observed_at - timedelta(seconds=45),
                    heartbeat_at=observed_at - timedelta(seconds=20),
                )
            )

        async with factory() as session:
            snapshot = await read_process_readiness(
                session,
                role="gateway",
                scheduler_enabled=False,
                worker_fresh_for_seconds=60,
                now=observed_at,
            )
            overview = await SystemOperationsRepository(session).overview(
                worker_fresh_for_seconds=60,
                now=observed_at,
            )

        assert (snapshot.worker_count, snapshot.worker_capacity) == (2, 8)
        assert (
            snapshot.private_run_worker_count,
            snapshot.private_run_worker_capacity,
        ) == (1, 3)
        assert overview.counts.ready_jobs == 1
        assert overview.counts.oldest_ready_job_age_seconds is not None
        assert overview.counts.oldest_ready_job_age_seconds >= 29
        assert overview.counts.stale_leases == 1
        assert overview.counts.waiting_for_worker_runs == 1
        assert overview.counts.waiting_for_terminalization_runs == 1
    finally:
        await engine.dispose()
