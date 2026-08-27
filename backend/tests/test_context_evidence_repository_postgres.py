from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from support.private_thread_seed import seed_private_thread_database

from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.persistence.context_evidence import (
    ContextEvidenceAppend,
    ContextEvidenceIdempotencyConflict,
    ContextEvidenceRepository,
    ContextEvidenceScope,
    ContextPayloadUnsafe,
    ContextProjectionConflict,
    ContextProjectionHeadWrite,
    ContextRetentionPurgeCounts,
    ContextSubjectRef,
)
from deerflow.persistence.context_evidence.model import (
    ContextEvidenceRow,
)
from deerflow.runtime.context_evidence import (
    CompactionProjection,
    ContextModelProjection,
    ContextProjectionHead,
    ContextProjectionSource,
    ContextProjector,
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProjectionFreshness,
    ProjectionPhase,
    ProviderCallIdentity,
    RequestPreparedV1,
)

pytestmark = pytest.mark.postgres


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _projection_write(
    *,
    thread_id: str,
    subject: ContextSubjectRef,
    generation_id: uuid.UUID,
    projection_seq: int | None,
    evidence_seq: int,
    projector_revision: str = "context-projector-v1",
    phase: str = "idle",
    checkpoint_id: str | None = None,
) -> ContextProjectionHeadWrite:
    core_subject = (
        ContextSubject.lead_thread(thread_id=thread_id)
        if subject.kind == "lead_thread"
        else ContextSubject.subagent_task(
            thread_id=thread_id,
            execution_id=subject.subject_id,
        )
    )
    source = ContextProjectionSource(
        subject=core_subject,
        phase=ProjectionPhase(phase),
        generation=ContextWindowGeneration(generation_id=generation_id),
        checkpoint_id=checkpoint_id,
        model=ContextModelProjection(
            identity_digest="f" * 64,
            context_window_tokens=300_000,
        ),
        measurement=FinalRequestMeasurement(
            request_fingerprint="e" * 64,
            adapter_revision="test-cost-v1",
            contributions=(),
        ),
        current_provider_call_id=None,
        compaction=CompactionProjection(enabled=False, reached=False),
        freshness=ProjectionFreshness.CURRENT,
    )
    head = ContextProjector.rebuild(
        source=source,
        evidence=(),
        projection_seq=str(projection_seq or 0),
        projector_revision=projector_revision,
        as_of=datetime(2026, 8, 27, tzinfo=UTC),
    )
    head = ContextProjectionHead.from_safe_mapping(
        {
            **head.to_safe_mapping(),
            "evidence_seq": str(evidence_seq),
        }
    )
    write = ContextProjectionHeadWrite.from_safe_contract(head)
    return replace(write, projection_seq=projection_seq)


@pytest.mark.asyncio
async def test_evidence_and_projection_commit_atomically_in_the_caller_transaction(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    scope = ContextEvidenceScope.from_resource(seed.owner_a_scope, thread_id)
    subject = ContextSubjectRef.lead_thread(thread_id)
    generation_id = uuid.uuid4()
    try:
        async with seed.factory.begin() as session:
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(
                    asset_id=seed.project_agent_id,
                    scope="project",
                ),
            )
            repository = ContextEvidenceRepository(session)
            reserved = await repository.reserve(
                scope,
                evidence_count=1,
                projection_count=1,
            )
            evidence = await repository.append(
                scope,
                ContextEvidenceAppend(
                    subject=subject,
                    context_window_generation=generation_id,
                    event_type="context.window.opened.v1",
                    origin_run_id=None,
                    provider_call_id=None,
                    checkpoint_id="checkpoint-1",
                    idempotency_key=_digest("opened"),
                    payload={
                        "event_type": "context.window.opened.v1",
                        "model_identity_digest": "a" * 64,
                        "context_window_tokens": 300_000,
                        "compaction_enabled": False,
                    },
                ),
                evidence_seq=reserved.first_evidence_seq,
            )
            head = await repository.upsert_head(
                scope,
                _projection_write(
                    thread_id=thread_id,
                    subject=subject,
                    projection_seq=reserved.first_projection_seq,
                    evidence_seq=evidence.evidence_seq,
                    generation_id=generation_id,
                    phase="idle",
                    checkpoint_id="checkpoint-1",
                ),
            )

        async with seed.factory() as session:
            repository = ContextEvidenceRepository(session)
            page = await repository.page_evidence(scope, after_seq=0, limit=10)
            persisted_head = await repository.read_head(scope, subject)

        assert [item.event_type for item in page] == ["context.window.opened.v1"]
        assert page[0].evidence_seq == 1
        assert persisted_head == head
        assert persisted_head is not None
        assert persisted_head.projection_seq == 1
        assert persisted_head.projection["totals"]["projected_tokens"] == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_subject_evidence_paging_is_scoped_sparse_and_strict(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    scope = ContextEvidenceScope.from_resource(seed.owner_a_scope, thread_id)
    lead = ContextSubjectRef.lead_thread(thread_id)
    task = ContextSubjectRef.subagent_task(uuid.uuid4())
    generation_id = uuid.uuid4()
    try:
        async with seed.factory.begin() as session:
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(
                    asset_id=seed.project_agent_id,
                    scope="project",
                ),
            )
            repository = ContextEvidenceRepository(session)
            for index, subject in enumerate((lead, task, lead, task), start=1):
                await repository.append(
                    scope,
                    ContextEvidenceAppend(
                        subject=subject,
                        context_window_generation=generation_id,
                        event_type="context.window.opened.v1",
                        origin_run_id="run-1",
                        provider_call_id=None,
                        checkpoint_id=None,
                        idempotency_key=_digest(f"subject-page-{index}"),
                        payload={
                            "event_type": "context.window.opened.v1",
                            "model_identity_digest": f"{index:x}" * 64,
                            "context_window_tokens": 300_000,
                            "compaction_enabled": False,
                        },
                    ),
                )

        async with seed.factory() as session:
            repository = ContextEvidenceRepository(session)
            lead_page = await repository.page_subject_evidence(
                scope,
                lead,
                after_seq=0,
                limit=10,
            )
            task_page = await repository.page_subject_evidence(
                scope,
                task,
                after_seq=2,
                limit=1,
            )
            lead_opened = await repository.page_subject_event_evidence(
                scope,
                lead,
                "context.window.opened.v1",
                origin_run_id="run-1",
                generation_id=generation_id,
                after_seq=0,
                limit=10,
            )
            latest_task = await repository.read_latest_subject_evidence(
                scope,
                task,
            )

            assert [item.evidence_seq for item in lead_page] == [1, 3]
            assert [item.evidence_seq for item in task_page] == [4]
            assert [item.evidence_seq for item in lead_opened] == [1, 3]
            assert latest_task is not None
            assert latest_task.evidence_seq == 4
            assert all(item.subject == lead for item in lead_page)
            assert all(item.subject == task for item in task_page)

            with pytest.raises(ValueError, match="scope is required"):
                await repository.page_subject_evidence(
                    object(),  # type: ignore[arg-type]
                    lead,
                    after_seq=0,
                    limit=10,
                )
            with pytest.raises(ValueError, match="does not belong"):
                await repository.page_subject_evidence(
                    scope,
                    ContextSubjectRef.lead_thread("another-thread"),
                    after_seq=0,
                    limit=10,
                )
            with pytest.raises(ValueError, match="Subject is invalid"):
                await repository.page_subject_evidence(
                    scope,
                    object(),  # type: ignore[arg-type]
                    after_seq=0,
                    limit=10,
                )
            with pytest.raises(ValueError, match="cursor is invalid"):
                await repository.page_subject_evidence(
                    scope,
                    lead,
                    after_seq=True,
                    limit=10,
                )
            with pytest.raises(ValueError, match="limit is invalid"):
                await repository.page_subject_evidence(
                    scope,
                    lead,
                    after_seq=0,
                    limit=1001,
                )
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_provider_call_evidence_paging_is_subject_scoped_and_strict(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    scope = ContextEvidenceScope.from_resource(seed.owner_a_scope, thread_id)
    lead_ref = ContextSubjectRef.lead_thread(thread_id)
    task_ref = ContextSubjectRef.subagent_task(uuid.uuid4())
    generation = ContextWindowGeneration(generation_id=uuid.uuid4())
    lead_subject = ContextSubject.lead_thread(thread_id=thread_id)
    task_subject = ContextSubject.subagent_task(
        thread_id=thread_id,
        execution_id=task_ref.subject_id,
    )
    lead_measurement = FinalRequestMeasurement(
        request_fingerprint="a" * 64,
        adapter_revision="provider-page-test-v1",
        contributions=(),
    )
    task_measurement = FinalRequestMeasurement(
        request_fingerprint="b" * 64,
        adapter_revision="provider-page-test-v1",
        contributions=(),
    )
    lead_call = ProviderCallIdentity.derive(
        subject=lead_subject,
        generation=generation,
        source_checkpoint_id="checkpoint-lead",
        graph_step="lead:model",
        model_call_ordinal=0,
        request_fingerprint=lead_measurement.request_fingerprint,
    )
    task_call = ProviderCallIdentity.derive(
        subject=task_subject,
        generation=generation,
        source_checkpoint_id="task-state-1",
        graph_step="subagent:model",
        model_call_ordinal=0,
        request_fingerprint=task_measurement.request_fingerprint,
    )
    try:
        async with seed.factory.begin() as session:
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(
                    asset_id=seed.project_agent_id,
                    scope="project",
                ),
            )
            repository = ContextEvidenceRepository(session)
            for index, (subject_ref, call, measurement) in enumerate(
                (
                    (lead_ref, lead_call, lead_measurement),
                    (task_ref, task_call, task_measurement),
                ),
                start=1,
            ):
                payload = RequestPreparedV1(
                    provider_call=call,
                    measurement=measurement,
                ).model_dump(mode="json", exclude_none=True)
                await repository.append(
                    scope,
                    ContextEvidenceAppend(
                        subject=subject_ref,
                        context_window_generation=uuid.UUID(
                            generation.generation_id,
                        ),
                        event_type="request.prepared.v1",
                        origin_run_id="run-1",
                        provider_call_id=call.provider_call_id,
                        checkpoint_id=call.source_checkpoint_id,
                        idempotency_key=_digest(f"provider-page-{index}"),
                        payload=payload,
                    ),
                )

        async with seed.factory() as session:
            repository = ContextEvidenceRepository(session)
            lead_page = await repository.page_provider_call_evidence(
                scope,
                lead_ref,
                lead_call.provider_call_id,
                after_seq=0,
                limit=10,
            )
            wrong_subject_page = await repository.page_provider_call_evidence(
                scope,
                lead_ref,
                task_call.provider_call_id,
                after_seq=0,
                limit=10,
            )
            lead_ordinal = await repository.count_subject_run_prepared_requests(
                scope,
                lead_ref,
                "run-1",
            )
            task_ordinal = await repository.count_subject_run_prepared_requests(
                scope,
                task_ref,
                "run-1",
            )

            assert [item.evidence_seq for item in lead_page] == [1]
            assert all(item.subject == lead_ref for item in lead_page)
            assert wrong_subject_page == ()
            assert lead_ordinal == 1
            assert task_ordinal == 1

            with pytest.raises(ValueError, match="scope is required"):
                await repository.page_provider_call_evidence(
                    object(),  # type: ignore[arg-type]
                    lead_ref,
                    lead_call.provider_call_id,
                    after_seq=0,
                    limit=10,
                )
            with pytest.raises(ValueError, match="does not belong"):
                await repository.page_provider_call_evidence(
                    scope,
                    ContextSubjectRef.lead_thread("another-thread"),
                    lead_call.provider_call_id,
                    after_seq=0,
                    limit=10,
                )
            with pytest.raises(ValueError, match="call identity is invalid"):
                await repository.page_provider_call_evidence(
                    scope,
                    lead_ref,
                    "not-a-provider-call",
                    after_seq=0,
                    limit=10,
                )
            with pytest.raises(ValueError, match="cursor is invalid"):
                await repository.page_provider_call_evidence(
                    scope,
                    lead_ref,
                    lead_call.provider_call_id,
                    after_seq=True,
                    limit=10,
                )
            with pytest.raises(ValueError, match="limit is invalid"):
                await repository.page_provider_call_evidence(
                    scope,
                    lead_ref,
                    lead_call.provider_call_id,
                    after_seq=0,
                    limit=0,
                )
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_caller_rollback_removes_evidence_head_and_sequence_reservations(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    scope = ContextEvidenceScope.from_resource(seed.owner_a_scope, thread_id)
    subject = ContextSubjectRef.lead_thread(thread_id)
    generation_id = uuid.uuid4()
    try:
        async with seed.factory.begin() as session:
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )

        async with seed.factory() as session:
            transaction = await session.begin()
            repository = ContextEvidenceRepository(session)
            evidence = await repository.append(
                scope,
                ContextEvidenceAppend(
                    subject=subject,
                    context_window_generation=generation_id,
                    event_type="context.window.opened.v1",
                    origin_run_id=None,
                    provider_call_id=None,
                    checkpoint_id=None,
                    idempotency_key=_digest("rolled-back-evidence"),
                    payload={
                        "event_type": "context.window.opened.v1",
                        "model_identity_digest": "a" * 64,
                        "context_window_tokens": 300_000,
                        "compaction_enabled": False,
                    },
                ),
            )
            await repository.upsert_head(
                scope,
                _projection_write(
                    thread_id=thread_id,
                    subject=subject,
                    projection_seq=None,
                    evidence_seq=evidence.evidence_seq,
                    generation_id=generation_id,
                ),
            )
            await transaction.rollback()

        async with seed.factory() as session:
            repository = ContextEvidenceRepository(session)
            assert await repository.page_evidence(scope, after_seq=0, limit=10) == ()
            assert await repository.read_head(scope, subject) is None
            reservation = await repository.reserve(
                scope,
                evidence_count=1,
                projection_count=1,
            )
            assert reservation.first_evidence_seq == 1
            assert reservation.first_projection_seq == 1
            await session.rollback()
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_projection_replay_returns_each_subjects_latest_head_after_the_cursor(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    scope = ContextEvidenceScope.from_resource(seed.owner_a_scope, thread_id)
    lead = ContextSubjectRef.lead_thread(thread_id)
    task = ContextSubjectRef.subagent_task(uuid.uuid4())
    generation_id = uuid.uuid4()
    try:
        async with seed.factory.begin() as session:
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            repository = ContextEvidenceRepository(session)
            for subject in (lead, task, lead):
                await repository.upsert_head(
                    scope,
                    _projection_write(
                        thread_id=thread_id,
                        subject=subject,
                        projection_seq=None,
                        evidence_seq=0,
                        generation_id=generation_id,
                        phase="idle" if subject == lead else "active",
                    ),
                )

        async with seed.factory() as session:
            replay = await ContextEvidenceRepository(session).page_heads_after(
                scope,
                after_projection_seq=0,
                limit=10,
            )

        assert [(item.subject.kind, item.projection_seq) for item in replay] == [
            ("subagent_task", 2),
            ("lead_thread", 3),
        ]
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_exact_retention_purge_removes_only_the_target_threads_context_state(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    scope = ContextEvidenceScope.from_resource(seed.owner_a_scope, thread_id)
    wrong_owner_scope = ContextEvidenceScope.from_resource(
        seed.owner_b_scope,
        thread_id,
    )
    subject = ContextSubjectRef.lead_thread(thread_id)
    generation_id = uuid.uuid4()
    try:
        async with seed.factory.begin() as session:
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            repository = ContextEvidenceRepository(session)
            evidence = await repository.append(
                scope,
                ContextEvidenceAppend(
                    subject=subject,
                    context_window_generation=generation_id,
                    event_type="context.window.opened.v1",
                    origin_run_id=None,
                    provider_call_id=None,
                    checkpoint_id=None,
                    idempotency_key=_digest("retention-opened"),
                    payload={
                        "event_type": "context.window.opened.v1",
                        "model_identity_digest": "a" * 64,
                        "context_window_tokens": 300_000,
                        "compaction_enabled": False,
                    },
                ),
            )
            await repository.upsert_head(
                scope,
                _projection_write(
                    thread_id=thread_id,
                    subject=subject,
                    projection_seq=None,
                    evidence_seq=evidence.evidence_seq,
                    generation_id=generation_id,
                    phase="idle",
                ),
            )

        async with seed.factory.begin() as session:
            repository = ContextEvidenceRepository(session)
            assert await repository.purge_thread(wrong_owner_scope) == (
                ContextRetentionPurgeCounts(
                    evidence=0,
                    projection_heads=0,
                    sequence=0,
                )
            )
            purged = await repository.purge_thread(scope)

        assert purged == ContextRetentionPurgeCounts(
            evidence=1,
            projection_heads=1,
            sequence=1,
        )
        async with seed.factory() as session:
            repository = ContextEvidenceRepository(session)
            assert await repository.page_evidence(scope, after_seq=0, limit=10) == ()
            assert await repository.read_head(scope, subject) is None
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_evidence_idempotency_replays_exactly_and_rejects_changed_facts(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    scope = ContextEvidenceScope.from_resource(seed.owner_a_scope, thread_id)
    subject = ContextSubjectRef.lead_thread(thread_id)
    generation_id = uuid.uuid4()
    first_command = ContextEvidenceAppend(
        subject=subject,
        context_window_generation=generation_id,
        event_type="context.window.opened.v1",
        origin_run_id="already-deleted-run",
        provider_call_id=None,
        checkpoint_id=None,
        idempotency_key=_digest("stable-idempotency"),
        payload={
            "event_type": "context.window.opened.v1",
            "model_identity_digest": "a" * 64,
            "context_window_tokens": 300_000,
            "compaction_enabled": False,
        },
    )
    try:
        async with seed.factory.begin() as session:
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            repository = ContextEvidenceRepository(session)
            first = await repository.append(scope, first_command)
            replay = await repository.append(scope, first_command)
            with pytest.raises(ContextEvidenceIdempotencyConflict):
                await repository.append(
                    scope,
                    ContextEvidenceAppend(
                        subject=subject,
                        context_window_generation=generation_id,
                        event_type="context.window.opened.v1",
                        origin_run_id="already-deleted-run",
                        provider_call_id=None,
                        checkpoint_id=None,
                        idempotency_key=first_command.idempotency_key,
                        payload={
                            "event_type": "context.window.opened.v1",
                            "model_identity_digest": "c" * 64,
                            "context_window_tokens": 300_000,
                            "compaction_enabled": False,
                        },
                    ),
                )
            with pytest.raises(ContextPayloadUnsafe):
                await repository.append(
                    scope,
                    ContextEvidenceAppend(
                        subject=subject,
                        context_window_generation=generation_id,
                        event_type="context.window.opened.v1",
                        origin_run_id=None,
                        provider_call_id=None,
                        checkpoint_id=None,
                        idempotency_key=_digest("unsafe-prompt"),
                        payload={
                            "event_type": "context.window.opened.v1",
                            "model_identity_digest": "a" * 64,
                            "context_window_tokens": 300_000,
                            "compaction_enabled": False,
                            "prompt": "private text",
                        },
                    ),
                )
            second = await repository.append(
                scope,
                ContextEvidenceAppend(
                    subject=subject,
                    context_window_generation=generation_id,
                    event_type="context.window.opened.v1",
                    origin_run_id=None,
                    provider_call_id=None,
                    checkpoint_id=None,
                    idempotency_key=_digest("second-evidence"),
                    payload={
                        "event_type": "context.window.opened.v1",
                        "model_identity_digest": "a" * 64,
                        "context_window_tokens": 300_000,
                        "compaction_enabled": False,
                    },
                ),
            )

        assert first == replay
        assert (first.evidence_seq, second.evidence_seq) == (1, 2)
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_projection_heads_replace_monotonically_and_can_be_rebuilt(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    scope = ContextEvidenceScope.from_resource(seed.owner_a_scope, thread_id)
    subject = ContextSubjectRef.lead_thread(thread_id)
    generation_id = uuid.uuid4()
    try:
        async with seed.factory.begin() as session:
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            repository = ContextEvidenceRepository(session)
            evidence = await repository.append(
                scope,
                ContextEvidenceAppend(
                    subject=subject,
                    context_window_generation=generation_id,
                    event_type="context.window.opened.v1",
                    origin_run_id=None,
                    provider_call_id=None,
                    checkpoint_id=None,
                    idempotency_key=_digest("append-only-evidence"),
                    payload={
                        "event_type": "context.window.opened.v1",
                        "model_identity_digest": "a" * 64,
                        "context_window_tokens": 300_000,
                        "compaction_enabled": False,
                    },
                ),
            )
            first_write = _projection_write(
                thread_id=thread_id,
                subject=subject,
                projection_seq=None,
                evidence_seq=evidence.evidence_seq,
                projector_revision="context-projector-v2",
                generation_id=generation_id,
                phase="idle",
            )
            first = await repository.upsert_head(scope, first_write)
            replay = await repository.upsert_head(
                scope,
                _projection_write(
                    thread_id=thread_id,
                    subject=subject,
                    projection_seq=first.projection_seq,
                    evidence_seq=evidence.evidence_seq,
                    projector_revision="context-projector-v2",
                    generation_id=generation_id,
                    phase="idle",
                ),
            )
            with pytest.raises(ContextProjectionConflict):
                await repository.upsert_head(
                    scope,
                    _projection_write(
                        thread_id=thread_id,
                        subject=subject,
                        projection_seq=None,
                        evidence_seq=evidence.evidence_seq,
                        projector_revision="context-projector-v1",
                        generation_id=generation_id,
                        phase="idle",
                    ),
                )

        assert replay == first
        async with seed.factory.begin() as session:
            repository = ContextEvidenceRepository(session)
            assert await repository.delete_head(scope, subject) is True
            assert await repository.read_head(scope, subject) is None
            rebuilt = await repository.upsert_head(
                scope,
                _projection_write(
                    thread_id=thread_id,
                    subject=subject,
                    projection_seq=None,
                    evidence_seq=evidence.evidence_seq,
                    projector_revision="context-projector-v2",
                    generation_id=generation_id,
                    phase="idle",
                ),
            )
            assert rebuilt.projection_seq > first.projection_seq
        async with seed.factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(sa.update(ContextEvidenceRow).where(ContextEvidenceRow.thread_id == thread_id).values(checkpoint_id="rewritten"))
            await session.rollback()
        async with seed.factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(sa.delete(ContextEvidenceRow).where(ContextEvidenceRow.thread_id == thread_id))
            await session.rollback()
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_parallel_subject_writes_share_ordering_without_combining_heads(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    scope = ContextEvidenceScope.from_resource(seed.owner_a_scope, thread_id)
    generation_id = uuid.uuid4()
    subjects = (
        ContextSubjectRef.subagent_task(uuid.uuid4()),
        ContextSubjectRef.subagent_task(uuid.uuid4()),
    )
    try:
        async with seed.factory.begin() as session:
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )

        async def write_subject(
            subject: ContextSubjectRef,
            marker: str,
        ) -> tuple[int, int]:
            async with seed.factory.begin() as session:
                repository = ContextEvidenceRepository(session)
                reserved = await repository.reserve(
                    scope,
                    evidence_count=1,
                    projection_count=1,
                )
                evidence = await repository.append(
                    scope,
                    ContextEvidenceAppend(
                        subject=subject,
                        context_window_generation=generation_id,
                        event_type="context.window.opened.v1",
                        origin_run_id=None,
                        provider_call_id=None,
                        checkpoint_id=None,
                        idempotency_key=_digest(f"parallel-{marker}"),
                        payload={
                            "event_type": "context.window.opened.v1",
                            "model_identity_digest": marker * 64,
                            "context_window_tokens": 300_000,
                            "compaction_enabled": False,
                        },
                    ),
                    evidence_seq=reserved.first_evidence_seq,
                )
                head = await repository.upsert_head(
                    scope,
                    _projection_write(
                        thread_id=thread_id,
                        subject=subject,
                        projection_seq=reserved.first_projection_seq,
                        evidence_seq=evidence.evidence_seq,
                        generation_id=generation_id,
                        phase="active",
                    ),
                )
                return evidence.evidence_seq, head.projection_seq

        ordered = sorted(
            await asyncio.gather(
                write_subject(subjects[0], "a"),
                write_subject(subjects[1], "b"),
            )
        )

        assert ordered == [(1, 1), (2, 2)]
        async with seed.factory() as session:
            repository = ContextEvidenceRepository(session)
            heads = await repository.page_heads_after(
                scope,
                after_projection_seq=0,
                limit=10,
            )
            evidence = await repository.page_evidence(
                scope,
                after_seq=0,
                limit=10,
            )
        assert [item.projection_seq for item in heads] == [1, 2]
        assert [item.evidence_seq for item in evidence] == [1, 2]
        assert {item.subject.subject_id for item in heads} == {subject.subject_id for subject in subjects}
    finally:
        await seed.engine.dispose()
