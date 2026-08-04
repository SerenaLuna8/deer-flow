from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from support.memory_v2_seed import admit_memory_extraction_job
from support.private_thread_seed import seed_private_thread_database

from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.errors import PrivateWorkConflict, PrivateWorkNotFound
from app.private_work.memory_v2_export import iter_memory_v2_export_records
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.worker.memory_extract import MemoryExtractJobHandler
from app.worker.service import JobLeaseAuthority, JobSettlement
from deerflow.agents.memory.extractor import (
    ExtractedMemoryCandidate,
    MemoryExtractionResult,
)
from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.private_work.file_repository import PrivateFileRepository
from deerflow.persistence.private_work.memory_v2_management import (
    MemoryV2ManagementRepository,
)
from deerflow.persistence.private_work.memory_v2_model import (
    RunMemoryContextSnapshotRow,
)
from deerflow.persistence.private_work.model import PrivateArtifactRow
from deerflow.runtime.events.store.db import DbRunEventStore


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
        return SimpleNamespace(name="memory-pr6-privacy-test")


class _Extractor:
    def __init__(self, content: str) -> None:
        self._content = content

    async def extract(self, _sources) -> MemoryExtractionResult:
        return MemoryExtractionResult(
            candidates=(
                ExtractedMemoryCandidate(
                    source_ordinal=0,
                    candidate_type="preference",
                    content=self._content,
                    confidence=0.95,
                    retention_class="durable",
                    sensitivity="normal",
                ),
            )
        )


@dataclass(frozen=True, slots=True)
class _SeededFact:
    thread_id: str
    run_id: str
    candidate_id: uuid.UUID
    fact_id: uuid.UUID


async def _seed_fact(seed, content: str) -> _SeededFact:
    admitted, claim = await admit_memory_extraction_job(
        seed,
        messages=[
            {
                "role": "user",
                "id": f"memory-pr6-source-{uuid.uuid4()}",
                "content": content,
            }
        ],
    )
    settlement = await MemoryExtractJobHandler(
        seed.factory,
        app_config=None,
        model_materializer=_ModelMaterializer(),
        runtime_policy_materializer=_PolicyMaterializer(),
        extractor_factory=lambda _model: _Extractor(content),
    )(
        claim,
        JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
    )
    assert isinstance(settlement, JobSettlement)
    await settlement.commit()

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
            now=datetime.now(UTC),
        )
    return _SeededFact(
        thread_id=admitted.run.thread_id,
        run_id=admitted.run.run_id,
        candidate_id=candidate.id,
        fact_id=fact.id,
    )


def _record_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(map(_record_keys, value.values())))
    if isinstance(value, list | tuple):
        return set().union(*(map(_record_keys, value)))
    return set()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_v2_export_is_owner_scoped_and_omits_integrity_material(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        content = "owner-a exportable memory"
        await _seed_fact(seed, content)

        async with seed.factory() as session:
            owner_a_records = [
                (record_type, data)
                async for record_type, data in iter_memory_v2_export_records(
                    session,
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    namespace="default",
                )
            ]
            owner_b_records = [
                (record_type, data)
                async for record_type, data in iter_memory_v2_export_records(
                    session,
                    project_id=seed.owner_b.project_id,
                    owner_user_id=str(seed.owner_b.user_id),
                    namespace="default",
                )
            ]
            integrity_material = (
                await session.execute(
                    text(
                        """SELECT b.source_identity_digest,b.policy_checksum,
                                  i.content_hmac,g.contract_digest,
                                  g.model_config_checksum,c.content_digest,
                                  r.content_digest,e.source_identity_hmac
                           FROM memory_source_batches b
                           JOIN memory_source_items i ON i.source_batch_id=b.id
                           JOIN memory_extraction_generations g
                             ON g.source_batch_id=b.id
                           JOIN memory_candidates c
                             ON c.extraction_generation_id=g.id
                           JOIN memory_fact_revisions r
                             ON r.source_candidate_id=c.id
                           JOIN memory_fact_evidence e
                             ON e.revision_id=r.id
                           WHERE b.project_id=:project
                             AND b.owner_user_id=:owner"""
                    ),
                    {
                        "project": seed.owner_a.project_id,
                        "owner": str(seed.owner_a.user_id),
                    },
                )
            ).one()

        record_types = {record_type for record_type, _data in owner_a_records}
        assert {
            "memory_v2_source_batch",
            "memory_v2_source_item",
            "memory_v2_extraction_generation",
            "memory_v2_candidate",
            "memory_v2_fact",
            "memory_v2_fact_revision",
            "memory_v2_fact_evidence",
        }.issubset(record_types)
        assert owner_b_records == []
        assert any(data.get("content") == content for _record_type, data in owner_a_records)
        exported_keys = {key.lower() for key in _record_keys(owner_a_records)}
        assert not any("hmac" in key or "checksum" in key for key in exported_keys)
        serialized = json.dumps(owner_a_records, ensure_ascii=False, sort_keys=True)
        assert all(str(value) not in serialized for value in integrity_material)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_active_run_delete_conflicts_without_mutating_the_run(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"memory-pr6-active-run-{uuid.uuid4()}"
    run_id = f"memory-pr6-active-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await PrivateRunRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                request=PrivateRunCreate(
                    run_id=run_id,
                    status="running",
                    metadata={"private": "active-metadata"},
                    kwargs={"input": {"messages": ["active-request"]}},
                    model_name="active-model",
                ),
            )

        service = PrivateRunService(seed.factory)
        with pytest.raises(PrivateWorkConflict):
            await service.delete(seed.owner_a, thread_id, run_id)

        visible = await service.get(seed.owner_a, thread_id, run_id)
        assert visible.status == "running"
        assert visible.metadata == {"private": "active-metadata"}
        assert visible.kwargs == {"input": {"messages": ["active-request"]}}
        assert visible.model_name == "active-model"
        async with seed.factory() as session:
            persisted = (
                await session.execute(
                    text(
                        """SELECT status,metadata_json,kwargs_json,model_name
                           FROM runs WHERE run_id=:run"""
                    ),
                    {"run": run_id},
                )
            ).one()
        assert tuple(persisted) == (
            "running",
            {"private": "active-metadata"},
            {"input": {"messages": ["active-request"]}},
            "active-model",
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_specific", "reason"),
    (
        (True, "run_deleted"),
        (False, "thread_deleted"),
    ),
)
async def test_memory_v2_source_erasure_detaches_lineage_and_preserves_fact(
    migrated_postgres_database_url: str,
    run_specific: bool,
    reason: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        content = f"preserved fact after {reason}"
        seeded = await _seed_fact(seed, content)

        async with seed.factory() as session, session.begin():
            erased_items = await MemoryV2ManagementRepository(session).erase_sources(
                seed.owner_a_scope,
                thread_id=seeded.thread_id,
                run_id=seeded.run_id if run_specific else None,
                reason=reason,
                now=datetime.now(UTC),
            )
            assert erased_items == 1

        async with seed.factory() as session:
            source_state = (
                await session.execute(
                    text(
                        """SELECT i.content,i.source_erased_at IS NOT NULL,
                                  b.suppressed_at IS NOT NULL,b.suppression_reason
                           FROM memory_source_items i
                           JOIN memory_source_batches b ON b.id=i.source_batch_id
                           WHERE b.project_id=:project
                             AND b.owner_user_id=:owner
                             AND b.run_id=:run"""
                    ),
                    {
                        "project": seed.owner_a.project_id,
                        "owner": str(seed.owner_a.user_id),
                        "run": seeded.run_id,
                    },
                )
            ).one()
            candidate_state = (
                await session.execute(
                    text(
                        """SELECT status,content,content_erased_at IS NOT NULL,
                                  consolidation_generation_id
                           FROM memory_candidates WHERE id=:id"""
                    ),
                    {"id": seeded.candidate_id},
                )
            ).one()
            fact_state = (
                await session.execute(
                    text(
                        """SELECT f.status,r.content,r.source_candidate_id
                           FROM memory_facts f
                           JOIN memory_fact_revisions r
                             ON r.id=f.current_revision_id
                           WHERE f.id=:id"""
                    ),
                    {"id": seeded.fact_id},
                )
            ).one()
            evidence_state = (
                await session.execute(
                    text(
                        """SELECT source_candidate_id,source_item_id,thread_id,run_id,
                                  run_event_sequence,evidence_excerpt,
                                  source_erased_at IS NOT NULL
                           FROM memory_fact_evidence WHERE fact_id=:id"""
                    ),
                    {"id": seeded.fact_id},
                )
            ).one()
            suppressions = (
                await session.execute(
                    text(
                        """SELECT suppression_kind,reason
                           FROM memory_suppressions
                           WHERE project_id=:project AND owner_user_id=:owner"""
                    ),
                    {
                        "project": seed.owner_a.project_id,
                        "owner": str(seed.owner_a.user_id),
                    },
                )
            ).all()
            owner_b_suppressions = await session.scalar(
                text(
                    """SELECT count(*) FROM memory_suppressions
                       WHERE project_id=:project AND owner_user_id=:owner"""
                ),
                {
                    "project": seed.owner_b.project_id,
                    "owner": str(seed.owner_b.user_id),
                },
            )

        assert tuple(source_state) == (None, True, True, reason)
        assert tuple(candidate_state) == ("accepted", None, True, None)
        assert tuple(fact_state) == ("active", content, None)
        assert tuple(evidence_state) == (None, None, None, None, None, None, True)
        assert suppressions == [("source", reason)]
        assert owner_b_suppressions == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_thread_delete_removes_context_snapshot_without_source_batch(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"memory-pr6-empty-source-{uuid.uuid4()}"
    run_id = f"memory-pr6-empty-source-run-{uuid.uuid4()}"
    snapshot_id = uuid.uuid4()
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await PrivateRunRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                request=PrivateRunCreate(
                    run_id=run_id,
                    status="success",
                ),
            )
            session.add(
                RunMemoryContextSnapshotRow(
                    id=snapshot_id,
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    namespace="default",
                    thread_id=thread_id,
                    run_id=run_id,
                    pipeline_mode="v2",
                    fact_revision_ceiling=0,
                    summary_id=None,
                    summary_revision=None,
                    selection_version="memory-pr6-test",
                    renderer_version="memory-pr6-test",
                    prompt_version="memory-pr6-test",
                    policy_revision=1,
                    token_budget=100,
                    rendered_content="context snapshot without a source batch",
                    rendered_content_digest=hashlib.sha256(b"context snapshot without a source batch").hexdigest(),
                    content_erased_at=None,
                )
            )

        scoped = ProjectScopedCheckpointer(InMemorySaver(), seed.factory).for_context(seed.owner_a)
        await scoped.adelete_thread(thread_id)

        async with seed.factory() as session:
            snapshot_count = await session.scalar(
                text("SELECT count(*) FROM run_memory_context_snapshots WHERE id=:id"),
                {"id": snapshot_id},
            )
            thread_state = (
                await session.execute(
                    text(
                        """SELECT deleted_at IS NOT NULL,checkpoint_delete_status
                           FROM threads_meta
                           WHERE project_id=:project AND owner_user_id=:owner
                             AND thread_id=:thread"""
                    ),
                    {
                        "project": seed.owner_a.project_id,
                        "owner": str(seed.owner_a.user_id),
                        "thread": thread_id,
                    },
                )
            ).one()
        assert snapshot_count == 0
        assert tuple(thread_state) == (True, "complete")
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_delete_retains_hidden_shell_and_scrubs_private_body(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        seeded = await _seed_fact(seed, "private source erased by run deletion")
        thread_id = seeded.thread_id
        run_id = seeded.run_id
        await DbRunEventStore(seed.factory).put(
            thread_id=thread_id,
            run_id=run_id,
            event_type="human_message",
            category="message",
            content="private event body",
            scope=seed.owner_a_scope,
        )

        file_content = b"private artifact body"
        file_sha256 = hashlib.sha256(file_content).hexdigest()
        feedback_id = f"memory-pr6-feedback-{uuid.uuid4()}"
        artifact_id = uuid.uuid4()
        async with seed.factory() as session, session.begin():
            files = PrivateFileRepository(session)
            staged = await files.stage(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                kind="output",
                logical_path="outputs/private.txt",
                media_type="text/plain",
                created_by_run_id=run_id,
            )
            await files.append_chunk(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                file_id=staged.id,
                chunk_index=0,
                content=file_content,
                size=len(file_content),
                sha256=file_sha256,
            )
            await files.finalize(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                file_id=staged.id,
                expected_size=len(file_content),
                expected_sha256=file_sha256,
            )
            session.add_all(
                (
                    FeedbackRow(
                        feedback_id=feedback_id,
                        project_id=seed.owner_a.project_id,
                        owner_user_id=str(seed.owner_a.user_id),
                        thread_id=thread_id,
                        run_id=run_id,
                        message_id=None,
                        rating=1,
                        comment="private feedback",
                    ),
                    PrivateArtifactRow(
                        id=artifact_id,
                        project_id=seed.owner_a.project_id,
                        owner_user_id=str(seed.owner_a.user_id),
                        thread_id=thread_id,
                        run_id=run_id,
                        file_id=staged.id,
                        display_name="private.txt",
                        media_type="text/plain",
                        artifact_metadata={"private": "artifact-secret"},
                    ),
                )
            )
            await session.execute(
                text(
                    """UPDATE runs
                       SET assistant_id='private-assistant',
                           metadata_json=CAST(:metadata AS json),
                           kwargs_json=CAST(:kwargs AS json),
                           error='private-error',message_count=2,
                           first_human_message='private-question',
                           last_ai_message='private-answer',
                           model_name='private-model',
                           token_usage_by_model=CAST(:token_usage AS json)
                       WHERE run_id=:run"""
                ),
                {
                    "metadata": json.dumps({"private": "metadata-secret"}),
                    "kwargs": json.dumps({"input": {"messages": ["private-request"]}}),
                    "run": run_id,
                    "token_usage": json.dumps({"private-model": {"total_tokens": 42}}),
                },
            )
            retained_identity = (
                await session.execute(
                    text(
                        """SELECT project_id,owner_user_id,thread_id,run_id,
                                  origin_trace_id,job_id
                           FROM runs WHERE run_id=:run"""
                    ),
                    {"run": run_id},
                )
            ).one()

        service = PrivateRunService(seed.factory)
        await service.delete(seed.owner_a, thread_id, run_id)

        with pytest.raises(PrivateWorkNotFound):
            await service.get(seed.owner_a, thread_id, run_id)
        assert await service.list(seed.owner_a, thread_id) == ()

        async with seed.factory() as session:
            shell = (
                await session.execute(
                    text(
                        """SELECT project_id,owner_user_id,thread_id,run_id,
                                  origin_trace_id,job_id,status,
                                  assistant_id,metadata_json,kwargs_json,error,
                                  first_human_message,last_ai_message,model_name,
                                  token_usage_by_model,follow_up_to_run_id
                           FROM runs WHERE run_id=:run"""
                    ),
                    {"run": run_id},
                )
            ).one()
            event_count = await session.scalar(
                text("SELECT count(*) FROM run_events WHERE run_id=:run"),
                {"run": run_id},
            )
            source_state = (
                await session.execute(
                    text(
                        """SELECT i.content,i.source_erased_at IS NOT NULL,
                                  b.suppressed_at IS NOT NULL,b.suppression_reason
                           FROM memory_source_items i
                           JOIN memory_source_batches b ON b.id=i.source_batch_id
                           WHERE b.run_id=:run"""
                    ),
                    {"run": run_id},
                )
            ).one()
            fact_status = await session.scalar(
                text("SELECT status FROM memory_facts WHERE id=:id"),
                {"id": seeded.fact_id},
            )
            dependent_counts = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT count(*) FROM run_events WHERE run_id=:run),
                           (SELECT count(*) FROM feedback WHERE feedback_id=:feedback),
                           (SELECT count(*) FROM artifacts WHERE id=:artifact),
                           (SELECT count(*) FROM files WHERE id=:file)"""
                    ),
                    {
                        "artifact": artifact_id,
                        "feedback": feedback_id,
                        "file": staged.id,
                        "run": run_id,
                    },
                )
            ).one()
        assert tuple(shell[:6]) == tuple(retained_identity)
        assert tuple(shell[6:]) == (
            "deleted",
            None,
            {},
            {},
            None,
            None,
            None,
            None,
            {},
            None,
        )
        assert event_count == 0
        assert tuple(dependent_counts) == (0, 0, 0, 1)
        assert tuple(source_state) == (None, True, True, "run_deleted")
        assert fact_status == "active"
    finally:
        await seed.engine.dispose()
