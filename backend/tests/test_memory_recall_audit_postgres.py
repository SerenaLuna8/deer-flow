"""Durable per-Run boundary for content-free Memory recall audit rows."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from support.private_thread_seed import seed_private_thread_database

from app.audit.service import AuditService, _bind_worker_audit_process
from app.audit.sinks import OperationalAuditSink
from app.private_work.memory_authority import PrivateRunMemoryAuthority
from app.private_work.run_repository import (
    PrivateRunCreate,
    PrivateRunRepository,
)
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.config.memory_config import MemoryConfig
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.jobs.model import WorkerNodeRow
from deerflow.persistence.jobs.sql import EnqueueJob, JobRepository, JobScope
from deerflow.persistence.private_work.memory_document_model import MemoryEpisodeRow
from deerflow.persistence.private_work.memory_document_repository import (
    REMEMBER_RUN_LIMIT,
)


def _audit_sink(factory) -> OperationalAuditSink:
    service = AuditService(
        factory,
        AuditHmacKeyring(
            active_key_id="recall-test",
            _keys={"recall-test": b"r" * 32},
        ),
    )
    return OperationalAuditSink(
        service,
        process_context=_bind_worker_audit_process(service),
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_recall_search_stays_available_after_durable_run_audit_cap(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = seed.owner_a_scope
    thread_id = f"recall-audit-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex
    worker_id = uuid.uuid4()
    tagged_text = "- [durable] audit-cap-search-result"
    now = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await PrivateRunRepository(session).create(
                scope=scope,
                thread_id=thread_id,
                request=PrivateRunCreate(
                    run_id=run_id,
                    origin_trace_id=trace_id,
                ),
            )
            session.add(
                MemoryEpisodeRow(
                    id=uuid.uuid4(),
                    project_id=uuid.UUID(scope.project_id),
                    owner_user_id=scope.owner_user_id,
                    namespace="default",
                    thread_id=thread_id,
                    origin="snip",
                    tagged_text=tagged_text,
                    content_digest=hashlib.sha256(tagged_text.encode()).hexdigest(),
                    occurred_at=now,
                    consumed_dream_job_id=uuid.uuid4(),
                    created_at=now,
                )
            )
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="recall-audit-test",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=1,
                    draining=False,
                    started_at=now,
                    heartbeat_at=now,
                )
            )
            job_id = await JobRepository(session).enqueue(
                EnqueueJob(
                    job_type="private_run",
                    scope=JobScope(
                        uuid.UUID(scope.project_id),
                        scope.owner_user_id,
                    ),
                    idempotency_key=hashlib.sha256(f"recall-audit:{run_id}".encode()).hexdigest(),
                    run_id=run_id,
                    occurrence_id=None,
                    max_attempts=3,
                    retry_safety="safe",
                    origin_trace_id=trace_id,
                )
            )
            await PrivateRunRepository(session).attach_job(
                scope=scope,
                run_id=run_id,
                job_id=job_id,
            )

        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
            )
            assert claim is not None
            assert claim.job_id == job_id
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )
            await PrivateRunRepository(session).begin_execution(
                scope=scope,
                run_id=run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                origin_trace_id=claim.origin_trace_id,
            )

        authority = PrivateRunMemoryAuthority(
            seed.factory,
            context=seed.owner_a,
            claim=claim,
            thread_id=thread_id,
            namespace="default",
            memory_config=MemoryConfig(
                enabled=True,
                max_injection_tokens=2_000,
            ),
            audit=_audit_sink(seed.factory),
        )

        async def search() -> None:
            records = await authority.search_episodes(query="audit-cap-search-result")
            assert records is not None
            assert [record.tagged_text for record in records] == [tagged_text]

        for _ in range(REMEMBER_RUN_LIMIT - 1):
            await search()
        # Both calls start with one slot left. The durable Job/Run lock must
        # serialize the count+append boundary without suppressing either
        # search result.
        await asyncio.gather(search(), search())
        await search()

        async with seed.factory() as session:
            rows = tuple(
                (
                    await session.execute(
                        sa.select(AuditLogRow)
                        .where(
                            AuditLogRow.project_id == uuid.UUID(scope.project_id),
                            AuditLogRow.job_id == job_id,
                            AuditLogRow.action == "memory.recall.executed",
                        )
                        .order_by(AuditLogRow.occurred_at, AuditLogRow.id)
                    )
                ).scalars()
            )
        assert len(rows) == REMEMBER_RUN_LIMIT
        assert all(row.target_kind == "run" for row in rows)
        assert all(
            set(row.metadata_json)
            == {
                "result_bucket",
                "matched_stage",
                "tags_filtered",
                "query_len_bucket",
            }
            for row in rows
        )
    finally:
        await seed.engine.dispose()
