from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from support.memory_v2_seed import admit_memory_extraction_job
from support.private_thread_seed import seed_private_thread_database

from app.personalization.repository import AccountPersonalizationConflict
from app.personalization.service import AccountPersonalizationService
from app.worker.memory_extract import MemoryExtractJobHandler
from app.worker.service import JobLeaseAuthority, JobSettlement
from deerflow.agents.memory.extractor import (
    ExtractedMemoryCandidate,
    MemoryExtractionResult,
)
from deerflow.persistence.private_work.memory_v2_management import (
    MemoryV2ManagementRepository,
)


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


class _Extractor:
    async def extract(self, _sources) -> MemoryExtractionResult:
        return MemoryExtractionResult(
            candidates=(
                ExtractedMemoryCandidate(
                    source_ordinal=0,
                    candidate_type="preference",
                    content="用户偏好中文且简洁的回答。",
                    confidence=0.95,
                    retention_class="durable",
                    sensitivity="normal",
                ),
            )
        )


async def _seed_memory_v1_and_v2(seed):
    admitted, claim = await admit_memory_extraction_job(
        seed,
        messages=[
            {
                "role": "user",
                "id": f"memory-pr13-{uuid.uuid4()}",
                "content": "请记住我偏好中文且简洁的回答。",
            }
        ],
        mode="consolidate",
        model_purpose="memory",
        make_policy_current=True,
    )
    settlement = await MemoryExtractJobHandler(
        seed.factory,
        app_config=None,
        model_materializer=_ExtractionModel(),
        runtime_policy_materializer=_ExtractionPolicy(),
        extractor_factory=lambda _model: _Extractor(),
    )(
        claim,
        JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
    )
    assert isinstance(settlement, JobSettlement)
    await settlement.commit()

    owner_a_memory_id = uuid.uuid4()
    owner_b_memory_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        candidate = (
            await session.execute(
                text(
                    """SELECT id,updated_at
                    FROM memory_candidates
                    WHERE project_id=:project AND owner_user_id=:owner
                    ORDER BY created_at,id LIMIT 1"""
                ),
                {
                    "project": seed.owner_a.project_id,
                    "owner": str(seed.owner_a.user_id),
                },
            )
        ).one()
        await MemoryV2ManagementRepository(session).accept_candidate(
            seed.owner_a_scope,
            namespace="default",
            candidate_id=candidate.id,
            expected_updated_at=candidate.updated_at,
            now=await session.scalar(text("SELECT now()")),
        )
        await session.execute(
            text(
                """INSERT INTO user_project_memories
                (id,project_id,owner_user_id,namespace,context_summary,version)
                VALUES
                (:memory_a,:project,:owner_a,'default','{}'::jsonb,1),
                (:memory_b,:project,:owner_b,'default','{}'::jsonb,1)"""
            ),
            {
                "memory_a": owner_a_memory_id,
                "memory_b": owner_b_memory_id,
                "project": seed.owner_a.project_id,
                "owner_a": str(seed.owner_a.user_id),
                "owner_b": str(seed.owner_b.user_id),
            },
        )
        await session.execute(
            text(
                """INSERT INTO user_project_memory_facts
                (id,project_id,owner_user_id,memory_id,content,category,confidence)
                VALUES
                (:id,:project,:owner,:memory,'旧版记忆','preference',0.9)"""
            ),
            {
                "id": uuid.uuid4(),
                "project": seed.owner_a.project_id,
                "owner": str(seed.owner_a.user_id),
                "memory": owner_a_memory_id,
            },
        )
        rendered = "<memory>用户偏好中文且简洁的回答。</memory>"
        await session.execute(
            text(
                """INSERT INTO run_memory_context_snapshots
                (id,project_id,owner_user_id,namespace,thread_id,run_id,
                 pipeline_mode,fact_revision_ceiling,summary_id,summary_revision,
                 selection_version,renderer_version,prompt_version,policy_revision,
                 token_budget,rendered_content,rendered_content_digest)
                VALUES
                (:id,:project,:owner,'default',:thread,:run,'v2',1,NULL,NULL,
                 'memory-selection-v1','memory-render-v1','memory-context-v1',1,
                 1024,:rendered,:digest)"""
            ),
            {
                "id": uuid.uuid4(),
                "project": admitted.run.project_id,
                "owner": admitted.run.owner_user_id,
                "thread": admitted.run.thread_id,
                "run": admitted.run.run_id,
                "rendered": rendered,
                "digest": hashlib.sha256(rendered.encode()).hexdigest(),
            },
        )
    return admitted


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_account_memory_preference_cas_and_reset_preserve_conversations(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        service = AccountPersonalizationService(seed.factory)
        owner_a_default = await service.get(seed.owner_a.user_id)
        owner_b_default = await service.get(seed.owner_b.user_id)
        assert (owner_a_default.memory_enabled, owner_a_default.version) == (
            True,
            1,
        )
        assert (owner_b_default.memory_enabled, owner_b_default.version) == (
            True,
            1,
        )

        await _seed_memory_v1_and_v2(seed)
        async with seed.factory() as session:
            conversations_before = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM threads_meta
                         WHERE owner_user_id=:owner),
                        (SELECT count(*) FROM runs
                         WHERE owner_user_id=:owner)"""
                    ),
                    {"owner": str(seed.owner_a.user_id)},
                )
            ).one()

        disabled = await service.update_memory(
            seed.owner_a.user_id,
            memory_enabled=False,
            expected_version=1,
        )
        assert (disabled.memory_enabled, disabled.version) == (False, 2)
        with pytest.raises(AccountPersonalizationConflict):
            await service.update_memory(
                seed.owner_a.user_id,
                memory_enabled=True,
                expected_version=1,
            )
        assert (await service.get(seed.owner_b.user_id)).version == 1

        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE project_memberships
                    SET status='left',ended_at=now(),retention_until=now()+interval '7 days',
                        ended_by_user_id=:owner,end_reason='left',version=version+1
                    WHERE project_id=:project AND user_id=:owner"""
                ),
                {
                    "project": seed.owner_a.project_id,
                    "owner": str(seed.owner_a.user_id),
                },
            )

        reset = await service.reset_memory(
            seed.owner_a.user_id,
            expected_version=2,
        )
        assert reset.version == 3
        assert reset.scopes_reset == 1
        assert reset.v1_memories == 1
        assert reset.source_batches == 1
        assert reset.candidates == 1
        assert reset.facts == 1
        assert reset.snapshots == 1

        preference_after = await service.get(seed.owner_a.user_id)
        assert (preference_after.memory_enabled, preference_after.version) == (
            False,
            3,
        )
        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM user_project_memories
                         WHERE owner_user_id=:owner_a),
                        (SELECT count(*) FROM memory_source_batches
                         WHERE owner_user_id=:owner_a),
                        (SELECT count(*) FROM memory_candidates
                         WHERE owner_user_id=:owner_a),
                        (SELECT count(*) FROM memory_facts
                         WHERE owner_user_id=:owner_a),
                        (SELECT count(*) FROM run_memory_context_snapshots
                         WHERE owner_user_id=:owner_a),
                        (SELECT count(*) FROM memory_suppressions
                         WHERE owner_user_id=:owner_a
                           AND reason='account_memory_reset'),
                        (SELECT count(*) FROM user_project_memories
                         WHERE owner_user_id=:owner_b),
                        (SELECT count(*) FROM threads_meta
                         WHERE owner_user_id=:owner_a),
                        (SELECT count(*) FROM runs
                         WHERE owner_user_id=:owner_a)"""
                    ),
                    {
                        "owner_a": str(seed.owner_a.user_id),
                        "owner_b": str(seed.owner_b.user_id),
                    },
                )
            ).one()
        assert tuple(state[:5]) == (0, 0, 0, 0, 0)
        assert state[5] == 1
        assert state[6] == 1
        assert tuple(state[7:]) == tuple(conversations_before)
    finally:
        await seed.engine.dispose()
