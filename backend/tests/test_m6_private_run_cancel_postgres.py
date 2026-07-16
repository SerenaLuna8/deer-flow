from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.reliability.execution import AgentExecutionResult, PrivateRunJobHandler
from app.reliability.workers import WorkerRegistry
from app.worker.service import JobLeaseAuthority
from deerflow.persistence.jobs.sql import JobRepository


async def _admit(seed, thread_id: str):
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
    return await PrivateRunAdmissionService(seed.factory).admit(
        seed.owner_a,
        thread_id,
        PrivateRunCreate(),
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_cancel_queued_private_run_settles_run_and_job_once(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-cancel-queued-{uuid.uuid4()}"
    try:
        admitted = await _admit(seed, thread_id)
        service = PrivateRunService(seed.factory)
        await service.cancel(seed.owner_a, thread_id, admitted.run.run_id)
        await service.cancel(seed.owner_a, thread_id, admitted.run.run_id)

        async with seed.factory() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT r.status,r.cancel_reason,j.status,j.cancel_reason
                        FROM runs r JOIN jobs j ON j.id=r.job_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert tuple(row) == (
            "interrupted",
            "user_requested",
            "cancelled",
            "user_requested",
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_cancel_leased_private_run_prevents_agent_execution(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-cancel-leased-{uuid.uuid4()}"
    executor_calls = 0

    class Executor:
        async def execute(self, _execution, _authority):
            nonlocal executor_calls
            executor_calls += 1
            return AgentExecutionResult.succeeded()

    try:
        admitted = await _admit(seed, thread_id)
        worker_id = uuid.uuid4()
        await WorkerRegistry(seed.factory, version="test-m6-cancel").register(
            worker_id,
            frozenset({"private_run"}),
            1,
        )
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=90,
            )
            assert claim is not None
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )

        await PrivateRunService(seed.factory).cancel(
            seed.owner_a,
            thread_id,
            admitted.run.run_id,
        )
        settlement = await PrivateRunJobHandler(seed.factory, executor=Executor())(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        assert executor_calls == 0
        async with seed.factory() as session:
            states = (
                await session.execute(
                    text(
                        """SELECT r.status,j.status
                        FROM runs r JOIN jobs j ON j.id=r.job_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert tuple(states) == ("interrupted", "cancelled")
    finally:
        await seed.engine.dispose()
