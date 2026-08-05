from __future__ import annotations

import asyncio
import hashlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from support.memory_v2_seed import admit_memory_extraction_job
from support.private_thread_seed import seed_private_thread_database

from app.private_work.memory_service import build_memory_consolidation_contract
from app.reliability.workers import WorkerRegistry
from app.scheduler.memory import (
    MemoryMaintenanceSchedulerService,
    resolve_memory_consolidation_runtime,
)
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import AgentRuntimePolicyValue
from app.worker.memory_consolidate import (
    MemoryConsolidateJobHandler,
    MemoryRetentionPurgeJobHandler,
)
from app.worker.memory_extract import MemoryExtractJobHandler
from app.worker.service import JobLeaseAuthority, JobOutcome, JobSettlement, LeaseLost
from deerflow.agents.memory.consolidator import (
    MemoryConsolidationDecision,
    MemoryConsolidationResult,
)
from deerflow.agents.memory.extractor import (
    ExtractedMemoryCandidate,
    MemoryExtractionResult,
)
from deerflow.agents.memory.storage import ProjectMemoryStorage
from deerflow.persistence.jobs.sql import JobOwnerRef, JobRepository
from deerflow.persistence.private_work.memory_v2_repository import MemoryV2Repository


class _ExtractionPolicy:
    async def materialize_run_snapshot(self, **_kwargs):
        return SimpleNamespace(
            memory=SimpleNamespace(
                enabled=True,
                pipeline_mode="consolidate",
                model_name="memory-test",
            )
        )


class _ExtractionModel:
    async def materialize_snapshot(self, **_kwargs):
        return SimpleNamespace(name="memory-test")


class _ExactModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def materialize_exact(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(name="memory-test")


class _RetentionPolicy:
    async def materialize_current(self, _section):
        base = AgentRuntimePolicyValue()
        return base.model_copy(
            update={
                "memory": base.memory.model_copy(
                    update={
                        "enabled": True,
                        "pipeline_mode": "consolidate",
                        "candidate_retention_days": 1,
                    }
                )
            }
        )

    async def materialize_current_in_session(
        self,
        _session,
        section,
        *,
        for_update=False,
    ):
        assert for_update is True
        return await self.materialize_current(section)


class _Extractor:
    def __init__(self, contents: tuple[str, ...]) -> None:
        self._contents = contents

    async def extract(self, _sources) -> MemoryExtractionResult:
        return MemoryExtractionResult(
            candidates=tuple(
                ExtractedMemoryCandidate(
                    source_ordinal=ordinal,
                    candidate_type="preference",
                    content=content,
                    confidence=0.95,
                    retention_class="durable",
                    sensitivity="normal",
                )
                for ordinal, content in enumerate(self._contents)
            )
        )


class _Consolidator:
    def __init__(self, decide) -> None:
        self._decide = decide
        self.calls = 0

    async def consolidate(self, candidates, facts) -> MemoryConsolidationResult:
        self.calls += 1
        return MemoryConsolidationResult(decisions=tuple(self._decide(candidate, facts) for candidate in candidates))


def _job_repository(session) -> JobRepository:
    return JobRepository(
        session,
        owner_ref_hasher=lambda owner: JobOwnerRef(
            key_id="memory-pr5-test",
            hmac_hex=hashlib.sha256(owner.encode()).hexdigest(),
        ),
    )


async def _seed_candidates(
    seed,
    contents: tuple[str, ...],
    *,
    age_for_scheduler: bool = True,
) -> None:
    _admitted, claim = await admit_memory_extraction_job(
        seed,
        messages=[
            {
                "role": "user",
                "id": f"source-{uuid.uuid4()}",
                "content": content,
            }
            for content in contents
        ],
        mode="consolidate",
        model_purpose="memory",
        make_policy_current=True,
    )
    handler = MemoryExtractJobHandler(
        seed.factory,
        app_config=None,
        model_materializer=_ExtractionModel(),
        runtime_policy_materializer=_ExtractionPolicy(),
        extractor_factory=lambda _model: _Extractor(contents),
    )
    settlement = await handler(
        claim,
        JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
    )
    assert isinstance(settlement, JobSettlement)
    await settlement.commit()
    if age_for_scheduler:
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE memory_candidates
                    SET created_at=now()-interval '121 minutes'
                    WHERE status='pending' AND consolidation_generation_id IS NULL"""
                )
            )


async def _admit_memory_jobs(seed):
    async with seed.factory() as session, session.begin():
        return await MemoryMaintenanceSchedulerService(
            runtime_resolver=resolve_memory_consolidation_runtime,
        ).admit_due(session, now=await session.scalar(text("SELECT now()")))


async def _claim(seed, job_type: str):
    worker_id = uuid.uuid4()
    await WorkerRegistry(seed.factory, version=f"memory-pr5-{job_type}").register(
        worker_id,
        frozenset({job_type}),
        1,
    )
    async with seed.factory() as session, session.begin():
        jobs = JobRepository(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({job_type}),
            lease_seconds=90,
        )
        assert claim is not None
        assert await jobs.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
        )
        return claim


def _create_decision(candidate, _facts) -> MemoryConsolidationDecision:
    return MemoryConsolidationDecision(
        candidate_id=candidate.id,
        action="create",
        target_fact_id=None,
        content=candidate.content,
        category="preference",
        confidence=0.95,
        change_reason="new_fact",
        decision_reason=None,
    )


async def _run_consolidation(seed, decide) -> _Consolidator:
    claim = await _claim(seed, "memory_consolidate")
    consolidator = _Consolidator(decide)
    handler = MemoryConsolidateJobHandler(
        seed.factory,
        app_config=None,
        model_materializer=_ExactModel(),
        runtime_policy_materializer=SystemRuntimePolicyMaterializer(seed.factory),
        consolidator_factory=lambda _model: consolidator,
    )
    settlement = await handler(
        claim,
        JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
    )
    assert isinstance(settlement, JobSettlement)
    await settlement.commit()
    return consolidator


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scheduler_admits_one_idempotent_bounded_job_per_scope(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(
            seed,
            tuple(f"稳定偏好 {index}" for index in range(21)),
        )

        first = await _admit_memory_jobs(seed)
        second = await _admit_memory_jobs(seed)

        assert first.consolidation_jobs == 1
        assert second.consolidation_jobs == 0
        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM jobs
                         WHERE job_type='memory_consolidate'),
                        (SELECT count(*) FROM memory_consolidation_generations),
                        (SELECT count(*) FROM memory_candidates
                         WHERE consolidation_generation_id IS NOT NULL),
                        (SELECT count(*) FROM memory_candidates
                         WHERE consolidation_generation_id IS NULL)"""
                    )
                )
            ).one()
        assert tuple(state) == (1, 1, 20, 1)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_dream_immediately_admits_exact_scope_only_once(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(
            seed,
            ("无需等待定时间隔的长期偏好",),
            age_for_scheduler=False,
        )
        async with seed.factory() as session:
            runs_before = await session.scalar(text("SELECT count(*) FROM runs"))

        async def admit_once():
            async with seed.factory() as session, session.begin():
                runtime = await resolve_memory_consolidation_runtime(session)
                contract = build_memory_consolidation_contract(runtime)
                assert contract is not None
                return await MemoryV2Repository(session).admit_consolidation_for_scope(
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    namespace="default",
                    contract=contract,
                    now=await session.scalar(text("SELECT now()")),
                )

        outcomes = await asyncio.gather(admit_once(), admit_once())

        assert sorted(outcome.disposition for outcome in outcomes) == [
            "already_running",
            "queued",
        ]
        queued = next(outcome for outcome in outcomes if outcome.disposition == "queued")
        running = next(outcome for outcome in outcomes if outcome.disposition == "already_running")
        assert queued.candidate_count == running.candidate_count == 1
        assert queued.job_id == running.job_id
        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM jobs
                         WHERE job_type='memory_consolidate'),
                        (SELECT count(*) FROM memory_consolidation_generations),
                        (SELECT count(*) FROM runs)"""
                    )
                )
            ).one()
        assert tuple(state) == (1, 1, runs_before)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scheduler_recovers_dead_generation_with_frozen_contract(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(seed, ("需要在故障后继续整理",))
        admitted = await _admit_memory_jobs(seed)
        assert admitted.consolidation_jobs == 1
        predecessor = await _claim(seed, "memory_consolidate")
        async with seed.factory() as session, session.begin():
            result = await _job_repository(session).retry_or_dead_result(
                predecessor.job_id,
                lease_token=predecessor.lease_token,
                public_error_code="MEMORY_CONSOLIDATE_MODEL_UNAVAILABLE",
                retryable=False,
                retry_initial_seconds=1,
                retry_max_seconds=1,
            )
            assert result.changed

        recovered = await _admit_memory_jobs(seed)
        assert recovered.consolidation_jobs == 1
        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT predecessor.status,successor.status,
                                  successor.predecessor_dead_job_id,
                                  generation.job_id,candidate.status
                           FROM memory_consolidation_generations generation
                           JOIN jobs successor ON successor.id=generation.job_id
                           JOIN jobs predecessor
                             ON predecessor.id=successor.predecessor_dead_job_id
                           JOIN memory_candidates candidate
                             ON candidate.consolidation_generation_id=generation.id"""
                    )
                )
            ).one()
        assert state[0] == "dead"
        assert state[1] == "queued"
        assert state[2] == predecessor.job_id
        assert state[3] != predecessor.job_id
        assert state[4] == "pending"

        await _run_consolidation(seed, _create_decision)
        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM memory_facts),
                        (SELECT count(*) FROM memory_fact_revisions),
                        (SELECT count(*) FROM memory_fact_evidence),
                        (SELECT count(*) FROM memory_candidates
                         WHERE status='accepted')"""
                    )
                )
            ).one()
        assert tuple(counts) == (1, 1, 1, 1)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scheduler_limits_transient_dead_recovery_to_one_successor(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(seed, ("永久故障不会无限创建接续任务",))
        await _admit_memory_jobs(seed)
        predecessor = await _claim(seed, "memory_consolidate")
        async with seed.factory() as session, session.begin():
            result = await _job_repository(session).retry_or_dead_result(
                predecessor.job_id,
                lease_token=predecessor.lease_token,
                public_error_code="MEMORY_CONSOLIDATE_MODEL_UNAVAILABLE",
                retryable=False,
                retry_initial_seconds=1,
                retry_max_seconds=1,
            )
            assert result.changed

        first_recovery = await _admit_memory_jobs(seed)
        assert first_recovery.consolidation_jobs == 1
        successor = await _claim(seed, "memory_consolidate")
        async with seed.factory() as session, session.begin():
            result = await _job_repository(session).retry_or_dead_result(
                successor.job_id,
                lease_token=successor.lease_token,
                public_error_code="MEMORY_CONSOLIDATE_MODEL_UNAVAILABLE",
                retryable=False,
                retry_initial_seconds=1,
                retry_max_seconds=1,
            )
            assert result.changed

        second_recovery = await _admit_memory_jobs(seed)
        assert second_recovery.consolidation_jobs == 0
        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM jobs
                         WHERE job_type='memory_consolidate'),
                        (SELECT count(*) FROM memory_consolidation_generations),
                        (SELECT count(*) FROM memory_candidates
                         WHERE status='pending'
                           AND consolidation_generation_id IS NOT NULL),
                        successor.status,
                        successor.predecessor_dead_job_id
                        FROM memory_consolidation_generations generation
                        JOIN jobs successor ON successor.id=generation.job_id"""
                    )
                )
            ).one()
        assert tuple(state[:4]) == (2, 1, 1, "dead")
        assert state[4] == predecessor.job_id
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scheduler_does_not_loop_deterministic_dead_generation(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(seed, ("确定性错误等待人工处理",))
        await _admit_memory_jobs(seed)
        predecessor = await _claim(seed, "memory_consolidate")
        async with seed.factory() as session, session.begin():
            result = await _job_repository(session).retry_or_dead_result(
                predecessor.job_id,
                lease_token=predecessor.lease_token,
                public_error_code="MEMORY_CONSOLIDATE_CONTRACT_UNSUPPORTED",
                retryable=False,
                retry_initial_seconds=1,
                retry_max_seconds=1,
            )
            assert result.changed

        recovered = await _admit_memory_jobs(seed)
        assert recovered.consolidation_jobs == 0
        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM jobs
                         WHERE job_type='memory_consolidate'),
                        (SELECT count(*) FROM memory_consolidation_generations),
                        (SELECT count(*) FROM memory_candidates
                         WHERE status='pending'
                           AND consolidation_generation_id IS NOT NULL),
                        (SELECT status FROM jobs WHERE id=:job)"""
                    ),
                    {"job": predecessor.job_id},
                )
            ).one()
        assert tuple(state) == (1, 1, 1, "dead")
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scope_restore_recovers_original_generation(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(seed, ("权限恢复后继续原整理任务",))
        await _admit_memory_jobs(seed)
        predecessor = await _claim(seed, "memory_consolidate")
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE project_memberships
                    SET role='viewer'
                    WHERE project_id=:project AND user_id=:owner"""
                ),
                {
                    "project": seed.owner_a.project_id,
                    "owner": str(seed.owner_a.user_id),
                },
            )

        handler = MemoryConsolidateJobHandler(
            seed.factory,
            app_config=None,
            model_materializer=_ExactModel(),
            runtime_policy_materializer=SystemRuntimePolicyMaterializer(
                seed.factory,
            ),
            consolidator_factory=lambda _model: _Consolidator(
                _create_decision,
            ),
        )
        outcome = await handler(
            predecessor,
            JobLeaseAuthority(seed.factory, predecessor, lease_seconds=90),
        )
        assert isinstance(outcome, JobOutcome)
        assert outcome.status == "failed"
        assert outcome.public_error_code == "MEMORY_CONSOLIDATE_SCOPE_UNAVAILABLE"
        async with seed.factory() as session, session.begin():
            result = await _job_repository(session).retry_or_dead_result(
                predecessor.job_id,
                lease_token=predecessor.lease_token,
                public_error_code=outcome.public_error_code,
                retryable=False,
                retry_initial_seconds=1,
                retry_max_seconds=1,
            )
            assert result.changed

        assert (await _admit_memory_jobs(seed)).consolidation_jobs == 0
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE project_memberships
                    SET role='admin'
                    WHERE project_id=:project AND user_id=:owner"""
                ),
                {
                    "project": seed.owner_a.project_id,
                    "owner": str(seed.owner_a.user_id),
                },
            )

        assert (await _admit_memory_jobs(seed)).consolidation_jobs == 1
        await _run_consolidation(seed, _create_decision)
        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM memory_facts),
                        (SELECT count(*) FROM memory_candidates
                         WHERE status='accepted'),
                        (SELECT count(*) FROM jobs
                         WHERE job_type='memory_consolidate')"""
                    )
                )
            ).one()
        assert tuple(state) == (1, 1, 2)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_same_batch_duplicate_creates_one_fact_with_two_evidence(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(
            seed,
            ("偏好使用中文回答", "请一直用中文回复"),
        )
        admitted = await _admit_memory_jobs(seed)
        assert admitted.consolidation_jobs == 1

        def normalize_duplicate(candidate, _facts):
            return MemoryConsolidationDecision(
                candidate.id,
                "create",
                None,
                "用户偏好中文回答。",
                "preference",
                0.95,
                "new_fact",
                None,
            )

        await _run_consolidation(seed, normalize_duplicate)

        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM memory_facts),
                        (SELECT count(*) FROM memory_fact_revisions),
                        (SELECT count(*) FROM memory_fact_evidence),
                        (SELECT count(*) FROM memory_candidates
                         WHERE status='accepted')"""
                    )
                )
            ).one()
        assert tuple(counts) == (1, 1, 2, 2)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_same_batch_confirm_and_revisions_are_order_independent(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(seed, ("初始事实",))
        await _admit_memory_jobs(seed)
        await _run_consolidation(seed, _create_decision)

        await _seed_candidates(
            seed,
            ("第一处纠正", "再次确认旧事实", "第二处纠正"),
        )
        await _admit_memory_jobs(seed)

        def decide(candidate, facts):
            fact = facts[0]
            if candidate.content == "再次确认旧事实":
                return MemoryConsolidationDecision(
                    candidate.id,
                    "confirm",
                    fact.id,
                    None,
                    None,
                    None,
                    None,
                    "same_fact",
                )
            return MemoryConsolidationDecision(
                candidate.id,
                "revise",
                fact.id,
                candidate.content,
                "preference",
                0.95,
                "correction",
                None,
            )

        await _run_consolidation(seed, decide)

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT f.version,r.content,
                        (SELECT count(*) FROM memory_fact_revisions),
                        (SELECT count(*) FROM memory_fact_evidence),
                        (SELECT count(*) FROM memory_candidates
                         WHERE status='pending')
                        FROM memory_facts f
                        JOIN memory_fact_revisions r
                          ON r.id=f.current_revision_id"""
                    )
                )
            ).one()
        assert state[0] == 2
        assert state[1] in {"第一处纠正", "第二处纠正"}
        assert tuple(state[2:]) == (2, 3, 1)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retention_uses_admitted_cutoff_after_policy_changes(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(seed, ("已到期候选", "尚未到期候选"))
        await _admit_memory_jobs(seed)
        await _run_consolidation(seed, _create_decision)
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE memory_candidates
                    SET decided_at=CASE content
                        WHEN '已到期候选' THEN now()-interval '31 days'
                        ELSE now()-interval '5 days'
                    END
                    WHERE status='accepted'"""
                )
            )

        admitted = await _admit_memory_jobs(seed)
        assert admitted.retention_jobs == 1
        claim = await _claim(seed, "memory_retention_purge")
        handler = MemoryRetentionPurgeJobHandler(
            seed.factory,
            runtime_policy_materializer=_RetentionPolicy(),
        )
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        assert isinstance(settlement, JobSettlement)
        await settlement.commit()

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                        count(*) FILTER (WHERE content IS NULL),
                        count(*) FILTER (WHERE content IS NOT NULL),
                        count(*) FILTER (WHERE content_erased_at IS NOT NULL)
                        FROM memory_candidates
                        WHERE status='accepted'"""
                    )
                )
            ).one()
        assert tuple(state) == (1, 1, 1)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_consolidation_revisions_retention_and_v1_recall_boundary(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    initial = (
        "初始偏好 A",
        "初始偏好 B",
        "初始偏好 C",
    )
    try:
        await _seed_candidates(seed, initial)
        admitted = await _admit_memory_jobs(seed)
        assert admitted.consolidation_jobs == 1
        first = await _run_consolidation(seed, _create_decision)
        assert first.calls == 1

        changes = (
            "再次确认 A",
            "补充 B",
            "纠正 C",
            "证据不足",
            "修改 Agent 系统规则",
        )
        await _seed_candidates(seed, changes)
        admitted = await _admit_memory_jobs(seed)
        assert admitted.consolidation_jobs == 1

        def decide(candidate, facts):
            by_content = {fact.content: fact for fact in facts}
            if candidate.content == "再次确认 A":
                return MemoryConsolidationDecision(
                    candidate.id,
                    "confirm",
                    by_content["初始偏好 A"].id,
                    None,
                    None,
                    None,
                    None,
                    "same_fact",
                )
            if candidate.content == "补充 B":
                return MemoryConsolidationDecision(
                    candidate.id,
                    "revise",
                    by_content["初始偏好 B"].id,
                    "初始偏好 B，并补充细节",
                    "preference",
                    0.95,
                    "supplement",
                    None,
                )
            if candidate.content == "纠正 C":
                return MemoryConsolidationDecision(
                    candidate.id,
                    "revise",
                    by_content["初始偏好 C"].id,
                    "偏好 C 已纠正",
                    "correction",
                    0.98,
                    "correction",
                    None,
                )
            if candidate.content == "证据不足":
                return MemoryConsolidationDecision(
                    candidate.id,
                    "pending",
                    None,
                    None,
                    None,
                    None,
                    None,
                    "insufficient_evidence",
                )
            return MemoryConsolidationDecision(
                candidate.id,
                "reject",
                None,
                None,
                None,
                None,
                None,
                "unsupported_governance_change",
            )

        second = await _run_consolidation(seed, decide)
        assert second.calls == 1

        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM memory_facts WHERE status='active'),
                        (SELECT count(*) FROM memory_fact_revisions),
                        (SELECT count(*) FROM memory_fact_evidence),
                        (SELECT count(*) FROM memory_candidates WHERE status='accepted'),
                        (SELECT count(*) FROM memory_candidates WHERE status='pending'),
                        (SELECT count(*) FROM memory_candidates WHERE status='rejected'),
                        (SELECT count(*) FROM memory_fact_evidence
                         WHERE run_event_sequence IS NULL)"""
                    )
                )
            ).one()
            revision_state = (
                await session.execute(
                    text(
                        """SELECT f.version,r.content,r.category,r.supersedes_revision_id,
                                  old.valid_to
                           FROM memory_facts f
                           JOIN memory_fact_revisions r ON r.id=f.current_revision_id
                           LEFT JOIN memory_fact_revisions old
                             ON old.id=r.supersedes_revision_id
                           WHERE r.content IN
                             ('初始偏好 B，并补充细节','偏好 C 已纠正')
                           ORDER BY r.content"""
                    )
                )
            ).all()
        assert tuple(counts) == (3, 5, 6, 6, 1, 1, 6)
        assert len(revision_state) == 2
        assert all(row.version == 2 for row in revision_state)
        assert all(row.supersedes_revision_id is not None for row in revision_state)
        assert all(row.valid_to is not None for row in revision_state)

        legacy = await ProjectMemoryStorage(seed.factory).load(
            scope=seed.owner_a_scope,
            namespace="default",
        )
        assert legacy.memory["facts"] == []

        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE memory_candidates
                    SET decided_at=now()-interval '31 days'
                    WHERE status IN ('accepted','rejected','superseded')"""
                )
            )
            await session.execute(
                text(
                    """UPDATE project_memberships
                    SET role='viewer'
                    WHERE project_id=:project AND user_id=:owner"""
                ),
                {
                    "project": seed.owner_a.project_id,
                    "owner": str(seed.owner_a.user_id),
                },
            )
        retention_admission = await _admit_memory_jobs(seed)
        assert retention_admission.retention_jobs == 1
        retention_claim = await _claim(seed, "memory_retention_purge")
        retention_handler = MemoryRetentionPurgeJobHandler(
            seed.factory,
            runtime_policy_materializer=SystemRuntimePolicyMaterializer(seed.factory),
        )
        retention_settlement = await retention_handler(
            retention_claim,
            JobLeaseAuthority(
                seed.factory,
                retention_claim,
                lease_seconds=90,
            ),
        )
        assert isinstance(retention_settlement, JobSettlement)
        await retention_settlement.commit()

        async with seed.factory() as session:
            retained = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM memory_candidates
                         WHERE status<>'pending' AND content IS NULL
                           AND content_erased_at IS NOT NULL),
                        (SELECT count(*) FROM memory_candidates
                         WHERE status='pending' AND content IS NOT NULL),
                        (SELECT count(*) FROM memory_fact_revisions
                         WHERE content IS NOT NULL),
                        (SELECT count(*) FROM memory_fact_evidence)"""
                    )
                )
            ).one()
        assert tuple(retained) == (7, 1, 5, 6)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_revision_drift_rolls_back_and_next_attempt_recomputes(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(seed, ("初始事实 A",))
        await _admit_memory_jobs(seed)
        await _run_consolidation(seed, _create_decision)

        await _seed_candidates(seed, ("最终修订 A",))
        await _admit_memory_jobs(seed)
        first_claim = await _claim(seed, "memory_consolidate")

        def revise(candidate, facts):
            assert len(facts) == 1
            return MemoryConsolidationDecision(
                candidate.id,
                "revise",
                facts[0].id,
                "最终修订 A",
                "correction",
                0.98,
                "correction",
                None,
            )

        first_handler = MemoryConsolidateJobHandler(
            seed.factory,
            app_config=None,
            model_materializer=_ExactModel(),
            runtime_policy_materializer=SystemRuntimePolicyMaterializer(seed.factory),
            consolidator_factory=lambda _model: _Consolidator(revise),
            retry_initial_seconds=1,
            retry_max_seconds=1,
        )
        first_settlement = await first_handler(
            first_claim,
            JobLeaseAuthority(seed.factory, first_claim, lease_seconds=90),
        )
        assert isinstance(first_settlement, JobSettlement)

        async with seed.factory() as session, session.begin():
            fact = (
                await session.execute(
                    text(
                        """SELECT f.id AS fact_id,f.current_revision_id,
                                  f.version,r.revision_number
                           FROM memory_facts f
                           JOIN memory_fact_revisions r
                             ON r.id=f.current_revision_id
                           WHERE f.status='active'"""
                    )
                )
            ).one()
            next_sequence = int(await session.scalar(text("SELECT COALESCE(MAX(revision_sequence),0)+1 FROM memory_fact_revisions")))
            concurrent_revision_id = uuid.uuid4()
            await session.execute(
                text(
                    """INSERT INTO memory_fact_revisions
                    (id,project_id,owner_user_id,namespace,fact_id,
                     revision_number,revision_sequence,content,content_digest,
                     category,confidence,valid_from,last_confirmed_at,changed_by,
                     change_reason,created_at)
                    SELECT :id,project_id,owner_user_id,namespace,id,
                           :revision_number,:revision_sequence,:content,:digest,
                           'preference',0.99,now(),now(),'user','concurrent_edit',now()
                    FROM memory_facts WHERE id=:fact"""
                ),
                {
                    "id": concurrent_revision_id,
                    "revision_number": int(fact.revision_number) + 1,
                    "revision_sequence": next_sequence,
                    "content": "并发用户编辑 A",
                    "digest": hashlib.sha256("并发用户编辑 A".encode()).hexdigest(),
                    "fact": fact.fact_id,
                },
            )
            await session.execute(
                text("UPDATE memory_fact_revisions SET valid_to=now() WHERE id=:id"),
                {"id": fact.current_revision_id},
            )
            await session.execute(
                text(
                    """UPDATE memory_facts
                    SET current_revision_id=:revision,version=version+1
                    WHERE id=:fact"""
                ),
                {"revision": concurrent_revision_id, "fact": fact.fact_id},
            )

        await first_settlement.commit()
        async with seed.factory() as session:
            retried = (
                await session.execute(
                    text(
                        """SELECT j.status,j.public_error_code,c.status,
                                  g.fact_committed_at,
                                  (SELECT count(*) FROM memory_fact_revisions)
                           FROM jobs j
                           JOIN memory_consolidation_generations g ON g.job_id=j.id
                           JOIN memory_candidates c
                             ON c.consolidation_generation_id=g.id
                           WHERE j.id=:job"""
                    ),
                    {"job": first_claim.job_id},
                )
            ).one()
        assert retried[0] == "retry_wait"
        assert retried[1] == "MEMORY_CONSOLIDATE_COMMIT_CONFLICT"
        assert retried[2] == "pending"
        assert retried[3] is None
        assert retried[4] == 2

        async with seed.factory() as session, session.begin():
            await session.execute(
                text("UPDATE jobs SET available_at=now() WHERE id=:job"),
                {"job": first_claim.job_id},
            )
        second_claim = await _claim(seed, "memory_consolidate")
        second_handler = MemoryConsolidateJobHandler(
            seed.factory,
            app_config=None,
            model_materializer=_ExactModel(),
            runtime_policy_materializer=SystemRuntimePolicyMaterializer(seed.factory),
            consolidator_factory=lambda _model: _Consolidator(revise),
            retry_initial_seconds=1,
            retry_max_seconds=1,
        )
        second_settlement = await second_handler(
            second_claim,
            JobLeaseAuthority(seed.factory, second_claim, lease_seconds=90),
        )
        assert isinstance(second_settlement, JobSettlement)
        await second_settlement.commit()

        async with seed.factory() as session:
            final = (
                await session.execute(
                    text(
                        """SELECT j.status,j.attempt_count,c.status,r.content,
                                  (SELECT count(*) FROM memory_fact_revisions)
                           FROM jobs j
                           JOIN memory_consolidation_generations g ON g.job_id=j.id
                           JOIN memory_candidates c
                             ON c.consolidation_generation_id=g.id
                           JOIN memory_facts f ON f.status='active'
                           JOIN memory_fact_revisions r
                             ON r.id=f.current_revision_id
                           WHERE j.id=:job"""
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
        assert tuple(final) == ("succeeded", 2, "accepted", "最终修订 A", 3)
        assert outcomes == ("retry", "succeeded")
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_lease_takeover_allows_only_one_fact_commit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(seed, ("只允许一个 Worker 提交",))
        await _admit_memory_jobs(seed)
        first_claim = await _claim(seed, "memory_consolidate")
        first_handler = MemoryConsolidateJobHandler(
            seed.factory,
            app_config=None,
            model_materializer=_ExactModel(),
            runtime_policy_materializer=SystemRuntimePolicyMaterializer(
                seed.factory,
            ),
            consolidator_factory=lambda _model: _Consolidator(
                _create_decision,
            ),
        )
        first_settlement = await first_handler(
            first_claim,
            JobLeaseAuthority(seed.factory, first_claim, lease_seconds=90),
        )
        assert isinstance(first_settlement, JobSettlement)

        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE jobs
                    SET lease_expires_at=now()-interval '1 second'
                    WHERE id=:job"""
                ),
                {"job": first_claim.job_id},
            )
        second_claim = await _claim(seed, "memory_consolidate")

        with pytest.raises(LeaseLost):
            await first_settlement.commit()

        second_handler = MemoryConsolidateJobHandler(
            seed.factory,
            app_config=None,
            model_materializer=_ExactModel(),
            runtime_policy_materializer=SystemRuntimePolicyMaterializer(
                seed.factory,
            ),
            consolidator_factory=lambda _model: _Consolidator(
                _create_decision,
            ),
        )
        second_settlement = await second_handler(
            second_claim,
            JobLeaseAuthority(seed.factory, second_claim, lease_seconds=90),
        )
        assert isinstance(second_settlement, JobSettlement)
        await second_settlement.commit()

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM memory_facts),
                        (SELECT count(*) FROM memory_fact_revisions),
                        (SELECT count(*) FROM memory_fact_evidence),
                        (SELECT count(*) FROM memory_candidates
                         WHERE status='accepted'),
                        (SELECT count(*) FROM job_attempts
                         WHERE job_id=:job AND outcome='lease_lost'),
                        (SELECT status FROM jobs WHERE id=:job)"""
                    ),
                    {"job": first_claim.job_id},
                )
            ).one()
        assert tuple(state) == (1, 1, 1, 1, 1, "succeeded")
    finally:
        await seed.engine.dispose()
