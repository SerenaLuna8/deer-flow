from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from support.memory_v2_seed import admit_memory_extraction_job
from support.private_thread_seed import seed_private_thread_database

from app.reliability.workers import WorkerRegistry
from app.worker.memory_extract import MemoryExtractJobHandler
from app.worker.service import (
    JobLeaseAuthority,
    JobOutcome,
    JobSettlement,
    LeaseLost,
    WorkerService,
)
from deerflow.agents.memory.extractor import (
    ExtractedMemoryCandidate,
    MemoryExtractionResult,
)
from deerflow.agents.memory.storage import ProjectMemoryStorage
from deerflow.config.worker_config import WorkerConfig
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.persistence.private_work.memory_v2_repository import (
    MemoryCandidateDraft,
    MemoryV2Repository,
    prepare_memory_candidate_writes,
)


class _PolicyMaterializer:
    def __init__(self, *, mode: str = "shadow", model_purpose: str = "lead") -> None:
        self._policy = SimpleNamespace(
            memory=SimpleNamespace(
                enabled=True,
                pipeline_mode=mode,
                model_name=("memory-model" if model_purpose == "memory" else None),
            )
        )

    async def materialize_run_snapshot(self, **_kwargs):
        return self._policy


class _ModelMaterializer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def materialize_snapshot(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(name="memory-pr4-test")


class _FailOnceModelMaterializer(_ModelMaterializer):
    async def materialize_snapshot(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise RuntimeError("injected pre-model failure")
        return SimpleNamespace(name="memory-pr4-test")


class _Extractor:
    def __init__(self, results: list[MemoryExtractionResult]) -> None:
        self._results = list(results)
        self.calls = 0

    async def extract(self, _sources) -> MemoryExtractionResult:
        self.calls += 1
        return self._results.pop(0)


class _FailAfterFinalizeRepository(MemoryV2Repository):
    async def finalize_extraction(self, **kwargs):
        await super().finalize_extraction(**kwargs)
        raise RuntimeError("injected Candidate commit failure")


def _result(*candidates: ExtractedMemoryCandidate) -> MemoryExtractionResult:
    return MemoryExtractionResult(candidates=tuple(candidates))


def _candidate(
    source_ordinal: int,
    content: str,
    *,
    candidate_type: str = "preference",
) -> ExtractedMemoryCandidate:
    return ExtractedMemoryCandidate(
        source_ordinal=source_ordinal,
        candidate_type=candidate_type,
        content=content,
        confidence=0.97,
        retention_class="durable",
        sensitivity="normal",
    )


def _handler(
    seed,
    extractor: _Extractor,
    *,
    model_purpose: str = "lead",
    repository_builder=MemoryV2Repository,
) -> tuple[MemoryExtractJobHandler, _ModelMaterializer]:
    materializer = _ModelMaterializer()
    return (
        MemoryExtractJobHandler(
            seed.factory,
            app_config=None,
            model_materializer=materializer,
            runtime_policy_materializer=_PolicyMaterializer(
                model_purpose=model_purpose,
            ),
            extractor_factory=lambda _model: extractor,
            repository_builder=repository_builder,
        ),
        materializer,
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_extract_atomically_commits_traceable_candidates_without_v1_recall(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        _admitted, claim = await admit_memory_extraction_job(
            seed,
            messages=[
                {"role": "user", "id": "source-1", "content": "项目统一使用 PostgreSQL。"},
                {"role": "user", "id": "source-2", "content": "我偏好简洁回答。"},
            ],
        )
        extractor = _Extractor(
            [
                _result(
                    _candidate(
                        1,
                        "用户偏好简洁回答。",
                    ),
                    _candidate(
                        0,
                        "项目统一使用 PostgreSQL。",
                        candidate_type="constraint",
                    ),
                )
            ]
        )
        handler, materializer = _handler(seed, extractor)
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        assert isinstance(settlement, JobSettlement)
        await settlement.commit()

        async with seed.factory() as session:
            generation_id = await session.scalar(
                text("SELECT id FROM memory_extraction_generations WHERE job_id=:job"),
                {"job": claim.job_id},
            )
            rows = (
                await session.execute(
                    text(
                        """SELECT c.id,c.ordinal,c.candidate_type,c.content,
                                  c.content_digest,c.confidence,c.retention_class,
                                  c.sensitivity,c.status,i.source_message_id
                           FROM memory_candidates c
                           JOIN memory_source_items i ON i.id=c.source_item_id
                           ORDER BY c.ordinal"""
                    )
                )
            ).all()
            state = (
                await session.execute(
                    text(
                        """SELECT j.status,a.outcome,
                                  g.candidate_committed_at IS NOT NULL
                           FROM jobs j
                           JOIN job_attempts a ON a.job_id=j.id
                           JOIN memory_extraction_generations g ON g.job_id=j.id
                           WHERE j.id=:job"""
                    ),
                    {"job": claim.job_id},
                )
            ).one()

        expected = prepare_memory_candidate_writes(
            generation_id,
            (
                MemoryCandidateDraft(
                    source_ordinal=1,
                    candidate_type="preference",
                    content="用户偏好简洁回答。",
                    confidence=0.97,
                    retention_class="durable",
                    sensitivity="normal",
                ),
                MemoryCandidateDraft(
                    source_ordinal=0,
                    candidate_type="constraint",
                    content="项目统一使用 PostgreSQL。",
                    confidence=0.97,
                    retention_class="durable",
                    sensitivity="normal",
                ),
            ),
        )
        assert [row.id for row in rows] == [item.id for item in expected]
        assert [row.source_message_id for row in rows] == ["source-1", "source-2"]
        assert [row.status for row in rows] == ["pending", "pending"]
        assert tuple(state) == ("succeeded", "succeeded", True)
        assert extractor.calls == 1
        assert materializer.calls[0]["purpose"] == "lead"

        legacy = await ProjectMemoryStorage(seed.factory).load(
            scope=seed.owner_a_scope,
            namespace="default",
        )
        assert legacy.memory["facts"] == []
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_empty_extraction_completes_generation_with_explicit_memory_model(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        _admitted, claim = await admit_memory_extraction_job(
            seed,
            messages=[{"role": "user", "content": "帮我检查这段代码。"}],
            model_purpose="memory",
        )
        extractor = _Extractor([_result()])
        handler, materializer = _handler(
            seed,
            extractor,
            model_purpose="memory",
        )
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT COUNT(*) FROM memory_candidates),
                           (SELECT COUNT(*) FROM memory_extraction_generations
                            WHERE candidate_committed_at IS NOT NULL),
                           (SELECT COUNT(*) FROM jobs
                            WHERE id=:job AND status='succeeded')"""
                    ),
                    {"job": claim.job_id},
                )
            ).one()
        assert tuple(state) == (0, 1, 1)
        assert materializer.calls[0]["purpose"] == "memory"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pre_model_failure_retries_and_next_attempt_commits_once(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        _admitted, first_claim = await admit_memory_extraction_job(
            seed,
            messages=[{"role": "user", "content": "我偏好中文。"}],
        )
        extractor = _Extractor([_result(_candidate(0, "用户偏好中文。"))])
        materializer = _FailOnceModelMaterializer()
        handler = MemoryExtractJobHandler(
            seed.factory,
            app_config=None,
            model_materializer=materializer,
            runtime_policy_materializer=_PolicyMaterializer(),
            extractor_factory=lambda _model: extractor,
        )

        first_outcome = await handler(
            first_claim,
            JobLeaseAuthority(seed.factory, first_claim, lease_seconds=90),
        )
        assert first_outcome == JobOutcome.failed(
            "MEMORY_EXTRACT_MODEL_UNAVAILABLE",
        )
        service = WorkerService(
            seed.factory,
            SimpleNamespace(),
            {"memory_extract": handler},
            WorkerConfig(retry_initial_seconds=1, retry_max_seconds=1),
        )
        await service._settle(first_claim, first_outcome)

        async with seed.factory() as session, session.begin():
            await session.execute(
                text("UPDATE jobs SET available_at=now() WHERE id=:job"),
                {"job": first_claim.job_id},
            )
        second_worker_id = uuid.uuid4()
        await WorkerRegistry(seed.factory, version="memory-pr4-retry").register(
            second_worker_id,
            frozenset({"memory_extract"}),
            1,
        )
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            second_claim = await jobs.claim_next(
                worker_id=second_worker_id,
                capabilities=frozenset({"memory_extract"}),
                lease_seconds=90,
            )
            assert second_claim is not None
            assert second_claim.job_id == first_claim.job_id
            assert await jobs.mark_running(
                second_claim.job_id,
                lease_token=second_claim.lease_token,
            )

        second_settlement = await handler(
            second_claim,
            JobLeaseAuthority(seed.factory, second_claim, lease_seconds=90),
        )
        assert isinstance(second_settlement, JobSettlement)
        await second_settlement.commit()

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT j.status,j.attempt_count,
                                  (SELECT COUNT(*) FROM memory_candidates)
                           FROM jobs j WHERE j.id=:job"""
                    ),
                    {"job": first_claim.job_id},
                )
            ).one()
            outcomes = tuple(
                (
                    await session.execute(
                        text(
                            """SELECT outcome FROM job_attempts
                               WHERE job_id=:job ORDER BY attempt_number"""
                        ),
                        {"job": first_claim.job_id},
                    )
                ).scalars()
            )
        assert tuple(state) == ("succeeded", 2, 1)
        assert outcomes == ("retry", "succeeded")
        assert len(materializer.calls) == 2
        assert extractor.calls == 1
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_new_lease_wins_after_model_recall_and_old_settlement_cannot_commit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        _admitted, first_claim = await admit_memory_extraction_job(
            seed,
            messages=[{"role": "user", "content": "我使用 Python。"}],
        )
        first_extractor = _Extractor([_result(_candidate(0, "用户使用 Python。"))])
        first_handler, _materializer = _handler(seed, first_extractor)
        first_settlement = await first_handler(
            first_claim,
            JobLeaseAuthority(seed.factory, first_claim, lease_seconds=90),
        )

        async with seed.factory() as session, session.begin():
            await session.execute(
                text("UPDATE jobs SET lease_expires_at=now()-interval '1 second' WHERE id=:job"),
                {"job": first_claim.job_id},
            )
        second_worker_id = uuid.uuid4()
        await WorkerRegistry(seed.factory, version="memory-pr4-retry").register(
            second_worker_id,
            frozenset({"memory_extract"}),
            1,
        )
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            second_claim = await jobs.claim_next(
                worker_id=second_worker_id,
                capabilities=frozenset({"memory_extract"}),
                lease_seconds=90,
            )
            assert second_claim is not None
            assert second_claim.job_id == first_claim.job_id
            assert await jobs.mark_running(
                second_claim.job_id,
                lease_token=second_claim.lease_token,
            )

        second_extractor = _Extractor([_result(_candidate(0, "用户的主要语言是 Python。"))])
        second_handler, _materializer = _handler(seed, second_extractor)
        second_settlement = await second_handler(
            second_claim,
            JobLeaseAuthority(seed.factory, second_claim, lease_seconds=90),
        )
        await second_settlement.commit()
        with pytest.raises(LeaseLost):
            await first_settlement.commit()

        async with seed.factory() as session:
            contents = tuple((await session.execute(text("SELECT content FROM memory_candidates"))).scalars())
            outcomes = tuple(
                (
                    await session.execute(
                        text("SELECT outcome FROM job_attempts WHERE job_id=:job ORDER BY attempt_number"),
                        {"job": first_claim.job_id},
                    )
                ).scalars()
            )
        assert contents == ("用户的主要语言是 Python。",)
        assert outcomes == ("lease_lost", "succeeded")
        assert first_extractor.calls == 1
        assert second_extractor.calls == 1
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_suppression_after_model_call_discards_output_and_cancels_job(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        _admitted, claim = await admit_memory_extraction_job(
            seed,
            messages=[{"role": "user", "content": "我偏好中文。"}],
        )
        extractor = _Extractor([_result(_candidate(0, "用户偏好中文。"))])
        handler, _materializer = _handler(seed, extractor)
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )

        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE memory_source_batches
                       SET suppressed_at=now(),suppression_reason='hard_forget'"""
                )
            )
            await session.execute(
                text(
                    """UPDATE memory_source_items
                       SET content=NULL,source_erased_at=now()"""
                )
            )
        await settlement.commit()

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT COUNT(*) FROM memory_candidates),
                           (SELECT COUNT(*) FROM memory_extraction_generations
                            WHERE candidate_committed_at IS NOT NULL),
                           (SELECT COUNT(*) FROM jobs
                            WHERE id=:job AND status='cancelled')"""
                    ),
                    {"job": claim.job_id},
                )
            ).one()
        assert tuple(state) == (0, 0, 1)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cancel_requested_after_model_call_discards_output(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        _admitted, claim = await admit_memory_extraction_job(
            seed,
            messages=[{"role": "user", "content": "我偏好中文。"}],
        )
        extractor = _Extractor([_result(_candidate(0, "用户偏好中文。"))])
        handler, _materializer = _handler(seed, extractor)
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )

        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE jobs
                       SET cancel_requested_at=now(),cancel_reason='user_request'
                       WHERE id=:job"""
                ),
                {"job": claim.job_id},
            )
        await settlement.commit()

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT COUNT(*) FROM memory_candidates),
                           (SELECT COUNT(*) FROM memory_extraction_generations
                            WHERE candidate_committed_at IS NOT NULL),
                           (SELECT COUNT(*) FROM jobs
                            WHERE id=:job AND status='cancelled')"""
                    ),
                    {"job": claim.job_id},
                )
            ).one()
        assert tuple(state) == (0, 0, 1)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_lease_expiring_while_commit_waits_for_lock_cannot_commit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        _admitted, claim = await admit_memory_extraction_job(
            seed,
            messages=[{"role": "user", "content": "我使用 Go。"}],
        )
        extractor = _Extractor([_result(_candidate(0, "用户使用 Go。"))])
        handler, _materializer = _handler(seed, extractor)
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )

        async with seed.factory() as lock_session, lock_session.begin():
            await lock_session.execute(
                text(
                    """SELECT id FROM memory_extraction_generations
                       WHERE job_id=:job FOR UPDATE"""
                ),
                {"job": claim.job_id},
            )
            commit_task = asyncio.create_task(settlement.commit())
            await asyncio.sleep(0.05)
            async with seed.factory() as session, session.begin():
                await session.execute(
                    text(
                        """UPDATE jobs
                           SET lease_expires_at=now()-interval '1 millisecond'
                           WHERE id=:job"""
                    ),
                    {"job": claim.job_id},
                )

        with pytest.raises(LeaseLost):
            await commit_task
        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT COUNT(*) FROM memory_candidates),
                           (SELECT COUNT(*) FROM memory_extraction_generations
                            WHERE candidate_committed_at IS NOT NULL),
                           (SELECT COUNT(*) FROM jobs
                            WHERE id=:job AND status='running'),
                           (SELECT COUNT(*) FROM job_attempts
                            WHERE job_id=:job AND outcome IS NULL)"""
                    ),
                    {"job": claim.job_id},
                )
            ).one()
        assert tuple(state) == (0, 0, 1, 1)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_candidate_failure_rolls_back_candidate_marker_and_job_settlement(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        _admitted, claim = await admit_memory_extraction_job(
            seed,
            messages=[{"role": "user", "content": "我使用 Rust。"}],
        )
        extractor = _Extractor([_result(_candidate(0, "用户使用 Rust。"))])
        handler, _materializer = _handler(
            seed,
            extractor,
            repository_builder=_FailAfterFinalizeRepository,
        )
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        with pytest.raises(RuntimeError, match="injected Candidate commit failure"):
            await settlement.commit()

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT COUNT(*) FROM memory_candidates),
                           (SELECT COUNT(*) FROM memory_extraction_generations
                            WHERE candidate_committed_at IS NOT NULL),
                           (SELECT COUNT(*) FROM jobs
                            WHERE id=:job AND status='running'),
                           (SELECT COUNT(*) FROM job_attempts
                            WHERE job_id=:job AND outcome IS NULL)"""
                    ),
                    {"job": claim.job_id},
                )
            ).one()
        assert tuple(state) == (0, 0, 1, 1)
    finally:
        await seed.engine.dispose()
