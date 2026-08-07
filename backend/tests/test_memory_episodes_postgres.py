"""Real-PostgreSQL episodic archive: settlement transfer, trgm, governance."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from support.private_thread_seed import seed_private_thread_database

from app.private_work.retention_purge import purge_private_scope
from deerflow.agents.memory.dream import DREAM_PROMPT_VERSION, EMPTY_MEMORY_DOCUMENT
from deerflow.persistence.jobs.model import WorkerNodeRow
from deerflow.persistence.jobs.sql import JobClaim, JobOwnerRef, JobRepository
from deerflow.persistence.private_work.memory_document_model import (
    MemoryEpisodeRow,
    MemoryHistoryEntryRow,
)
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryDreamFrozenRuntime,
    memory_document_digest,
)


def _owner_ref(_owner_user_id: str) -> JobOwnerRef:
    return JobOwnerRef(key_id="memory-test", hmac_hex="f" * 64)


def _jobs(session) -> JobRepository:
    return JobRepository(session, owner_ref_hasher=_owner_ref)


async def _seed_model_version(seed, scope) -> uuid.UUID:
    model_id = uuid.uuid4()
    model_version_id = uuid.uuid4()
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO system_model_configs
                (id,logical_name,display_name,description,status,
                 current_version_id,revision,sort_order,created_by_user_id,
                 updated_by_user_id)
                VALUES (:id,:name,'Episode test','Episode PG test',
                        'active',NULL,1,0,:owner,:owner)"""
            ),
            {
                "id": model_id,
                "name": f"episode-test-{model_id.hex}",
                "owner": scope.owner_user_id,
            },
        )
        await connection.execute(
            text(
                """INSERT INTO system_model_config_versions
                (id,model_config_id,version_number,provider_adapter,
                 provider_model,settings,supports_thinking,
                 supports_reasoning_effort,supports_vision,credential_id,
                 credential_version_id,credential_env_key,payload_checksum,
                 supersedes_version_id,created_by_user_id)
                VALUES (:id,:model,1,'codex_cli','episode-test',
                        '{}'::jsonb,false,false,false,NULL,NULL,NULL,
                        :checksum,NULL,:owner)"""
            ),
            {
                "id": model_version_id,
                "model": model_id,
                "checksum": "a" * 64,
                "owner": scope.owner_user_id,
            },
        )
        await connection.execute(
            text("UPDATE system_model_configs SET current_version_id=:version WHERE id=:model"),
            {"version": model_version_id, "model": model_id},
        )
    return model_version_id


def _history_entry(scope, *, index: int, model_version_id: uuid.UUID, created_at: datetime) -> MemoryHistoryEntryRow:
    tagged_text = f"- [durable] episode-fact-{index:02d}"
    return MemoryHistoryEntryRow(
        id=uuid.uuid4(),
        project_id=scope.project_id,
        owner_user_id=scope.owner_user_id,
        namespace=scope.namespace,
        thread_id="episode-pg",
        source_checkpoint_id=f"source-{index}",
        committed_checkpoint_id=f"committed-{index}",
        source_digest=hashlib.sha256(f"episode-source-{index}".encode()).hexdigest(),
        status="pending",
        tagged_text=tagged_text,
        content_digest=hashlib.sha256(tagged_text.encode()).hexdigest(),
        preference_version=1,
        snip_prompt_version="snip-prompt-v1",
        summary_model_ref=model_version_id,
        created_at=created_at,
    )


def _episode(scope, *, occurred_at: datetime, tagged_text: str) -> MemoryEpisodeRow:
    return MemoryEpisodeRow(
        id=uuid.uuid4(),
        project_id=scope.project_id,
        owner_user_id=scope.owner_user_id,
        namespace=scope.namespace,
        thread_id="episode-pg",
        origin="snip",
        tagged_text=tagged_text,
        content_digest=hashlib.sha256(tagged_text.encode()).hexdigest(),
        occurred_at=occurred_at,
        consumed_dream_job_id=uuid.uuid4(),
        created_at=occurred_at,
    )


async def _claim_dream(factory, *, worker_id: uuid.UUID, now: datetime) -> JobClaim:
    async with factory() as session, session.begin():
        jobs = _jobs(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"memory_dream"}),
            lease_seconds=60,
            now=now,
        )
        assert claim is not None
        assert await jobs.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
            now=now,
        )
        return claim


@pytest.mark.postgres
@pytest.mark.anyio
async def test_postgres_settlement_transfers_history_into_episodes(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = MemoryDocumentScope(
        project_id=uuid.UUID(seed.owner_a.resource_scope.project_id),
        owner_user_id=seed.owner_a.resource_scope.owner_user_id,
    )
    base_time = datetime.now(UTC)
    worker_id = uuid.uuid4()
    try:
        model_version_id = await _seed_model_version(seed, scope)
        frozen = MemoryDreamFrozenRuntime(
            preference_version=1,
            policy_revision=1,
            model_config_id=uuid.uuid4(),
            model_version_id=model_version_id,
            model_payload_checksum="a" * 64,
            prompt_version=DREAM_PROMPT_VERSION,
        )

        expired_text = "- [ephemeral] long-forgotten"
        async with seed.factory() as session, session.begin():
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="memory-episode-test",
                    capabilities_json=["memory_dream"],
                    max_concurrent_jobs=1,
                    draining=False,
                    started_at=base_time,
                    heartbeat_at=base_time,
                )
            )
            for index in range(1, 4):
                session.add(
                    _history_entry(
                        scope,
                        index=index,
                        model_version_id=model_version_id,
                        created_at=base_time + timedelta(microseconds=index),
                    )
                )
            # Older than the 365-day default retention: pruned at settlement.
            session.add(
                _episode(
                    scope,
                    occurred_at=base_time - timedelta(days=400),
                    tagged_text=expired_text,
                )
            )

        async with seed.factory() as session, session.begin():
            admission = await MemoryDocumentRepository(session, jobs=_jobs(session)).admit_dream(
                scope,
                trigger="manual_dream",
                frozen=frozen,
                initial_content=EMPTY_MEMORY_DOCUMENT,
                now=base_time,
            )
        assert admission.disposition == "queued"
        assert admission.history_count == 3
        job_id = admission.job_id
        assert job_id is not None

        async with seed.factory() as session:
            history_rows = tuple(
                (
                    await session.execute(
                        sa.select(MemoryHistoryEntryRow).where(MemoryHistoryEntryRow.dream_job_id == job_id).order_by(MemoryHistoryEntryRow.sequence),
                    )
                ).scalars()
            )
            run_digest = await session.scalar(text("SELECT history_digest FROM memory_dream_runs WHERE job_id=:job"), {"job": job_id})
            expected_by_id = {row.id: (row.origin, row.tagged_text, row.created_at) for row in history_rows}
        assert run_digest is not None

        claim = await _claim_dream(
            seed.factory,
            worker_id=worker_id,
            now=base_time + timedelta(seconds=1),
        )
        assert claim.job_id == job_id
        changed_content = EMPTY_MEMORY_DOCUMENT.replace(
            "# 项目背景",
            "# 项目背景\n\n- episodes 转入测试。",
        )

        async def finalize(now: datetime):
            async with seed.factory() as session, session.begin():
                return await MemoryDocumentRepository(session, jobs=_jobs(session)).finalize_dream(
                    scope,
                    job_id=job_id,
                    lease_token=claim.lease_token,
                    expected_history_digest=run_digest,
                    expected_base_version=0,
                    expected_base_digest=memory_document_digest(EMPTY_MEMORY_DOCUMENT),
                    content=changed_content,
                    now=now,
                )

        version = await finalize(base_time + timedelta(seconds=2))
        assert version.version == 1

        async with seed.factory() as session:
            episodes = tuple(
                (
                    await session.execute(
                        sa.select(MemoryEpisodeRow).where(
                            MemoryEpisodeRow.project_id == scope.project_id,
                            MemoryEpisodeRow.owner_user_id == scope.owner_user_id,
                            MemoryEpisodeRow.namespace == scope.namespace,
                        )
                    )
                ).scalars()
            )
            consumed = tuple(
                (
                    await session.execute(
                        sa.select(MemoryHistoryEntryRow).where(MemoryHistoryEntryRow.dream_job_id == job_id),
                    )
                ).scalars()
            )
            trgm_extension = await session.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='pg_trgm'"))
            trgm_index = await session.scalar(text("SELECT count(*) FROM pg_indexes WHERE tablename='memory_episodes' AND indexname='ix_memory_episodes_trgm'"))
            ranked = list(
                (
                    await session.execute(
                        text(
                            """SELECT tagged_text FROM memory_episodes
                            WHERE project_id=:project AND owner_user_id=:owner AND namespace=:namespace
                            ORDER BY similarity(tagged_text, 'episode-fact') DESC, occurred_at DESC"""
                        ),
                        {
                            "project": scope.project_id,
                            "owner": scope.owner_user_id,
                            "namespace": scope.namespace,
                        },
                    )
                ).scalars()
            )

        assert {episode.id for episode in episodes} == set(expected_by_id)
        for episode in episodes:
            origin, tagged_text, created_at = expected_by_id[episode.id]
            assert episode.origin == origin == "snip"
            assert episode.tagged_text == tagged_text
            assert episode.occurred_at == created_at
            assert episode.consumed_dream_job_id == job_id
        # Tombstone semantics survive: consumed rows keep identity, lose text.
        assert all(row.status == "consumed" and row.tagged_text is None and row.consumed_at is not None for row in consumed)
        # The 400-day-old episode fell out of the default retention window.
        assert all(episode.tagged_text != expired_text for episode in episodes)
        assert trgm_extension == 1
        assert trgm_index == 1
        assert ranked[0].startswith("- [durable] episode-fact-")

        # Retried settlement is answered from the existing version row.
        retried = await finalize(base_time + timedelta(seconds=3))
        assert retried.version == 1
        async with seed.factory() as session:
            episode_count = await session.scalar(sa.select(sa.func.count()).select_from(MemoryEpisodeRow))
        assert episode_count == 3

        with pytest.raises(IntegrityError):
            async with seed.factory() as session, session.begin():
                bogus = _episode(
                    scope,
                    occurred_at=base_time,
                    tagged_text="- [durable] bogus-origin",
                )
                bogus.origin = "manual"
                session.add(bogus)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_postgres_recall_search_ranks_and_pages_episodes(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = MemoryDocumentScope(
        project_id=uuid.UUID(seed.owner_a.resource_scope.project_id),
        owner_user_id=seed.owner_a.resource_scope.owner_user_id,
    )
    base_time = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            session.add(
                _episode(
                    scope,
                    occurred_at=base_time - timedelta(days=3),
                    tagged_text="- [durable] deployment target is region-eu",
                )
            )
            session.add(
                _episode(
                    scope,
                    occurred_at=base_time - timedelta(days=1),
                    tagged_text="- [ephemeral] deployment window pending approval",
                )
            )
            session.add(
                _episode(
                    scope,
                    occurred_at=base_time - timedelta(days=2),
                    tagged_text="- [permanent] user prefers 100% test coverage",
                )
            )

        async with seed.factory() as session:
            repository = MemoryDocumentRepository(session, jobs=_jobs(session))

            ranked = await repository.search_episodes(
                scope,
                query="deployment target",
                limit=5,
                retention_days=365,
                now=base_time,
            )
            # Exact substring outranks the merely trigram-similar row even
            # though the similar row is newer.
            assert [record.tagged_text for record in ranked] == [
                "- [durable] deployment target is region-eu",
                "- [ephemeral] deployment window pending approval",
            ]

            tagged = await repository.search_episodes(
                scope,
                query="deployment",
                tags=("ephemeral",),
                limit=5,
                retention_days=365,
                now=base_time,
            )
            assert [record.tagged_text for record in tagged] == ["- [ephemeral] deployment window pending approval"]

            # LIKE metacharacters in the query must be literal, not wildcards.
            literal = await repository.search_episodes(
                scope,
                query="100% test",
                limit=5,
                retention_days=365,
                now=base_time,
            )
            assert literal
            assert literal[0].tagged_text == "- [permanent] user prefers 100% test coverage"

            first_page = await repository.list_episodes(
                scope,
                limit=2,
                retention_days=365,
                now=base_time,
            )
            assert [record.tagged_text for record in first_page] == [
                "- [ephemeral] deployment window pending approval",
                "- [permanent] user prefers 100% test coverage",
            ]
            second_page = await repository.list_episodes(
                scope,
                before=first_page[-1].occurred_at,
                limit=2,
                retention_days=365,
                now=base_time,
            )
            assert [record.tagged_text for record in second_page] == ["- [durable] deployment target is region-eu"]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_postgres_reset_and_retention_purge_delete_episodes(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope_a = MemoryDocumentScope(
        project_id=uuid.UUID(seed.owner_a.resource_scope.project_id),
        owner_user_id=seed.owner_a.resource_scope.owner_user_id,
    )
    scope_b = MemoryDocumentScope(
        project_id=uuid.UUID(seed.owner_b.resource_scope.project_id),
        owner_user_id=seed.owner_b.resource_scope.owner_user_id,
    )
    base_time = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            for offset in range(2):
                session.add(
                    _episode(
                        scope_a,
                        occurred_at=base_time - timedelta(hours=offset),
                        tagged_text=f"- [durable] owner-a-{offset}",
                    )
                )
            session.add(
                _episode(
                    scope_b,
                    occurred_at=base_time,
                    tagged_text="- [durable] owner-b-keeps-scope",
                )
            )

        async with seed.factory() as session, session.begin():
            counts = await MemoryDocumentRepository(session, jobs=_jobs(session)).reset_owner(
                scope_a.owner_user_id,
                now=base_time,
            )
        assert counts.episodes == 2

        async with seed.factory() as session:
            owners = set(
                (
                    await session.execute(
                        sa.select(MemoryEpisodeRow.owner_user_id),
                    )
                ).scalars()
            )
        assert owners == {scope_b.owner_user_id}

        async with seed.factory() as session, session.begin():
            await purge_private_scope(
                session,
                project_id=scope_b.project_id,
                owner_user_id=scope_b.owner_user_id,
            )

        async with seed.factory() as session:
            remaining = await session.scalar(sa.select(sa.func.count()).select_from(MemoryEpisodeRow))
        assert remaining == 0
    finally:
        await seed.engine.dispose()
