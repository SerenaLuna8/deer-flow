from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.errors import PrivateWorkConflict, PrivateWorkUnavailable
from app.private_work.run_admission import (
    PrivateRunAdmissionServerContext,
    PrivateRunAdmissionService,
)
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.reliability.errors import ReliabilityQuotaExceeded


async def _create_thread(seed, thread_id: str) -> None:
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_admission_atomically_persists_run_and_private_job(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-admit-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    try:
        await _create_thread(seed, thread_id)

        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(run_id=run_id, metadata={"source": "m6"}),
        )

        assert admitted.run.job_id == admitted.job.job_id
        assert admitted.job.job_type == "private_run"
        assert admitted.job.project_id == seed.owner_a.project_id
        assert admitted.job.owner_user_id == str(seed.owner_a.user_id)
        assert admitted.job.run_id == run_id
        assert admitted.job.status == "queued"

        async with seed.factory() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT r.job_id,j.run_id,j.project_id,j.owner_user_id,j.status
                        FROM runs r JOIN jobs j ON j.id=r.job_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": run_id},
                )
            ).one()
        assert row.job_id == admitted.job.job_id
        assert row.run_id == run_id
        assert row.project_id == seed.owner_a.project_id
        assert row.owner_user_id == str(seed.owner_a.user_id)
        assert row.status == "queued"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_admission_rolls_back_run_job_and_snapshot_when_audit_fails(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-audit-rollback-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())

    class FailingAudit:
        async def run_admitted(self, session, context, run, job) -> None:
            assert session.in_transaction()
            assert run.job_id == job.job_id
            raise PrivateWorkUnavailable(context.request_id)

    try:
        await _create_thread(seed, thread_id)
        with pytest.raises(PrivateWorkUnavailable):
            await PrivateRunAdmissionService(seed.factory, audit=FailingAudit()).admit(
                seed.owner_a,
                thread_id,
                PrivateRunCreate(run_id=run_id),
            )

        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM runs WHERE run_id=:run_id) AS runs,
                        (SELECT count(*) FROM jobs WHERE run_id=:run_id) AS jobs,
                        (SELECT count(*) FROM run_asset_versions WHERE run_id=:run_id) AS assets"""
                    ),
                    {"run_id": run_id},
                )
            ).one()
        assert tuple(counts) == (0, 0, 0)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_admission_retry_returns_same_run_job_pair_without_duplicate_hooks(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-idempotent-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    calls = {"quota": 0, "audit": 0}

    class Quota:
        async def reserve_concurrent_run(self, session, context, run) -> None:
            assert session.in_transaction()
            calls["quota"] += 1

    class Audit:
        async def run_admitted(self, session, context, run, job) -> None:
            assert session.in_transaction()
            calls["audit"] += 1

    try:
        await _create_thread(seed, thread_id)
        service = PrivateRunAdmissionService(seed.factory, quota=Quota(), audit=Audit())
        request = PrivateRunCreate(
            run_id=run_id,
            metadata={"source": "retry"},
            kwargs={"command": {"resume": "first"}},
        )

        first = await service.admit(seed.owner_a, thread_id, request)
        second = await service.admit(seed.owner_a, thread_id, request)

        assert second.run == first.run
        assert second.job == first.job
        assert second.snapshot == first.snapshot
        assert calls == {"quota": 1, "audit": 1}
        with pytest.raises(PrivateWorkConflict):
            await service.admit(
                seed.owner_a,
                thread_id,
                PrivateRunCreate(
                    run_id=run_id,
                    metadata={"source": "retry"},
                    kwargs={"command": {"resume": "different"}},
                ),
            )
        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM runs WHERE run_id=:run_id) AS runs,
                        (SELECT count(*) FROM jobs WHERE run_id=:run_id) AS jobs"""
                    ),
                    {"run_id": run_id},
                )
            ).one()
        assert tuple(counts) == (1, 1)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_admission_persists_only_typed_server_non_interactive_authority(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-server-context-{uuid.uuid4()}"
    try:
        await _create_thread(seed, thread_id)
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(
                kwargs={
                    "interrupt_before": "*",
                    "config": {
                        "context": {
                            "non_interactive": True,
                            "project_id": "forged-project",
                        }
                    },
                }
            ),
            server_context=PrivateRunAdmissionServerContext(non_interactive=True),
        )

        assert admitted.run.kwargs["config"]["context"] == {
            "non_interactive": True,
        }
        assert admitted.run.kwargs["interrupt_before"] == "*"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_admission_quota_rejection_leaves_no_partial_rows(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-quota-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())

    class RejectingQuota:
        async def reserve_concurrent_run(self, session, context, run) -> None:
            assert session.in_transaction()
            raise ReliabilityQuotaExceeded(context.request_id)

    try:
        await _create_thread(seed, thread_id)
        with pytest.raises(ReliabilityQuotaExceeded):
            await PrivateRunAdmissionService(seed.factory, quota=RejectingQuota()).admit(
                seed.owner_a,
                thread_id,
                PrivateRunCreate(run_id=run_id),
            )

        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM runs WHERE run_id=:run_id) AS runs,
                        (SELECT count(*) FROM jobs WHERE run_id=:run_id) AS jobs,
                        (SELECT count(*) FROM run_asset_versions WHERE run_id=:run_id) AS assets"""
                    ),
                    {"run_id": run_id},
                )
            ).one()
        assert tuple(counts) == (0, 0, 0)
    finally:
        await seed.engine.dispose()
