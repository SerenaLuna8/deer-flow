"""Physical private-work purge owns exact Context Evidence destruction."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import func, select
from support.private_thread_seed import seed_private_thread_database

from app.private_work.retention_purge import purge_private_scope
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.persistence.context_evidence import (
    ContextEvidenceAppend,
    ContextEvidenceRepository,
    ContextEvidenceScope,
    ContextSubjectRef,
)
from deerflow.persistence.context_evidence.model import (
    ContextEvidenceRow,
    ContextEvidenceSequenceRow,
    ContextProjectionHeadRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow

pytestmark = pytest.mark.postgres


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


async def _seed_context_state(
    session,
    *,
    scope: ContextEvidenceScope,
    origin_run_id: str | None = None,
) -> None:
    repository = ContextEvidenceRepository(session)
    evidence = await repository.append(
        scope,
        ContextEvidenceAppend(
            subject=ContextSubjectRef.lead_thread(scope.thread_id),
            context_window_generation=uuid.uuid4(),
            event_type="context.window.opened.v1",
            origin_run_id=origin_run_id,
            provider_call_id=None,
            checkpoint_id=None,
            idempotency_key=_digest(f"opened:{scope.owner_user_id}:{scope.thread_id}"),
            payload={
                "event_type": "context.window.opened.v1",
                "model_identity_digest": "a" * 64,
                "context_window_tokens": 300_000,
                "compaction_enabled": False,
            },
        ),
    )
    reservation = await repository.reserve(scope, projection_count=1)
    assert reservation.first_projection_seq is not None
    session.add(
        ContextProjectionHeadRow(
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            thread_id=scope.thread_id,
            subject_kind="lead_thread",
            subject_id=scope.thread_id,
            projection_seq=reservation.first_projection_seq,
            evidence_seq=evidence.evidence_seq,
            projector_revision="retention-test-v1",
            projection_schema_version=2,
            context_window_generation=evidence.context_window_generation,
            checkpoint_id=None,
            active_run_id=None,
            phase="idle",
            basis="estimated",
            coverage="complete",
            freshness="current",
            payload_digest=_digest(f"head:{scope.owner_user_id}:{scope.thread_id}"),
            projection_json={},
        )
    )
    await session.flush()


async def _context_counts(session, scope: ContextEvidenceScope) -> tuple[int, int, int]:
    predicates = (
        ContextEvidenceRow.project_id == scope.project_id,
        ContextEvidenceRow.owner_user_id == scope.owner_user_id,
        ContextEvidenceRow.thread_id == scope.thread_id,
    )
    evidence = await session.scalar(select(func.count()).select_from(ContextEvidenceRow).where(*predicates))
    heads = await session.scalar(
        select(func.count())
        .select_from(ContextProjectionHeadRow)
        .where(
            ContextProjectionHeadRow.project_id == scope.project_id,
            ContextProjectionHeadRow.owner_user_id == scope.owner_user_id,
            ContextProjectionHeadRow.thread_id == scope.thread_id,
        )
    )
    sequence = await session.scalar(
        select(func.count())
        .select_from(ContextEvidenceSequenceRow)
        .where(
            ContextEvidenceSequenceRow.project_id == scope.project_id,
            ContextEvidenceSequenceRow.owner_user_id == scope.owner_user_id,
            ContextEvidenceSequenceRow.thread_id == scope.thread_id,
        )
    )
    return int(evidence or 0), int(heads or 0), int(sequence or 0)


@pytest.mark.asyncio
async def test_compensated_thread_create_purges_context_before_thread_metadata(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    scope = ContextEvidenceScope.from_resource(seed.owner_a_scope, thread_id)
    try:
        async with seed.factory.begin() as session:
            repository = PrivateThreadRepository(session)
            created = await repository.create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await _seed_context_state(session, scope=scope)
            tombstone = await repository.mark_deleted(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                expected_version=created.version,
            )
            assert tombstone.deleted_at is not None
            await repository.request_checkpoint_delete_for_compensation(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                expected_created_at=created.created_at,
                expected_deleted_at=tombstone.deleted_at,
            )
            assert await repository.set_checkpoint_delete_status(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                status="complete",
            )
            await repository.purge_compensated_create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                expected_created_at=created.created_at,
                expected_deleted_at=tombstone.deleted_at,
            )

        async with seed.factory() as session:
            assert await session.get(ThreadMetaRow, thread_id) is None
            assert await _context_counts(session, scope) == (0, 0, 0)
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_single_run_delete_preserves_thread_owned_context_state(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    scope = ContextEvidenceScope.from_resource(seed.owner_a_scope, thread_id)
    try:
        async with seed.factory.begin() as session:
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await PrivateRunRepository(session).create_terminal_empty_shell(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                request=PrivateRunCreate(
                    run_id=run_id,
                    status="success",
                ),
            )
            await _seed_context_state(
                session,
                scope=scope,
                origin_run_id=run_id,
            )

        await PrivateRunService(seed.factory).delete(
            seed.owner_a,
            thread_id,
            run_id,
        )

        async with seed.factory() as session:
            deleted_run = await session.get(RunRow, run_id)
            assert deleted_run is not None
            assert deleted_run.status == "deleted"
            assert await _context_counts(session, scope) == (1, 1, 1)
            evidence = await ContextEvidenceRepository(session).page_evidence(
                scope,
                after_seq=0,
                limit=10,
            )
            assert len(evidence) == 1
            assert evidence[0].origin_run_id == run_id
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_ordinary_thread_tombstone_retains_context_until_exact_purge(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    scope = ContextEvidenceScope.from_resource(seed.owner_a_scope, thread_id)
    try:
        async with seed.factory.begin() as session:
            repository = PrivateThreadRepository(session)
            created = await repository.create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await _seed_context_state(session, scope=scope)
            tombstone = await repository.mark_deleted(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                expected_version=created.version,
            )
            assert tombstone.deleted_at is not None

        async with seed.factory() as session:
            tombstone = await PrivateThreadRepository(session).get_deleted(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
            )
            assert tombstone is not None
            assert tombstone.deleted_at is not None
            assert await _context_counts(session, scope) == (1, 1, 1)

        async with seed.factory.begin() as session:
            purged = await ContextEvidenceRepository(session).purge_thread(
                scope,
            )
            assert (
                purged.evidence,
                purged.projection_heads,
                purged.sequence,
            ) == (1, 1, 1)

        async with seed.factory() as session:
            assert await _context_counts(session, scope) == (0, 0, 0)
            assert (
                await PrivateThreadRepository(session).get_deleted(
                    scope=seed.owner_a_scope,
                    thread_id=thread_id,
                )
                is not None
            )
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_exact_context_purge_preserves_same_owner_neighbor_thread(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    target_thread_id = str(uuid.uuid4())
    neighbor_thread_id = str(uuid.uuid4())
    target_scope = ContextEvidenceScope.from_resource(
        seed.owner_a_scope,
        target_thread_id,
    )
    neighbor_scope = ContextEvidenceScope.from_resource(
        seed.owner_a_scope,
        neighbor_thread_id,
    )
    try:
        async with seed.factory.begin() as session:
            repository = PrivateThreadRepository(session)
            for thread_id, scope in (
                (target_thread_id, target_scope),
                (neighbor_thread_id, neighbor_scope),
            ):
                await repository.create(
                    scope=seed.owner_a_scope,
                    thread_id=thread_id,
                    agent=ThreadAgentRef(seed.project_agent_id, "project"),
                )
                await _seed_context_state(session, scope=scope)

        async with seed.factory.begin() as session:
            purged = await ContextEvidenceRepository(session).purge_thread(
                target_scope,
            )
            assert (
                purged.evidence,
                purged.projection_heads,
                purged.sequence,
            ) == (1, 1, 1)

        async with seed.factory() as session:
            assert await _context_counts(session, target_scope) == (0, 0, 0)
            assert await _context_counts(session, neighbor_scope) == (1, 1, 1)
            assert await session.get(ThreadMetaRow, target_thread_id) is not None
            assert await session.get(ThreadMetaRow, neighbor_thread_id) is not None
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_owner_retention_purges_only_exact_scoped_context_state(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    target_thread_id = str(uuid.uuid4())
    neighbor_thread_id = str(uuid.uuid4())
    target_scope = ContextEvidenceScope.from_resource(
        seed.owner_a_scope,
        target_thread_id,
    )
    neighbor_scope = ContextEvidenceScope.from_resource(
        seed.owner_b_scope,
        neighbor_thread_id,
    )
    try:
        async with seed.factory.begin() as session:
            repository = PrivateThreadRepository(session)
            await repository.create(
                scope=seed.owner_a_scope,
                thread_id=target_thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await repository.create(
                scope=seed.owner_b_scope,
                thread_id=neighbor_thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await _seed_context_state(session, scope=target_scope)
            await _seed_context_state(session, scope=neighbor_scope)

        async with seed.factory.begin() as session:
            await purge_private_scope(
                session,
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
            )

        async with seed.factory() as session:
            assert await _context_counts(session, target_scope) == (0, 0, 0)
            assert await _context_counts(session, neighbor_scope) == (1, 1, 1)
            assert await session.get(ThreadMetaRow, neighbor_thread_id) is not None
    finally:
        await seed.engine.dispose()
