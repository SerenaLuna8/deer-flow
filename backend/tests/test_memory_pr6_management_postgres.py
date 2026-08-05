from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from support.memory_v2_seed import admit_memory_extraction_job
from support.private_thread_seed import seed_private_thread_database

from app.worker.memory_extract import MemoryExtractJobHandler
from app.worker.service import JobLeaseAuthority, JobSettlement
from deerflow.agents.memory.extractor import (
    ExtractedMemoryCandidate,
    MemoryExtractionResult,
)
from deerflow.persistence.private_work.memory_v2_management import (
    MemoryV2ManagementConflict,
    MemoryV2ManagementNotFound,
    MemoryV2ManagementRepository,
)
from deerflow.persistence.private_work.memory_v2_repository import MemoryV2Repository


class _PolicyMaterializer:
    async def materialize_run_snapshot(self, **_kwargs):
        return SimpleNamespace(
            memory=SimpleNamespace(
                enabled=True,
                pipeline_mode="shadow",
                model_name=None,
            )
        )


class _ModelMaterializer:
    async def materialize_snapshot(self, **_kwargs):
        return SimpleNamespace(name="memory-pr6-test")


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


async def _seed_candidates(seed, *contents: str) -> None:
    _admitted, claim = await admit_memory_extraction_job(
        seed,
        messages=[
            {
                "role": "user",
                "id": f"source-{ordinal}-{uuid.uuid4()}",
                "content": content,
            }
            for ordinal, content in enumerate(contents)
        ],
    )
    handler = MemoryExtractJobHandler(
        seed.factory,
        app_config=None,
        model_materializer=_ModelMaterializer(),
        runtime_policy_materializer=_PolicyMaterializer(),
        extractor_factory=lambda _model: _Extractor(tuple(contents)),
    )
    settlement = await handler(
        claim,
        JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
    )
    assert isinstance(settlement, JobSettlement)
    await settlement.commit()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_v2_management_enforces_candidate_cas_fact_lifecycle_and_owner_scope(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(
            seed,
            "用户喜欢简洁回答。",
            "用户喜欢先看结论。",
        )
        accepted_at = datetime.now(UTC)

        async with seed.factory() as session, session.begin():
            repository = MemoryV2ManagementRepository(session)
            candidates = await repository.list_candidates(
                seed.owner_a_scope,
                namespace="default",
                statuses=("pending",),
                limit=100,
                offset=0,
            )
            assert {candidate.content for candidate in candidates} == {
                "用户喜欢简洁回答。",
                "用户喜欢先看结论。",
            }
            by_content = {candidate.content: candidate for candidate in candidates}
            accepted_candidate = by_content["用户喜欢简洁回答。"]
            rejected_candidate = by_content["用户喜欢先看结论。"]

            assert (
                await repository.list_candidates(
                    seed.owner_b_scope,
                    namespace="default",
                    statuses=("pending",),
                    limit=100,
                    offset=0,
                )
                == ()
            )
            with pytest.raises(MemoryV2ManagementNotFound):
                await repository.accept_candidate(
                    seed.owner_b_scope,
                    namespace="default",
                    candidate_id=accepted_candidate.id,
                    expected_updated_at=accepted_candidate.updated_at,
                    now=accepted_at,
                )

            with pytest.raises(MemoryV2ManagementConflict):
                await repository.accept_candidate(
                    seed.owner_a_scope,
                    namespace="default",
                    candidate_id=accepted_candidate.id,
                    expected_updated_at=accepted_candidate.updated_at - timedelta(microseconds=1),
                    now=accepted_at,
                )
            fact = await repository.accept_candidate(
                seed.owner_a_scope,
                namespace="default",
                candidate_id=accepted_candidate.id,
                expected_updated_at=accepted_candidate.updated_at,
                now=accepted_at,
            )
            assert (fact.status, fact.version, fact.current_revision.content) == (
                "active",
                1,
                "用户喜欢简洁回答。",
            )

            with pytest.raises(MemoryV2ManagementConflict):
                await repository.reject_candidate(
                    seed.owner_a_scope,
                    namespace="default",
                    candidate_id=rejected_candidate.id,
                    expected_updated_at=rejected_candidate.updated_at - timedelta(microseconds=1),
                    now=accepted_at + timedelta(seconds=1),
                )
            rejected = await repository.reject_candidate(
                seed.owner_a_scope,
                namespace="default",
                candidate_id=rejected_candidate.id,
                expected_updated_at=rejected_candidate.updated_at,
                now=accepted_at + timedelta(seconds=1),
            )
            assert (rejected.status, rejected.decision_reason) == (
                "rejected",
                "user_rejected",
            )

            with pytest.raises(MemoryV2ManagementNotFound):
                await repository.get_fact_detail(
                    seed.owner_b_scope,
                    namespace="default",
                    fact_id=fact.id,
                )
            with pytest.raises(MemoryV2ManagementConflict):
                await repository.revise_fact(
                    seed.owner_a_scope,
                    namespace="default",
                    fact_id=fact.id,
                    expected_version=2,
                    content="用户喜欢一句话结论。",
                    category="preference",
                    confidence=0.9,
                    reason="user_edit",
                    now=accepted_at + timedelta(seconds=2),
                )

            revised = await repository.revise_fact(
                seed.owner_a_scope,
                namespace="default",
                fact_id=fact.id,
                expected_version=1,
                content="用户喜欢一句话结论。",
                category="preference",
                confidence=0.9,
                reason="user_edit",
                now=accepted_at + timedelta(seconds=2),
            )
            assert (revised.version, revised.current_revision.revision_number) == (2, 2)
            assert revised.current_revision.content == "用户喜欢一句话结论。"

            disabled = await repository.set_fact_enabled(
                seed.owner_a_scope,
                namespace="default",
                fact_id=fact.id,
                expected_version=2,
                enabled=False,
                now=accepted_at + timedelta(seconds=3),
            )
            assert (disabled.status, disabled.version) == ("disabled", 3)
            with pytest.raises(MemoryV2ManagementConflict):
                await repository.set_fact_enabled(
                    seed.owner_a_scope,
                    namespace="default",
                    fact_id=fact.id,
                    expected_version=2,
                    enabled=True,
                    now=accepted_at + timedelta(seconds=4),
                )
            restored = await repository.set_fact_enabled(
                seed.owner_a_scope,
                namespace="default",
                fact_id=fact.id,
                expected_version=3,
                enabled=True,
                now=accepted_at + timedelta(seconds=4),
            )
            assert (restored.status, restored.version) == ("active", 4)

            detail = await repository.get_fact_detail(
                seed.owner_a_scope,
                namespace="default",
                fact_id=fact.id,
            )
            assert [revision.revision_number for revision in detail.revisions] == [2, 1]
            assert len(detail.evidence) == 1
            assert (
                await repository.list_facts(
                    seed.owner_b_scope,
                    namespace="default",
                    statuses=("active", "disabled"),
                    limit=100,
                    offset=0,
                )
                == ()
            )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_v2_fact_search_filters_current_revision_before_pagination(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(
            seed,
            "Plan ALPHA work first.",
            "Plain context note.",
            "Category-only match.",
        )
        now = datetime.now(UTC)

        async with seed.factory() as session, session.begin():
            repository = MemoryV2ManagementRepository(session)
            candidates = await repository.list_candidates(
                seed.owner_a_scope,
                namespace="default",
                statuses=("pending",),
                limit=100,
                offset=0,
            )
            by_content = {candidate.content: candidate for candidate in candidates}
            facts = {}
            for ordinal, content in enumerate(
                (
                    "Plan ALPHA work first.",
                    "Plain context note.",
                    "Category-only match.",
                )
            ):
                candidate = by_content[content]
                facts[content] = await repository.accept_candidate(
                    seed.owner_a_scope,
                    namespace="default",
                    candidate_id=candidate.id,
                    expected_updated_at=candidate.updated_at,
                    now=now + timedelta(seconds=ordinal),
                )

            context_fact = facts["Plain context note."]
            await repository.revise_fact(
                seed.owner_a_scope,
                namespace="default",
                fact_id=context_fact.id,
                expected_version=1,
                content=None,
                category="context",
                confidence=None,
                reason="test_category",
                now=now + timedelta(seconds=4),
            )
            category_match = facts["Category-only match."]
            await repository.revise_fact(
                seed.owner_a_scope,
                namespace="default",
                fact_id=category_match.id,
                expected_version=1,
                content=None,
                category="AlphaCategory",
                confidence=None,
                reason="test_category",
                now=now + timedelta(seconds=5),
            )

            first_page = await repository.list_facts(
                seed.owner_a_scope,
                namespace="default",
                statuses=("active",),
                limit=1,
                offset=0,
                query="  alpha  ",
            )
            second_page = await repository.list_facts(
                seed.owner_a_scope,
                namespace="default",
                statuses=("active",),
                limit=1,
                offset=1,
                query="alpha",
            )
            exhausted = await repository.list_facts(
                seed.owner_a_scope,
                namespace="default",
                statuses=("active",),
                limit=1,
                offset=2,
                query="alpha",
            )
            context_only = await repository.list_facts(
                seed.owner_a_scope,
                namespace="default",
                statuses=("active",),
                limit=100,
                offset=0,
                category="  context  ",
            )

            assert {item.current_revision.content for item in (*first_page, *second_page)} == {
                "Plan ALPHA work first.",
                "Category-only match.",
            }
            assert exhausted == ()
            assert [item.id for item in context_only] == [context_fact.id]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_v2_hard_forget_erases_lineage_and_blocks_source_replay(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(seed, "用户不希望保留这条偏好。")
        forgotten_at = datetime.now(UTC)
        lineage_hmac = hashlib.sha256(b"memory-pr6-forgotten-lineage").hexdigest()

        async with seed.factory() as session, session.begin():
            repository = MemoryV2ManagementRepository(session)
            (candidate,) = await repository.list_candidates(
                seed.owner_a_scope,
                namespace="default",
                statuses=("pending",),
                limit=100,
                offset=0,
            )
            fact = await repository.accept_candidate(
                seed.owner_a_scope,
                namespace="default",
                candidate_id=candidate.id,
                expected_updated_at=candidate.updated_at,
                now=forgotten_at,
            )
            revised = await repository.revise_fact(
                seed.owner_a_scope,
                namespace="default",
                fact_id=fact.id,
                expected_version=1,
                content="这条偏好必须被彻底删除。",
                category="preference",
                confidence=1.0,
                reason="user_edit",
                now=forgotten_at + timedelta(seconds=1),
            )

            source = (
                await session.execute(
                    text(
                        """SELECT i.content_hmac,b.source_hmac_key_version
                           FROM memory_source_items i
                           JOIN memory_source_batches b ON b.id=i.source_batch_id
                           WHERE i.project_id=:project AND i.owner_user_id=:owner"""
                    ),
                    {
                        "project": seed.owner_a.project_id,
                        "owner": str(seed.owner_a.user_id),
                    },
                )
            ).one()

            with pytest.raises(MemoryV2ManagementNotFound):
                await repository.hard_forget_fact(
                    seed.owner_b_scope,
                    namespace="default",
                    fact_id=fact.id,
                    expected_version=revised.version,
                    lineage_identity_hmac=lineage_hmac,
                    lineage_hmac_key_version="memory-lineage-test-v1",
                    now=forgotten_at + timedelta(seconds=2),
                )
            with pytest.raises(MemoryV2ManagementConflict):
                await repository.hard_forget_fact(
                    seed.owner_a_scope,
                    namespace="default",
                    fact_id=fact.id,
                    expected_version=1,
                    lineage_identity_hmac=lineage_hmac,
                    lineage_hmac_key_version="memory-lineage-test-v1",
                    now=forgotten_at + timedelta(seconds=2),
                )

            result = await repository.hard_forget_fact(
                seed.owner_a_scope,
                namespace="default",
                fact_id=fact.id,
                expected_version=revised.version,
                lineage_identity_hmac=lineage_hmac,
                lineage_hmac_key_version="memory-lineage-test-v1",
                now=forgotten_at + timedelta(seconds=2),
            )
            assert (result.status, result.version) == ("deleted", revised.version + 1)
            assert result.erased_candidates == 1
            assert result.erased_revisions == 2
            assert result.erased_evidence == 1
            assert result.erased_source_items == 1

            with pytest.raises(MemoryV2ManagementNotFound):
                await repository.get_fact_detail(
                    seed.owner_a_scope,
                    namespace="default",
                    fact_id=fact.id,
                )
            with pytest.raises(MemoryV2ManagementNotFound):
                await repository.set_fact_enabled(
                    seed.owner_a_scope,
                    namespace="default",
                    fact_id=fact.id,
                    expected_version=result.version,
                    enabled=True,
                    now=forgotten_at + timedelta(seconds=3),
                )

            fact_state = (
                await session.execute(
                    text("SELECT status,version,deleted_at IS NOT NULL FROM memory_facts WHERE id=:id"),
                    {"id": fact.id},
                )
            ).one()
            assert tuple(fact_state) == ("deleted", result.version, True)
            revision_state = (
                await session.execute(
                    text(
                        """SELECT count(*),bool_and(content IS NULL),
                                  bool_and(content_erased_at IS NOT NULL),
                                  bool_and(source_candidate_id IS NULL)
                           FROM memory_fact_revisions WHERE fact_id=:id"""
                    ),
                    {"id": fact.id},
                )
            ).one()
            assert tuple(revision_state) == (2, True, True, True)
            evidence_state = (
                await session.execute(
                    text(
                        """SELECT source_candidate_id,source_item_id,thread_id,run_id,
                                  run_event_sequence,evidence_excerpt,source_erased_at IS NOT NULL
                           FROM memory_fact_evidence WHERE fact_id=:id"""
                    ),
                    {"id": fact.id},
                )
            ).one()
            assert tuple(evidence_state) == (None, None, None, None, None, None, True)
            candidate_state = (
                await session.execute(
                    text(
                        """SELECT status,content,content_erased_at IS NOT NULL,
                                  consolidation_generation_id
                           FROM memory_candidates WHERE id=:id"""
                    ),
                    {"id": candidate.id},
                )
            ).one()
            assert tuple(candidate_state) == ("superseded", None, True, None)
            source_state = (
                await session.execute(
                    text(
                        """SELECT i.content,i.source_erased_at IS NOT NULL,
                                  b.suppressed_at IS NOT NULL,b.suppression_reason
                           FROM memory_source_items i
                           JOIN memory_source_batches b ON b.id=i.source_batch_id
                           WHERE i.project_id=:project AND i.owner_user_id=:owner"""
                    ),
                    {
                        "project": seed.owner_a.project_id,
                        "owner": str(seed.owner_a.user_id),
                    },
                )
            ).one()
            assert tuple(source_state) == (None, True, True, "hard_forget")
            suppressions = (
                await session.execute(
                    text(
                        """SELECT suppression_kind,identity_hmac,hmac_key_version
                           FROM memory_suppressions
                           WHERE project_id=:project AND owner_user_id=:owner
                           ORDER BY suppression_kind"""
                    ),
                    {
                        "project": seed.owner_a.project_id,
                        "owner": str(seed.owner_a.user_id),
                    },
                )
            ).all()
            assert suppressions == [
                ("fact_lineage", lineage_hmac, "memory-lineage-test-v1"),
                ("source", source.content_hmac, source.source_hmac_key_version),
            ]
            assert await MemoryV2Repository(session).source_suppressed(
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                namespace="default",
                hmac_key_version=source.source_hmac_key_version,
                identity_hmacs=(source.content_hmac,),
            )
            assert not await MemoryV2Repository(session).source_suppressed(
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_b.user_id),
                namespace="default",
                hmac_key_version=source.source_hmac_key_version,
                identity_hmacs=(source.content_hmac,),
            )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_v2_owner_purge_removes_only_the_requested_private_scope(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _seed_candidates(seed, "仅属于 owner A 的候选。")
        owner_b_suppression_id = uuid.uuid4()
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """INSERT INTO memory_suppressions
                       (id,project_id,owner_user_id,namespace,suppression_kind,
                        identity_hmac,hmac_key_version,reason)
                       VALUES (:id,:project,:owner,'default','source',:identity,
                               'memory-test-v1','owner_b_control')"""
                ),
                {
                    "id": owner_b_suppression_id,
                    "project": seed.owner_b.project_id,
                    "owner": str(seed.owner_b.user_id),
                    "identity": hashlib.sha256(b"owner-b-source").hexdigest(),
                },
            )
            await MemoryV2ManagementRepository(session).purge_scope(
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                now=datetime.now(UTC),
            )

        async with seed.factory() as session:
            owner_a_counts = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT count(*) FROM memory_source_batches WHERE owner_user_id=:owner),
                           (SELECT count(*) FROM memory_source_items WHERE owner_user_id=:owner),
                           (SELECT count(*) FROM memory_extraction_generations WHERE owner_user_id=:owner),
                           (SELECT count(*) FROM memory_candidates WHERE owner_user_id=:owner),
                           (SELECT count(*) FROM memory_facts WHERE owner_user_id=:owner),
                           (SELECT count(*) FROM memory_suppressions WHERE owner_user_id=:owner)"""
                    ),
                    {"owner": str(seed.owner_a.user_id)},
                )
            ).one()
            assert tuple(owner_a_counts) == (0, 0, 0, 0, 0, 0)
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM memory_suppressions WHERE id=:id"),
                    {"id": owner_b_suppression_id},
                )
                == 1
            )
    finally:
        await seed.engine.dispose()
