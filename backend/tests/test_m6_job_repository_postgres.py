from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

import deerflow.persistence.jobs.sql as jobs_sql
from deerflow.persistence.jobs.model import DeadJobRow, JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import (
    DeadJobRequeuedEvent,
    EnqueueJob,
    JobHeartbeat,
    JobIdempotencyConflict,
    JobOwnerRef,
    JobOwnerRefRequired,
    JobRepository,
    JobRequeueForbidden,
    JobScope,
    consume_issued_dead_job_requeued_event,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow


@dataclass(frozen=True, slots=True)
class JobSeed:
    data: M4ThreadSeed
    scope: JobScope
    worker_a: uuid.UUID
    worker_b: uuid.UUID


@pytest_asyncio.fixture()
async def seed(migrated_postgres_database_url: str) -> JobSeed:
    data = await seed_m4_thread_database(migrated_postgres_database_url)
    worker_a = uuid.uuid4()
    worker_b = uuid.uuid4()
    async with data.factory() as session, session.begin():
        session.add_all(
            [
                WorkerNodeRow(
                    id=worker_a,
                    version="test",
                    capabilities_json=["private_run", "retention_purge"],
                    max_concurrent_jobs=4,
                ),
                WorkerNodeRow(
                    id=worker_b,
                    version="test",
                    capabilities_json=["private_run", "retention_purge"],
                    max_concurrent_jobs=4,
                ),
            ]
        )
    value = JobSeed(
        data=data,
        scope=JobScope(data.owner_a.project_id, str(data.owner_a.user_id)),
        worker_a=worker_a,
        worker_b=worker_b,
    )
    try:
        yield value
    finally:
        await data.engine.dispose()


async def _create_private_run(seed: JobSeed) -> str:
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    async with seed.data.factory() as session, session.begin():
        session.add(
            ThreadMetaRow(
                thread_id=thread_id,
                owner_user_id=seed.scope.owner_user_id,
                project_id=seed.scope.project_id,
                agent_asset_id=seed.data.project_agent_id,
                agent_scope="project",
            )
        )
        await session.flush()
        session.add(
            RunRow(
                run_id=run_id,
                thread_id=thread_id,
                owner_user_id=seed.scope.owner_user_id,
                project_id=seed.scope.project_id,
                status="pending",
            )
        )
    return run_id


def _private_request(
    seed: JobSeed,
    run_id: str,
    key: str,
    *,
    max_attempts: int = 3,
    retry_safety: str = "safe",
    available_at: datetime | None = None,
) -> EnqueueJob:
    return EnqueueJob(
        job_type="private_run",
        scope=seed.scope,
        idempotency_key=key,
        run_id=run_id,
        occurrence_id=None,
        max_attempts=max_attempts,
        retry_safety=retry_safety,  # type: ignore[arg-type]
        available_at=available_at,
    )


def _retention_request(
    seed: JobSeed,
    key: str,
    *,
    max_attempts: int,
    retry_safety: str = "safe",
    available_at: datetime | None = None,
) -> EnqueueJob:
    return EnqueueJob(
        job_type="retention_purge",
        scope=JobScope(seed.scope.project_id, None),
        idempotency_key=key,
        run_id=None,
        occurrence_id=None,
        max_attempts=max_attempts,
        retry_safety=retry_safety,  # type: ignore[arg-type]
        available_at=available_at,
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_rejects_cross_authority_key_reuse(seed: JobSeed) -> None:
    run_id = await _create_private_run(seed)
    request = _private_request(seed, run_id, "a" * 64)

    async def enqueue_once() -> uuid.UUID:
        async with seed.data.factory() as session, session.begin():
            return await JobRepository(session).enqueue(request)

    first, second = await asyncio.gather(enqueue_once(), enqueue_once())
    assert first == second
    async with seed.data.factory() as session:
        job_count = len(
            (
                await session.execute(
                    select(JobRow.id).where(
                        JobRow.job_type == "private_run",
                        JobRow.idempotency_key == "a" * 64,
                    )
                )
            ).all()
        )
    assert job_count == 1

    conflicting = EnqueueJob(
        job_type="private_run",
        scope=JobScope(seed.data.project_b_owner_a.project_id, str(seed.data.owner_a.user_id)),
        idempotency_key="a" * 64,
        run_id=run_id,
        occurrence_id=None,
        max_attempts=3,
    )
    async with seed.data.factory() as session, session.begin():
        with pytest.raises(JobIdempotencyConflict):
            await JobRepository(session).enqueue(conflicting)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_twenty_concurrent_claims_have_exactly_one_owner(seed: JobSeed) -> None:
    for index in range(20):
        run_id = await _create_private_run(seed)
        async with seed.data.factory() as session, session.begin():
            await JobRepository(session).enqueue(_private_request(seed, run_id, f"{index + 1:064x}"))

        async def claim(worker_id: uuid.UUID):
            async with seed.data.factory() as session, session.begin():
                return await JobRepository(session).claim_next(
                    worker_id=worker_id,
                    capabilities=frozenset({"private_run"}),
                    lease_seconds=90,
                )

        first, second = await asyncio.gather(
            claim(seed.worker_a),
            claim(seed.worker_b),
        )
        claims = [item for item in (first, second) if item is not None]
        assert len(claims) == 1
        winner = claims[0]
        async with seed.data.factory() as session, session.begin():
            repository = JobRepository(session)
            assert await repository.mark_running(winner.job_id, lease_token=winner.lease_token)
            assert await repository.settle_success(winner.job_id, lease_token=winner.lease_token)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stale_lease_token_cannot_transition_or_complete(seed: JobSeed) -> None:
    run_id = await _create_private_run(seed)
    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(session)
        job_id = await repository.enqueue(_private_request(seed, run_id, "b" * 64))
        claim = await repository.claim_next(
            worker_id=seed.worker_a,
            capabilities=frozenset({"private_run"}),
            lease_seconds=90,
        )
        assert claim is not None and claim.job_id == job_id
        persisted = await session.get(JobRow, job_id)
        assert persisted is not None
        assert persisted.lease_token_hash == hashlib.sha256(claim.lease_token.encode("utf-8")).hexdigest()
        assert claim.lease_token not in {
            persisted.lease_token_hash,
            persisted.idempotency_key,
        }
        assert not await repository.mark_running(job_id, lease_token="wrong-token")
        assert not await repository.heartbeat(job_id, lease_token="wrong-token", lease_seconds=90)
        assert not await repository.retry_or_dead(
            job_id,
            lease_token="wrong-token",
            public_error_code="TEMPORARY_FAILURE",
            retry_initial_seconds=2,
            retry_max_seconds=300,
        )
        assert await repository.mark_running(job_id, lease_token=claim.lease_token)
        assert not await repository.settle_success(job_id, lease_token="wrong-token")
        assert await repository.settle_success(job_id, lease_token=claim.lease_token)
        assert not await repository.settle_success(job_id, lease_token=claim.lease_token)

    async with seed.data.factory() as session:
        job = await session.get(JobRow, job_id)
        attempts = (await session.execute(select(JobAttemptRow).where(JobAttemptRow.job_id == job_id))).scalars().all()
    assert job is not None and job.status == "succeeded" and job.lease_token_hash is None
    assert len(attempts) == 1 and attempts[0].outcome == "succeeded"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_running_cancel_is_settled_only_by_current_owner(seed: JobSeed) -> None:
    run_id = await _create_private_run(seed)
    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(session)
        job_id = await repository.enqueue(_private_request(seed, run_id, "4" * 64))
        claim = await repository.claim_next(
            worker_id=seed.worker_a,
            capabilities=frozenset({"private_run"}),
            lease_seconds=90,
        )
        assert claim is not None
        assert await repository.mark_running(job_id, lease_token=claim.lease_token)
        assert await repository.request_cancel(seed.scope, job_id, reason="user_requested")
        heartbeat = await repository.heartbeat(
            job_id,
            lease_token=claim.lease_token,
            lease_seconds=90,
        )
        assert isinstance(heartbeat, JobHeartbeat)
        assert heartbeat.cancel_requested is True
        assert not await repository.settle_cancelled(job_id, lease_token="wrong-token")
        assert await repository.settle_cancelled(job_id, lease_token=claim.lease_token)
    async with seed.data.factory() as session:
        job = await session.get(JobRow, job_id)
        attempt = (await session.execute(select(JobAttemptRow).where(JobAttemptRow.job_id == job_id))).scalar_one()
    assert job is not None and job.status == "cancelled"
    assert attempt.outcome == "cancelled"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cancel_before_claim_settles_without_an_attempt(seed: JobSeed) -> None:
    run_id = await _create_private_run(seed)
    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(session)
        job_id = await repository.enqueue(_private_request(seed, run_id, "c" * 64))
        assert await repository.request_cancel(seed.scope, job_id, reason="user_requested")
        assert (
            await repository.claim_next(
                worker_id=seed.worker_a,
                capabilities=frozenset({"private_run"}),
                lease_seconds=90,
            )
            is None
        )
    async with seed.data.factory() as session:
        job = await session.get(JobRow, job_id)
        attempt_count = await session.scalar(select(JobAttemptRow).where(JobAttemptRow.job_id == job_id).with_only_columns(JobAttemptRow.id).limit(1))
    assert job is not None and job.status == "cancelled" and job.completed_at is not None
    assert attempt_count is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_expired_safe_lease_reclaims_with_monotonic_attempts(seed: JobSeed) -> None:
    run_id = await _create_private_run(seed)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(session)
        job_id = await repository.enqueue(_private_request(seed, run_id, "d" * 64, available_at=started))
        first = await repository.claim_next(
            worker_id=seed.worker_a,
            capabilities=frozenset({"private_run"}),
            lease_seconds=30,
            now=started,
        )
        assert first is not None
    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(session)
        second = await repository.claim_next(
            worker_id=seed.worker_b,
            capabilities=frozenset({"private_run"}),
            lease_seconds=30,
            now=started + timedelta(seconds=31),
        )
        assert second is not None and second.job_id == job_id
        assert second.attempt_id != first.attempt_id
        assert not await repository.heartbeat(
            job_id,
            lease_token=first.lease_token,
            lease_seconds=30,
            now=started + timedelta(seconds=31),
        )
    async with seed.data.factory() as session:
        attempts = (await session.execute(select(JobAttemptRow).where(JobAttemptRow.job_id == job_id).order_by(JobAttemptRow.attempt_number))).scalars().all()
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert attempts[0].outcome == "lease_lost"
    assert attempts[1].outcome is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_expired_unsafe_or_exhausted_job_becomes_dead(seed: JobSeed) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    cases = (
        ("e" * 64, "unsafe", 3, "SIDE_EFFECT_STATE_UNKNOWN"),
        ("f" * 64, "safe", 1, "ATTEMPTS_EXHAUSTED"),
    )
    for key, retry_safety, max_attempts, expected_code in cases:
        request = _retention_request(
            seed,
            key,
            max_attempts=max_attempts,
            retry_safety=retry_safety,
            available_at=started,
        )
        async with seed.data.factory() as session, session.begin():
            repository = JobRepository(session)
            job_id = await repository.enqueue(request)
            claim = await repository.claim_next(
                worker_id=seed.worker_a,
                capabilities=frozenset({"retention_purge"}),
                lease_seconds=30,
                now=started,
            )
            assert claim is not None
        async with seed.data.factory() as session, session.begin():
            assert (
                await JobRepository(session).claim_next(
                    worker_id=seed.worker_b,
                    capabilities=frozenset({"retention_purge"}),
                    lease_seconds=30,
                    now=started + timedelta(seconds=31),
                )
                is None
            )
        async with seed.data.factory() as session:
            job = await session.get(JobRow, job_id)
            dead = await session.get(DeadJobRow, job_id)
        assert job is not None and job.status == "dead"
        assert dead is not None and dead.public_error_code == expected_code


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cancel_does_not_hide_expired_unsafe_ambiguity(seed: JobSeed) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    project_scope = JobScope(seed.scope.project_id, None)
    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(session)
        job_id = await repository.enqueue(
            _retention_request(
                seed,
                "7" * 64,
                max_attempts=3,
                retry_safety="unsafe",
                available_at=started,
            )
        )
        claim = await repository.claim_next(
            worker_id=seed.worker_a,
            capabilities=frozenset({"retention_purge"}),
            lease_seconds=30,
            now=started,
        )
        assert claim is not None
        assert await repository.mark_running(job_id, lease_token=claim.lease_token, now=started)
        assert await repository.request_cancel(
            project_scope,
            job_id,
            reason="user_requested",
            now=started + timedelta(seconds=1),
        )

    async with seed.data.factory() as session, session.begin():
        assert (
            await JobRepository(session).claim_next(
                worker_id=seed.worker_b,
                capabilities=frozenset({"retention_purge"}),
                lease_seconds=30,
                now=started + timedelta(seconds=31),
            )
            is None
        )

    async with seed.data.factory() as session:
        job = await session.get(JobRow, job_id)
        dead = await session.get(DeadJobRow, job_id)
        attempt = (await session.execute(select(JobAttemptRow).where(JobAttemptRow.job_id == job_id))).scalar_one()
    assert job is not None and job.status == "dead"
    assert dead is not None and dead.public_error_code == "SIDE_EFFECT_STATE_UNKNOWN"
    assert attempt.outcome == "dead"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retry_backoff_then_exhaustion_writes_dead_projection(seed: JobSeed) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    request = _retention_request(
        seed,
        "1" * 64,
        max_attempts=2,
        available_at=started,
    )
    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(session)
        job_id = await repository.enqueue(request)
        first = await repository.claim_next(
            worker_id=seed.worker_a,
            capabilities=frozenset({"retention_purge"}),
            lease_seconds=90,
            now=started,
        )
        assert first is not None
        assert await repository.mark_running(job_id, lease_token=first.lease_token, now=started)
        assert await repository.retry_or_dead(
            job_id,
            lease_token=first.lease_token,
            public_error_code="TEMPORARY_FAILURE",
            retry_initial_seconds=2,
            retry_max_seconds=300,
            now=started,
        )
    async with seed.data.factory() as session:
        waiting = await session.get(JobRow, job_id)
        assert waiting is not None
        assert waiting.status == "retry_wait"
        assert waiting.available_at == started + timedelta(seconds=2)

    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(session)
        second = await repository.claim_next(
            worker_id=seed.worker_b,
            capabilities=frozenset({"retention_purge"}),
            lease_seconds=90,
            now=started + timedelta(seconds=2),
        )
        assert second is not None
        assert await repository.mark_running(job_id, lease_token=second.lease_token, now=started + timedelta(seconds=2))
        assert await repository.retry_or_dead(
            job_id,
            lease_token=second.lease_token,
            public_error_code="FINAL_FAILURE",
            retry_initial_seconds=2,
            retry_max_seconds=300,
            now=started + timedelta(seconds=2),
        )
    async with seed.data.factory() as session:
        job = await session.get(JobRow, job_id)
        dead = await session.get(DeadJobRow, job_id)
    assert job is not None and job.status == "dead"
    assert dead is not None and dead.public_error_code == "FINAL_FAILURE"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_private_dead_job_requires_and_stores_owner_hmac_before_transition(
    seed: JobSeed,
) -> None:
    run_id = await _create_private_run(seed)
    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(session)
        job_id = await repository.enqueue(_private_request(seed, run_id, "5" * 64, max_attempts=1))
        claim = await repository.claim_next(
            worker_id=seed.worker_a,
            capabilities=frozenset({"private_run"}),
            lease_seconds=90,
        )
        assert claim is not None
        assert await repository.mark_running(job_id, lease_token=claim.lease_token)
        with pytest.raises(JobOwnerRefRequired):
            await repository.retry_or_dead(
                job_id,
                lease_token=claim.lease_token,
                public_error_code="FINAL_FAILURE",
                retry_initial_seconds=2,
                retry_max_seconds=300,
            )
        current = await session.get(JobRow, job_id)
        assert current is not None and current.status == "running"

        protected = JobRepository(
            session,
            owner_ref_hasher=lambda _owner: JobOwnerRef(
                key_id="audit-v1",
                hmac_hex="f" * 64,
            ),
        )
        assert await protected.retry_or_dead(
            job_id,
            lease_token=claim.lease_token,
            public_error_code="FINAL_FAILURE",
            retry_initial_seconds=2,
            retry_max_seconds=300,
        )
    async with seed.data.factory() as session:
        dead = await session.get(DeadJobRow, job_id)
    assert dead is not None
    assert dead.owner_ref_key_id == "audit-v1"
    assert dead.owner_ref_hmac == "f" * 64


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_only_scope_cannot_list_or_requeue_private_dead_job(seed: JobSeed) -> None:
    run_id = await _create_private_run(seed)
    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(
            session,
            owner_ref_hasher=lambda _owner: JobOwnerRef(
                key_id="audit-v1",
                hmac_hex="f" * 64,
            ),
        )
        dead_job_id = await repository.enqueue(_private_request(seed, run_id, "8" * 64, max_attempts=1))
        claim = await repository.claim_next(
            worker_id=seed.worker_a,
            capabilities=frozenset({"private_run"}),
            lease_seconds=90,
        )
        assert claim is not None
        assert await repository.retry_or_dead(
            dead_job_id,
            lease_token=claim.lease_token,
            public_error_code="FINAL_FAILURE",
            retry_initial_seconds=2,
            retry_max_seconds=300,
        )

    project_only_scope = JobScope(seed.scope.project_id, None)
    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(session)
        assert await repository.list_dead(project_only_scope, limit=10) == ()
        with pytest.raises(JobRequeueForbidden):
            await repository.requeue_safe(
                project_only_scope,
                dead_job_id,
                idempotency_key="9" * 64,
                max_attempts=3,
                request_id="cross-owner-requeue",
                audit_port=_AuditPort(),
            )


class _AuditPort:
    def __init__(self) -> None:
        self.events: list[DeadJobRequeuedEvent] = []

    async def dead_job_requeued(self, _session, event: DeadJobRequeuedEvent) -> None:
        self.events.append(event)


class _FailingAuditPort:
    async def dead_job_requeued(self, _session, _event: DeadJobRequeuedEvent) -> None:
        raise RuntimeError("audit unavailable")


def test_dead_job_requeue_event_cannot_be_fabricated() -> None:
    with pytest.raises(TypeError):
        DeadJobRequeuedEvent(
            project_id=uuid.uuid4(),
            predecessor_job_id=uuid.uuid4(),
            successor_job_id=uuid.uuid4(),
            request_id="fabricated-system-requeue",
        )


def test_module_signer_cannot_fabricate_requeue_event_without_repository_state() -> None:
    signer = getattr(jobs_sql, "_issue_dead_job_requeued_event", None)
    if signer is None:
        return

    fabricated = signer(
        project_id=uuid.uuid4(),
        predecessor_job_id=uuid.uuid4(),
        successor_job_id=uuid.uuid4(),
        request_id="fabricated-without-repository",
        job_type="private_run",
        attempt_count=0,
        retry_safety="safe",
    )

    assert not consume_issued_dead_job_requeued_event(fabricated)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_safe_requeue_creates_successor_and_preserves_dead_projection(seed: JobSeed) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    request = _retention_request(
        seed,
        "2" * 64,
        max_attempts=1,
        available_at=started,
    )
    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(session)
        dead_job_id = await repository.enqueue(request)
        claim = await repository.claim_next(
            worker_id=seed.worker_a,
            capabilities=frozenset({"retention_purge"}),
            lease_seconds=90,
            now=started,
        )
        assert claim is not None
        assert await repository.mark_running(dead_job_id, lease_token=claim.lease_token, now=started)
        assert await repository.retry_or_dead(
            dead_job_id,
            lease_token=claim.lease_token,
            public_error_code="FAILED_ONCE",
            retry_initial_seconds=2,
            retry_max_seconds=300,
            now=started,
        )

    project_scope = JobScope(seed.scope.project_id, None)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        async with seed.data.factory() as session, session.begin():
            await JobRepository(session).requeue_safe(
                project_scope,
                dead_job_id,
                idempotency_key="6" * 64,
                max_attempts=3,
                request_id="request-audit-failure",
                audit_port=_FailingAuditPort(),
            )
    async with seed.data.factory() as session:
        rolled_back = await session.scalar(
            select(JobRow.id).where(
                JobRow.job_type == "retention_purge",
                JobRow.idempotency_key == "6" * 64,
            )
        )
    assert rolled_back is None

    audit = _AuditPort()
    async with seed.data.factory() as session, session.begin():
        before = await JobRepository(session).list_dead(project_scope, limit=10)
        successor = await JobRepository(session).requeue_safe(
            project_scope,
            dead_job_id,
            idempotency_key="3" * 64,
            max_attempts=3,
            request_id="request-requeue",
            audit_port=audit,
        )
        repeated = await JobRepository(session).requeue_safe(
            project_scope,
            dead_job_id,
            idempotency_key="3" * 64,
            max_attempts=3,
            request_id="request-requeue",
            audit_port=audit,
        )
        retried_with_different_key = await JobRepository(session).requeue_safe(
            project_scope,
            dead_job_id,
            idempotency_key="4" * 64,
            max_attempts=3,
            request_id="request-requeue-different-key",
            audit_port=audit,
        )
        after = await JobRepository(session).list_dead(project_scope, limit=10)

    assert successor != dead_job_id
    assert repeated == successor
    assert retried_with_different_key == successor
    assert before == after
    assert len(audit.events) == 1
    assert audit.events[0].predecessor_job_id == dead_job_id
    assert audit.events[0].successor_job_id == successor
    async with seed.data.factory() as session:
        successor_row = await session.get(JobRow, successor)
    assert successor_row is not None
    assert successor_row.predecessor_dead_job_id == dead_job_id
    assert successor_row.status == "queued"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_system_safe_requeue_concurrent_different_keys_adopt_one_successor_and_one_audit(seed: JobSeed) -> None:
    started = datetime(2026, 1, 2, tzinfo=UTC)
    request = _retention_request(
        seed,
        "a" * 64,
        max_attempts=1,
        available_at=started,
    )
    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(session)
        predecessor_id = await repository.enqueue(request)
        claim = await repository.claim_next(
            worker_id=seed.worker_a,
            capabilities=frozenset({"retention_purge"}),
            lease_seconds=90,
            now=started,
        )
        assert claim is not None
        assert await repository.mark_running(
            predecessor_id,
            lease_token=claim.lease_token,
            now=started,
        )
        assert await repository.retry_or_dead(
            predecessor_id,
            lease_token=claim.lease_token,
            public_error_code="FAILED_ONCE",
            retry_initial_seconds=2,
            retry_max_seconds=300,
            now=started,
        )

    audit = _AuditPort()

    async def requeue_once(idempotency_key: str) -> uuid.UUID:
        async with seed.data.factory() as session, session.begin():
            return await JobRepository(session).requeue_safe_system(
                seed.scope.project_id,
                predecessor_id,
                idempotency_key=idempotency_key,
                max_attempts=3,
                request_id=f"concurrent-system-requeue-{idempotency_key[:1]}",
                audit_port=audit,
            )

    first, second = await asyncio.gather(
        requeue_once("b" * 64),
        requeue_once("c" * 64),
    )

    assert first == second
    assert len(audit.events) == 1
    async with seed.data.factory() as session:
        successors = (await session.scalars(select(JobRow.id).where(JobRow.predecessor_dead_job_id == predecessor_id))).all()
    assert successors == [first]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_system_safe_requeue_rejects_private_predecessor_without_rebinding_run(
    seed: JobSeed,
) -> None:
    run_id = await _create_private_run(seed)
    async with seed.data.factory() as session, session.begin():
        repository = JobRepository(
            session,
            owner_ref_hasher=lambda _owner: JobOwnerRef(
                key_id="audit-v1",
                hmac_hex="f" * 64,
            ),
        )
        dead_job_id = await repository.enqueue(_private_request(seed, run_id, "a" * 64, max_attempts=1))
        claim = await repository.claim_next(
            worker_id=seed.worker_a,
            capabilities=frozenset({"private_run"}),
            lease_seconds=90,
        )
        assert claim is not None
        assert await repository.retry_or_dead(
            dead_job_id,
            lease_token=claim.lease_token,
            public_error_code="FINAL_FAILURE",
            retry_initial_seconds=2,
            retry_max_seconds=300,
        )

    audit = _AuditPort()
    with pytest.raises(JobRequeueForbidden):
        async with seed.data.factory() as session, session.begin():
            await JobRepository(session).requeue_safe_system(
                seed.scope.project_id,
                dead_job_id,
                idempotency_key="b" * 64,
                max_attempts=3,
                request_id="system-private-requeue",
                audit_port=audit,
            )

    assert audit.events == []
    async with seed.data.factory() as session:
        successors = await session.scalars(select(JobRow).where(JobRow.predecessor_dead_job_id == dead_job_id))
    assert tuple(successors) == ()

    assert run_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_system_safe_requeue_rejects_unsafe_and_predecessor_mismatch(
    seed: JobSeed,
) -> None:
    dead_ids: list[uuid.UUID] = []
    for index in range(3):
        async with seed.data.factory() as session, session.begin():
            repository = JobRepository(session)
            dead_job_id = await repository.enqueue(
                _retention_request(
                    seed,
                    str(index + 1) * 64,
                    max_attempts=1,
                    retry_safety="unknown" if index == 1 else "safe",
                )
            )
            claim = await repository.claim_next(
                worker_id=seed.worker_a,
                capabilities=frozenset({"retention_purge"}),
                lease_seconds=90,
            )
            assert claim is not None
            assert await repository.retry_or_dead(
                dead_job_id,
                lease_token=claim.lease_token,
                public_error_code="FINAL_FAILURE",
                retry_initial_seconds=2,
                retry_max_seconds=300,
            )
            dead_ids.append(dead_job_id)

    audit = _AuditPort()
    async with seed.data.factory() as session, session.begin():
        await JobRepository(session).requeue_safe_system(
            seed.scope.project_id,
            dead_ids[0],
            idempotency_key="d" * 64,
            max_attempts=3,
            request_id="system-safe-requeue",
            audit_port=audit,
        )

    with pytest.raises(JobRequeueForbidden):
        async with seed.data.factory() as session, session.begin():
            await JobRepository(session).requeue_safe_system(
                seed.scope.project_id,
                dead_ids[1],
                idempotency_key="e" * 64,
                max_attempts=3,
                request_id="system-unsafe-requeue",
                audit_port=audit,
            )

    with pytest.raises(JobIdempotencyConflict):
        async with seed.data.factory() as session, session.begin():
            await JobRepository(session).requeue_safe_system(
                seed.scope.project_id,
                dead_ids[2],
                idempotency_key="d" * 64,
                max_attempts=3,
                request_id="system-predecessor-mismatch",
                audit_port=audit,
            )
