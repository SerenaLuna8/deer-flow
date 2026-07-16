from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from support.m4_private_threads import seed_m4_thread_database

from app.reliability.workers import WorkerRegistry
from app.worker.service import JobOutcome, WorkerService
from deerflow.config.worker_config import WorkerConfig
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import EnqueueJob, JobRepository, JobScope
from deerflow.persistence.projects.model import ProjectRow


@pytest_asyncio.fixture()
async def worker_registry(migrated_postgres_database_url: str):
    data = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield WorkerRegistry(data.factory, version="m6-test")
    finally:
        await data.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_registry_persists_only_bounded_worker_metadata(worker_registry: WorkerRegistry) -> None:
    worker_id = uuid.uuid4()
    started = datetime(2026, 1, 1, tzinfo=UTC)
    await worker_registry.register(
        worker_id,
        frozenset({"private_run", "retention_purge"}),
        4,
        now=started,
    )

    async with worker_registry.session_factory() as session:
        row = await session.get(WorkerNodeRow, worker_id)
    assert row is not None
    assert row.version == "m6-test"
    assert row.capabilities_json == ["private_run", "retention_purge"]
    assert row.max_concurrent_jobs == 4
    assert row.started_at == started
    assert row.heartbeat_at == started
    assert row.draining is False
    assert set(row.__table__.columns.keys()) == {
        "id",
        "version",
        "capabilities_json",
        "max_concurrent_jobs",
        "draining",
        "started_at",
        "heartbeat_at",
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_fresh_capability_excludes_stale_and_draining_workers(
    worker_registry: WorkerRegistry,
) -> None:
    worker_id = uuid.uuid4()
    started = datetime(2026, 1, 1, tzinfo=UTC)
    await worker_registry.register(
        worker_id,
        frozenset({"private_run"}),
        2,
        now=started,
    )
    assert await worker_registry.has_fresh_capability(
        "private_run",
        fresh_for_seconds=30,
        now=started + timedelta(seconds=29),
    )
    assert not await worker_registry.has_fresh_capability(
        "private_run",
        fresh_for_seconds=30,
        now=started + timedelta(seconds=31),
    )

    assert await worker_registry.heartbeat(worker_id, now=started + timedelta(seconds=31))
    assert await worker_registry.has_fresh_capability(
        "private_run",
        fresh_for_seconds=30,
        now=started + timedelta(seconds=31),
    )
    assert await worker_registry.mark_draining(worker_id, now=started + timedelta(seconds=32))
    assert not await worker_registry.has_fresh_capability(
        "private_run",
        fresh_for_seconds=30,
        now=started + timedelta(seconds=32),
    )
    assert not await worker_registry.heartbeat(uuid.uuid4(), now=started)
    assert await worker_registry.remove(worker_id)
    assert await worker_registry.remove(worker_id)
    assert not await worker_registry.remove(uuid.uuid4())
    async with worker_registry.session_factory() as session:
        retained = await session.get(WorkerNodeRow, worker_id)
    assert retained is not None and retained.draining is True


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_exit_preserves_node_referenced_by_attempt_history(
    worker_registry: WorkerRegistry,
) -> None:
    worker_id = uuid.uuid4()
    async with worker_registry.session_factory() as session, session.begin():
        project_id = await session.scalar(select(ProjectRow.id).limit(1))
        assert project_id is not None
        job_id = await JobRepository(session).enqueue(
            EnqueueJob(
                job_type="retention_purge",
                scope=JobScope(project_id, None),
                idempotency_key="a" * 64,
                run_id=None,
                occurrence_id=None,
                max_attempts=3,
            )
        )

    async def handler(_claim, _authority):
        return JobOutcome.succeeded()

    service = WorkerService(
        worker_registry.session_factory,
        worker_registry,
        {"retention_purge": handler},
        WorkerConfig(),
        worker_id=worker_id,
    )
    await service.run_until_idle()

    async with worker_registry.session_factory() as session:
        node = await session.get(WorkerNodeRow, worker_id)
        job = await session.get(JobRow, job_id)
        attempt = await session.scalar(select(JobAttemptRow).where(JobAttemptRow.job_id == job_id))
    assert node is not None and node.draining is True
    assert job is not None and job.status == "succeeded"
    assert attempt is not None and attempt.worker_id == worker_id
