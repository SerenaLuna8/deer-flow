from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from support.private_thread_seed import PrivateThreadSeed, seed_private_thread_database

from app.private_work.context import PrivateWorkContext
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.persistence.private_work.memory_v2_management import MemoryV2ManagementRepository
from deerflow.persistence.private_work.memory_v2_recall import (
    MemoryV2RecallContract,
    MemoryV2RecallRepository,
)
from deerflow.private_scope import PrivateResourceScope


@dataclass(frozen=True, slots=True)
class _SeededFact:
    fact_id: uuid.UUID
    revision_id: uuid.UUID
    revision_sequence: int
    content: str
    content_digest: str


def _contract() -> MemoryV2RecallContract:
    return MemoryV2RecallContract(
        policy_revision=7,
        max_facts=100,
        token_budget=4096,
        guaranteed_categories=("profile", "preference"),
        guaranteed_token_budget=1024,
        use_tiktoken=False,
        pipeline_mode="v2",
        selection_version="active-facts-v1",
        renderer_version="hidden-human-v1",
        prompt_version="memory-v2-v1",
    )


def _render(facts) -> str:
    return "\n".join(f"[{fact.category}] {fact.content}" for fact in facts)


async def _create_run(
    seed: PrivateThreadSeed,
    context: PrivateWorkContext,
) -> tuple[str, str]:
    thread_id = f"memory-pr7-{uuid.uuid4().hex}"
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=context.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
    admitted = await PrivateRunAdmissionService(seed.factory).admit(
        context,
        thread_id,
        PrivateRunCreate(
            kwargs={
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "id": f"message-{uuid.uuid4()}",
                            "content": "读取记忆。",
                        }
                    ]
                },
                "command": None,
                "config": {
                    "configurable": {"thread_id": thread_id},
                    "context": {},
                },
                "stream_mode": ["values"],
                "stream_subgraphs": False,
            }
        ),
    )
    return thread_id, admitted.run.run_id


async def _insert_fact(
    seed: PrivateThreadSeed,
    *,
    scope: PrivateResourceScope,
    content: str,
    namespace: str = "default",
    category: str = "preference",
    created_at: datetime | None = None,
) -> _SeededFact:
    inserted_at = created_at or datetime.now(UTC)
    fact_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    async with seed.factory() as session, session.begin():
        revision_sequence = int(
            await session.scalar(
                text(
                    """SELECT COALESCE(MAX(revision_sequence),0)+1
                       FROM memory_fact_revisions
                       WHERE project_id=:project AND owner_user_id=:owner
                         AND namespace=:namespace"""
                ),
                {
                    "project": uuid.UUID(scope.project_id),
                    "owner": scope.owner_user_id,
                    "namespace": namespace,
                },
            )
        )
        await session.execute(
            text(
                """INSERT INTO memory_facts
                   (id,project_id,owner_user_id,namespace,fact_kind,status,
                    current_revision_id,version,created_at,updated_at)
                   VALUES (:id,:project,:owner,:namespace,:kind,'active',
                           :revision,1,:created_at,:created_at)"""
            ),
            {
                "id": fact_id,
                "project": uuid.UUID(scope.project_id),
                "owner": scope.owner_user_id,
                "namespace": namespace,
                "kind": category,
                "revision": revision_id,
                "created_at": inserted_at,
            },
        )
        await session.execute(
            text(
                """INSERT INTO memory_fact_revisions
                   (id,project_id,owner_user_id,namespace,fact_id,revision_number,
                    revision_sequence,content,content_digest,category,confidence,
                    valid_from,last_confirmed_at,changed_by,change_reason,created_at)
                   VALUES (:id,:project,:owner,:namespace,:fact,1,:sequence,
                           :content,:digest,:category,0.95,:created_at,:created_at,
                           'user','test_seed',:created_at)"""
            ),
            {
                "id": revision_id,
                "project": uuid.UUID(scope.project_id),
                "owner": scope.owner_user_id,
                "namespace": namespace,
                "fact": fact_id,
                "sequence": revision_sequence,
                "content": content,
                "digest": content_digest,
                "category": category,
                "created_at": inserted_at,
            },
        )
    return _SeededFact(
        fact_id=fact_id,
        revision_id=revision_id,
        revision_sequence=revision_sequence,
        content=content,
        content_digest=content_digest,
    )


async def _load(
    seed: PrivateThreadSeed,
    *,
    scope: PrivateResourceScope,
    thread_id: str,
    run_id: str,
    namespace: str = "default",
):
    async with seed.factory() as session, session.begin():
        return await MemoryV2RecallRepository(session).load_or_create(
            scope=scope,
            namespace=namespace,
            thread_id=thread_id,
            run_id=run_id,
            contract=_contract(),
            renderer=_render,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_v2_recall_creates_once_reuses_and_never_creates_v1_memory(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        fact = await _insert_fact(
            seed,
            scope=seed.owner_a_scope,
            content="用户喜欢先看结论。",
        )
        thread_id, run_id = await _create_run(seed, seed.owner_a)
        async with seed.factory() as session:
            v1_before = int(
                await session.scalar(
                    text(
                        """SELECT count(*) FROM user_project_memories
                           WHERE project_id=:project AND owner_user_id=:owner"""
                    ),
                    {
                        "project": seed.owner_a.project_id,
                        "owner": str(seed.owner_a.user_id),
                    },
                )
            )

        first = await _load(
            seed,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=run_id,
        )
        reused = await _load(
            seed,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=run_id,
        )

        assert reused.id == first.id
        assert reused.created_at == first.created_at
        assert first.version == fact.revision_sequence
        assert len(first.facts) == 1
        assert first.facts[0].id == fact.fact_id
        assert first.facts[0].revision_id == fact.revision_id
        assert first.facts[0].content_digest == fact.content_digest
        assert first.rendered_content == "[preference] 用户喜欢先看结论。"

        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT count(*) FROM run_memory_context_snapshots
                            WHERE project_id=:project AND owner_user_id=:owner
                              AND namespace='default' AND run_id=:run),
                           (SELECT count(*) FROM run_memory_context_items
                            WHERE project_id=:project AND owner_user_id=:owner
                              AND namespace='default' AND snapshot_id=:snapshot),
                           (SELECT count(*) FROM user_project_memories
                            WHERE project_id=:project AND owner_user_id=:owner)"""
                    ),
                    {
                        "project": seed.owner_a.project_id,
                        "owner": str(seed.owner_a.user_id),
                        "run": run_id,
                        "snapshot": first.id,
                    },
                )
            ).one()
        assert tuple(counts) == (1, 1, v1_before)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_v2_recall_pins_revision_ceiling_for_retry_and_new_run_reads_latest(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        original = await _insert_fact(
            seed,
            scope=seed.owner_a_scope,
            content="用户喜欢简短回答。",
        )
        first_thread_id, first_run_id = await _create_run(seed, seed.owner_a)
        frozen = await _load(
            seed,
            scope=seed.owner_a_scope,
            thread_id=first_thread_id,
            run_id=first_run_id,
        )

        changed_at = datetime.now(UTC) + timedelta(seconds=1)
        async with seed.factory() as session, session.begin():
            revised = await MemoryV2ManagementRepository(session).revise_fact(
                seed.owner_a_scope,
                namespace="default",
                fact_id=original.fact_id,
                expected_version=1,
                content="用户喜欢一句话结论。",
                category=None,
                confidence=None,
                reason="user_edit",
                now=changed_at,
            )
        added = await _insert_fact(
            seed,
            scope=seed.owner_a_scope,
            content="用户使用中文交流。",
            category="profile",
            created_at=changed_at + timedelta(seconds=1),
        )

        retried = await _load(
            seed,
            scope=seed.owner_a_scope,
            thread_id=first_thread_id,
            run_id=first_run_id,
        )
        assert retried.id == frozen.id
        assert retried.version == original.revision_sequence
        assert [(fact.revision_id, fact.content) for fact in retried.facts] == [(original.revision_id, original.content)]
        assert "一句话结论" not in retried.rendered_content
        assert "中文交流" not in retried.rendered_content

        latest_thread_id, latest_run_id = await _create_run(seed, seed.owner_a)
        latest = await _load(
            seed,
            scope=seed.owner_a_scope,
            thread_id=latest_thread_id,
            run_id=latest_run_id,
        )
        assert latest.version == added.revision_sequence
        assert {(fact.id, fact.revision_id, fact.content) for fact in latest.facts} == {
            (original.fact_id, revised.current_revision.id, "用户喜欢一句话结论。"),
            (added.fact_id, added.revision_id, added.content),
        }
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_v2_recall_applies_disabled_and_hard_forget_overlay_to_frozen_snapshot(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        disabled_fact = await _insert_fact(
            seed,
            scope=seed.owner_a_scope,
            content="这条记忆会被停用。",
        )
        forgotten_fact = await _insert_fact(
            seed,
            scope=seed.owner_a_scope,
            content="这条记忆会被彻底遗忘。",
        )
        thread_id, run_id = await _create_run(seed, seed.owner_a)
        frozen = await _load(
            seed,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=run_id,
        )
        assert {fact.id for fact in frozen.facts} == {
            disabled_fact.fact_id,
            forgotten_fact.fact_id,
        }

        changed_at = datetime.now(UTC) + timedelta(seconds=1)
        async with seed.factory() as session, session.begin():
            await MemoryV2ManagementRepository(session).set_fact_enabled(
                seed.owner_a_scope,
                namespace="default",
                fact_id=disabled_fact.fact_id,
                expected_version=1,
                enabled=False,
                now=changed_at,
            )

        after_disable = await _load(
            seed,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=run_id,
        )
        assert after_disable.id == frozen.id
        assert [fact.id for fact in after_disable.facts] == [forgotten_fact.fact_id]
        assert disabled_fact.content not in after_disable.rendered_content
        assert forgotten_fact.content in after_disable.rendered_content

        async with seed.factory() as session, session.begin():
            await MemoryV2ManagementRepository(session).hard_forget_fact(
                seed.owner_a_scope,
                namespace="default",
                fact_id=forgotten_fact.fact_id,
                expected_version=1,
                lineage_identity_hmac=hashlib.sha256(b"memory-pr7-forgotten-lineage").hexdigest(),
                lineage_hmac_key_version="memory-pr7-test-v1",
                now=changed_at + timedelta(seconds=1),
            )

        after_forget = await _load(
            seed,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=run_id,
        )
        assert after_forget.id == frozen.id
        assert after_forget.version == frozen.version
        assert after_forget.facts == ()
        assert after_forget.rendered_content == ""
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_v2_recall_empty_namespace_is_virtual_zero_and_owner_scoped(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        owner_a_fact = await _insert_fact(
            seed,
            scope=seed.owner_a_scope,
            content="仅 owner A 可见。",
        )
        owner_b_fact = await _insert_fact(
            seed,
            scope=seed.owner_b_scope,
            content="仅 owner B 可见。",
        )
        owner_a_thread, owner_a_run = await _create_run(seed, seed.owner_a)
        owner_b_thread, owner_b_run = await _create_run(seed, seed.owner_b)

        empty = await _load(
            seed,
            scope=seed.owner_a_scope,
            namespace="empty",
            thread_id=owner_a_thread,
            run_id=owner_a_run,
        )
        assert empty.version == 0
        assert empty.facts == ()
        assert empty.rendered_content == ""

        owner_a = await _load(
            seed,
            scope=seed.owner_a_scope,
            thread_id=owner_a_thread,
            run_id=owner_a_run,
        )
        owner_b = await _load(
            seed,
            scope=seed.owner_b_scope,
            thread_id=owner_b_thread,
            run_id=owner_b_run,
        )
        assert {fact.id for fact in owner_a.facts} == {owner_a_fact.fact_id}
        assert {fact.id for fact in owner_b.facts} == {owner_b_fact.fact_id}
        assert owner_b_fact.content not in owner_a.rendered_content
        assert owner_a_fact.content not in owner_b.rendered_content
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_v2_recall_concurrent_first_read_converges_on_one_snapshot(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        await _insert_fact(
            seed,
            scope=seed.owner_a_scope,
            content="并发读取只能冻结一次。",
        )
        thread_id, run_id = await _create_run(seed, seed.owner_a)

        first, second = await asyncio.gather(
            _load(
                seed,
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                run_id=run_id,
            ),
            _load(
                seed,
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                run_id=run_id,
            ),
        )
        assert first.id == second.id
        assert first.version == second.version
        assert first.facts == second.facts
        assert first.rendered_content == second.rendered_content

        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT count(*) FROM run_memory_context_snapshots
                            WHERE project_id=:project AND owner_user_id=:owner
                              AND namespace='default' AND run_id=:run),
                           (SELECT count(*) FROM run_memory_context_items
                            WHERE project_id=:project AND owner_user_id=:owner
                              AND namespace='default')"""
                    ),
                    {
                        "project": seed.owner_a.project_id,
                        "owner": str(seed.owner_a.user_id),
                        "run": run_id,
                    },
                )
            ).one()
        assert tuple(counts) == (1, 1)
    finally:
        await seed.engine.dispose()
