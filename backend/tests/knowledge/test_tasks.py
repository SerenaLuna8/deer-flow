"""M4 gates: task queue operations, delete handlers, purge, and user retry.

Everything runs against the installed Schema V1 snapshot. Object storage is a
fake in-memory store so deletion ordering (objects before rows) and failure
propagation can be asserted precisely.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from actweave_knowledge import (
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeError,
    KnowledgeSettings,
)
from actweave_knowledge.bases import KnowledgeBaseService
from actweave_knowledge.documents import KnowledgeDocumentService
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from actweave_knowledge.persistence.tasks import (
    claim_next_task,
    defer_running_task_for_inactive_project,
    extend_task_lease,
    recover_expired_tasks,
    settle_task_failure,
    settle_task_success,
    update_task_progress,
)
from actweave_knowledge.tasks import (
    KnowledgeBaseDeletionHandler,
    KnowledgeDocumentDeletionHandler,
    KnowledgeDocumentObjectDeletionHandler,
    KnowledgeTaskClaim,
    purge_project_knowledge,
)
from extraction_test_helpers import make_test_file_capability_provider, make_test_quota_port
from registry_helpers import registry_model_port, seed_registry_models
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.knowledge.composition import (
    is_knowledge_project_active,
    is_knowledge_project_pending_deletion,
)

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail_delete_keys: set[str] = set()
        self.fail_versioning_check = False
        self.versioning_checks = 0

    async def require_unversioned_bucket(self) -> None:
        self.versioning_checks += 1
        if self.fail_versioning_check:
            raise KnowledgeError(
                KNOWLEDGE_STORAGE_UNAVAILABLE,
                "Knowledge bucket 必须关闭版本控制",
            )

    async def delete_many(self, keys: list[str]) -> None:
        await self.require_unversioned_bucket()
        for key in keys:
            await self.delete(key)

    async def delete(self, key: str) -> None:
        if key in self.fail_delete_keys:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储删除失败，请稍后重试")
        self.objects.pop(key, None)
        self.deleted.append(key)

    async def require_absent(self, key: str) -> None:
        if key in self.objects:
            raise KnowledgeError(
                KNOWLEDGE_STORAGE_UNAVAILABLE,
                "对象存储删除结果无法确认",
            )

    async def download_to(self, key: str, target_path: Path) -> None:  # pragma: no cover - unused
        raise AssertionError("not used in these tests")

    async def delete_project_objects(self, project_id: uuid.UUID) -> None:
        prefix = f"projects/{project_id}/knowledge/"
        for key in list(self.objects):
            if key.startswith(prefix):
                await self.delete(key)


class _Harness:
    def __init__(self, engine, factory, store: _FakeStore, quota) -> None:  # noqa: ANN001
        self.engine = engine
        self.factory = factory
        self.store = store
        self.quota = quota


async def _harness(postgres_database_url: str) -> _Harness:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return _Harness(engine, factory, _FakeStore(), make_test_quota_port(factory))


async def _mark_project_pending_deletion(
    harness: _Harness,
    project_id: uuid.UUID,
) -> None:
    async with harness.factory() as session, session.begin():
        await session.execute(
            text("UPDATE projects SET status = 'pending_deletion', deletion_requested_at = now(), deletion_effective_at = now() WHERE id = :project_id"),
            {"project_id": project_id},
        )


async def _seed_project(session: AsyncSession, label: str) -> uuid.UUID:
    user_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    await session.execute(
        text(
            """INSERT INTO users (
                   id, email, username, system_role, created_at,
                   needs_setup, token_version
               ) VALUES (:user_id, :email, :username, 'user', now(), false, 1)"""
        ),
        {"user_id": user_id, "email": f"{label}@example.invalid", "username": f"m4t_{label}"},
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:project_id, :slug, :display_name, :user_id)"""
        ),
        {"project_id": project_id, "slug": f"m4t-{label}", "display_name": label, "user_id": user_id},
    )
    return project_id


async def _seed_base(
    harness: _Harness,
    project_id: uuid.UUID,
    *,
    status: str = "active",
) -> uuid.UUID:
    embedding_model_id, _ = await seed_registry_models(harness.factory, dimension=8)
    async with harness.factory() as session, session.begin():
        base_id = uuid.uuid4()
        session.add(
            KnowledgeBaseRow(
                id=base_id,
                project_id=project_id,
                name=f"base-{base_id.hex[:8]}",
                embedding_model_id=embedding_model_id,
                status=status,
            )
        )
    return base_id


async def _seed_document(
    harness: _Harness,
    project_id: uuid.UUID,
    base_id: uuid.UUID,
    *,
    status: str = "ready",
    version: int = 1,
    error_message: str | None = None,
    segments: int = 0,
    with_object: bool = True,
) -> uuid.UUID:
    document_id = uuid.uuid4()
    storage_key = f"projects/{project_id}/knowledge/{base_id}/{document_id}.md"
    async with harness.factory() as session, session.begin():
        session.add(
            KnowledgeDocumentRow(
                id=document_id,
                project_id=project_id,
                knowledge_base_id=base_id,
                name="note.md",
                original_name="note.md",
                storage_key=storage_key,
                size_bytes=32,
                upload_state="stored" if with_object else "pending",
                status=status,
                version=version,
                error_message=error_message,
                segment_count=segments,
            )
        )
        for position in range(1, segments + 1):
            session.add(
                KnowledgeSegmentRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    knowledge_base_id=base_id,
                    knowledge_document_id=document_id,
                    document_version=version,
                    position=position,
                    content=f"分段 {position}",
                    source_position={"page": position},
                    embedding=[0.25] * 8,
                )
            )
    if with_object:
        harness.store.objects[storage_key] = b"content"
    return document_id


async def _seed_task(
    harness: _Harness,
    project_id: uuid.UUID,
    resource_id: uuid.UUID,
    *,
    kind: str = "ingest_document",
    target_version: int | None = 1,
    status: str = "queued",
    attempt_count: int = 0,
    claim_token: uuid.UUID | None = None,
    lease_until: datetime | None = None,
    error_message: str | None = None,
    storage_key: str | None = None,
) -> uuid.UUID:
    task_id = uuid.uuid4()
    async with harness.factory() as session, session.begin():
        session.add(
            KnowledgeTaskRow(
                id=task_id,
                project_id=project_id,
                resource_id=resource_id,
                kind=kind,
                target_version=target_version,
                status=status,
                attempt_count=attempt_count,
                claim_token=claim_token,
                lease_until=lease_until,
                error_message=error_message,
                storage_key=storage_key,
            )
        )
    return task_id


async def _task_row(harness: _Harness, task_id: uuid.UUID) -> KnowledgeTaskRow:
    async with harness.factory() as session:
        row = await session.get(KnowledgeTaskRow, task_id)
        assert row is not None
        return row


async def _claim_snapshot(harness: _Harness, *, lease_seconds: int = 60) -> KnowledgeTaskClaim:
    async with harness.factory() as session, session.begin():
        row = await claim_next_task(session, lease_seconds=lease_seconds)
        assert row is not None, "expected a claimable task"
        return KnowledgeTaskClaim(
            id=row.id,
            project_id=row.project_id,
            resource_id=row.resource_id,
            kind=row.kind,
            target_version=row.target_version,
            claim_token=row.claim_token,  # type: ignore[arg-type]
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
            storage_key=row.storage_key,
            reparse_settings=row.reparse_settings,
        )


def _documents_service(harness: _Harness, **settings_overrides: object) -> KnowledgeDocumentService:
    settings = KnowledgeSettings.model_validate({"enabled": False, **settings_overrides})
    return KnowledgeDocumentService(
        project_active_check=is_knowledge_project_active,
        quota=make_test_quota_port(harness.factory),
        session_factory=harness.factory,
        settings=settings,
        file_capabilities=make_test_file_capability_provider(settings),
        object_store=harness.store,  # type: ignore[arg-type]
    )


def _bases_service(harness: _Harness) -> KnowledgeBaseService:
    settings = KnowledgeSettings.model_validate({"enabled": False})
    return KnowledgeBaseService(
        session_factory=harness.factory,
        settings=settings,
        model_port=registry_model_port(),
    )


# ---------------------------------------------------------------------------
# Lease and settlement operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extend_task_lease_requires_the_claim_token(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        task_id = await _seed_task(harness, project_id, uuid.uuid4())
        claim = await _claim_snapshot(harness, lease_seconds=60)
        before = (await _task_row(harness, task_id)).lease_until
        assert before is not None

        async with harness.factory() as session, session.begin():
            assert await extend_task_lease(session, claim.id, claim.claim_token, lease_seconds=600) is True
        after = (await _task_row(harness, task_id)).lease_until
        assert after is not None and after > before

        async with harness.factory() as session, session.begin():
            assert await extend_task_lease(session, claim.id, uuid.uuid4(), lease_seconds=600) is False
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["heartbeat", "success", "failure", "progress", "defer"])
async def test_expired_claim_cannot_be_revived_or_settled(postgres_database_url: str, operation: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        task_id = await _seed_task(harness, project_id, uuid.uuid4())
        claim = await _claim_snapshot(harness)
        expired = datetime.now(UTC) - timedelta(seconds=1)
        async with harness.factory() as session, session.begin():
            cached = await session.get(KnowledgeTaskRow, task_id)
            assert cached is not None and cached.lease_until > datetime.now(UTC)
            async with harness.factory() as other, other.begin():
                await other.execute(text("UPDATE knowledge_tasks SET lease_until = :expired WHERE id = :id"), {"expired": expired, "id": task_id})
            if operation == "heartbeat":
                result = await extend_task_lease(session, claim.id, claim.claim_token, lease_seconds=60)
            elif operation == "success":
                result = await settle_task_success(session, claim.id, claim.claim_token)
            elif operation == "failure":
                result = await settle_task_failure(session, claim.id, claim.claim_token, error_message="late failure", retry_delay_seconds=30)
            elif operation == "defer":
                result = await defer_running_task_for_inactive_project(session, claim.id, claim.claim_token)
            else:
                result = await update_task_progress(
                    session,
                    task_id=claim.id,
                    claim_token=claim.claim_token,
                    attempt_count=claim.attempt_count,
                    target_version=claim.target_version,
                    stage="embedding",
                    completed_units=1,
                    total_units=3,
                )
        assert result is (None if operation == "failure" else False)
        row = await _task_row(harness, task_id)
        assert row.status == "running"
        assert row.claim_token == claim.claim_token and row.lease_until == expired
        assert row.completed_units == 0
        async with harness.factory() as session, session.begin():
            assert await recover_expired_tasks(session) == 1
        recovered = await _task_row(harness, task_id)
        assert recovered.status == "retry_wait" and recovered.claim_token is None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_task_claim_mutations_preserve_explicit_repository_clock(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        moment = datetime(2020, 1, 1, tzinfo=UTC)
        claim_token = uuid.uuid4()
        task_id = await _seed_task(
            harness,
            project_id,
            uuid.uuid4(),
            status="running",
            attempt_count=1,
            claim_token=claim_token,
            lease_until=moment + timedelta(seconds=1),
        )
        async with harness.factory() as session, session.begin():
            assert await extend_task_lease(session, task_id, claim_token, lease_seconds=60, now=moment) is True
        row = await _task_row(harness, task_id)
        assert row.lease_until == moment + timedelta(seconds=60)
        async with harness.factory() as session, session.begin():
            assert await settle_task_success(session, task_id, claim_token, now=moment + timedelta(seconds=61)) is False
        async with harness.factory() as session, session.begin():
            assert await settle_task_success(session, task_id, claim_token, now=moment + timedelta(seconds=59)) is True
        settled = await _task_row(harness, task_id)
        assert settled.finished_at == moment + timedelta(seconds=59)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["heartbeat", "success", "failure", "progress", "defer"])
async def test_claim_expiring_while_waiting_for_task_lock_cannot_be_mutated(postgres_database_url: str, operation: str) -> None:
    harness = await _harness(postgres_database_url)
    blocker = harness.factory()
    running: asyncio.Task | None = None
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        task_id = await _seed_task(harness, project_id, uuid.uuid4())
        claim = await _claim_snapshot(harness, lease_seconds=2)
        before = await _task_row(harness, task_id)
        await blocker.begin()
        # Deliberately do not update the row: PostgreSQL does not re-evaluate
        # a pre-lock WHERE predicate after a pure row-lock wait.
        await blocker.scalar(select(KnowledgeTaskRow.id).where(KnowledgeTaskRow.id == task_id).with_for_update())
        connection_pid: list[int] = []

        async def mutate_waiting_claim():  # noqa: ANN202
            async with harness.factory() as session, session.begin():
                connection_pid.append(await session.scalar(select(func.pg_backend_pid())))
                if operation == "heartbeat":
                    return await extend_task_lease(session, claim.id, claim.claim_token, lease_seconds=60)
                if operation == "success":
                    return await settle_task_success(session, claim.id, claim.claim_token)
                if operation == "failure":
                    return await settle_task_failure(session, claim.id, claim.claim_token, error_message="late failure", retry_delay_seconds=30)
                if operation == "defer":
                    return await defer_running_task_for_inactive_project(session, claim.id, claim.claim_token)
                return await update_task_progress(
                    session,
                    task_id=claim.id,
                    claim_token=claim.claim_token,
                    attempt_count=claim.attempt_count,
                    target_version=claim.target_version,
                    stage="embedding",
                    completed_units=1,
                    total_units=3,
                )

        running = asyncio.create_task(mutate_waiting_claim())
        async with asyncio.timeout(5):
            while True:
                if connection_pid:
                    async with harness.factory() as session:
                        waiting = await session.scalar(text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"), {"pid": connection_pid[0]})
                    if waiting:
                        break
                await asyncio.sleep(0.01)
            await asyncio.sleep(2.1)
        await blocker.rollback()
        assert await running is (None if operation == "failure" else False)
        after = await _task_row(harness, task_id)
        assert after.status == "running"
        assert after.claim_token == claim.claim_token and after.lease_until == before.lease_until
        assert after.attempt_count == 1 and after.completed_units == 0
    finally:
        await blocker.close()
        if running is not None and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_settle_task_success_is_token_guarded(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        task_id = await _seed_task(harness, project_id, uuid.uuid4())
        claim = await _claim_snapshot(harness)

        async with harness.factory() as session, session.begin():
            assert await settle_task_success(session, claim.id, uuid.uuid4()) is False
        assert (await _task_row(harness, task_id)).status == "running"

        async with harness.factory() as session, session.begin():
            assert await settle_task_success(session, claim.id, claim.claim_token) is True
        settled = await _task_row(harness, task_id)
        assert settled.status == "succeeded"
        assert settled.claim_token is None and settled.lease_until is None
        assert settled.finished_at is not None

        # Settling again is a no-op: the claim no longer exists.
        async with harness.factory() as session, session.begin():
            assert await settle_task_success(session, claim.id, claim.claim_token) is False
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_settle_task_failure_retries_then_fails_and_marks_the_document(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id)
        document_id = await _seed_document(harness, project_id, base_id, status="processing")
        task_id = await _seed_task(harness, project_id, document_id)

        # Attempts one and two end in retry_wait with a delayed available_at.
        for expected_attempt in (1, 2):
            claim = await _claim_snapshot(harness)
            assert claim.attempt_count == expected_attempt
            async with harness.factory() as session, session.begin():
                outcome = await settle_task_failure(
                    session,
                    claim.id,
                    claim.claim_token,
                    error_message="模型调用失败",
                    retry_delay_seconds=0,
                )
            assert outcome == "retry_wait"
            row = await _task_row(harness, task_id)
            assert row.status == "retry_wait"
            assert row.claim_token is None and row.lease_until is None
            assert row.error_message == "模型调用失败"
            async with harness.factory() as session:
                document = await session.get(KnowledgeDocumentRow, document_id)
                assert document is not None and document.status == "processing"

        # The third failure exhausts the budget and fails the document too.
        claim = await _claim_snapshot(harness)
        assert claim.attempt_count == 3
        async with harness.factory() as session, session.begin():
            outcome = await settle_task_failure(
                session,
                claim.id,
                claim.claim_token,
                error_message="模型持续不可用",
                retry_delay_seconds=0,
            )
        assert outcome == "failed"
        row = await _task_row(harness, task_id)
        assert row.status == "failed"
        assert row.finished_at is not None
        async with harness.factory() as session:
            document = await session.get(KnowledgeDocumentRow, document_id)
            assert document is not None
            assert document.status == "failed"
            assert document.error_message == "模型持续不可用"

        # No further claims: the task budget is spent.
        async with harness.factory() as session, session.begin():
            assert await claim_next_task(session, lease_seconds=60) is None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_recover_expired_tasks_requeues_or_fails_by_remaining_budget(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id)
        expired = datetime.now(UTC) - timedelta(seconds=5)

        retryable_document = await _seed_document(harness, project_id, base_id, status="processing")
        retryable_task = await _seed_task(
            harness,
            project_id,
            retryable_document,
            status="running",
            attempt_count=1,
            claim_token=uuid.uuid4(),
            lease_until=expired,
        )
        exhausted_document = await _seed_document(harness, project_id, base_id, status="processing")
        exhausted_task = await _seed_task(
            harness,
            project_id,
            exhausted_document,
            status="running",
            attempt_count=3,
            claim_token=uuid.uuid4(),
            lease_until=expired,
        )

        async with harness.factory() as session, session.begin():
            recovered = await recover_expired_tasks(session)
        assert recovered == 2

        retryable = await _task_row(harness, retryable_task)
        assert retryable.status == "retry_wait"
        assert retryable.claim_token is None and retryable.lease_until is None

        failed = await _task_row(harness, exhausted_task)
        assert failed.status == "failed"
        assert failed.error_message is not None
        assert failed.finished_at is not None
        async with harness.factory() as session:
            document = await session.get(KnowledgeDocumentRow, exhausted_document)
            assert document is not None
            assert document.status == "failed"
            assert document.error_message == failed.error_message

        # The retryable task is immediately claimable again.
        claim = await _claim_snapshot(harness)
        assert claim.id == retryable_task
        assert claim.attempt_count == 2
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Verified progress of the current attempt (M10 T4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_resets_progress_and_settlement_stamps_done(postgres_database_url: str) -> None:
    """A new attempt starts from zero — stale stage/counters of the previous
    attempt never accumulate — and only settlement stamps the ``done`` stage."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        task_id = await _seed_task(harness, project_id, uuid.uuid4(), status="retry_wait", attempt_count=1)
        async with harness.factory() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_tasks SET stage = 'embedding', completed_units = 5, total_units = 9 WHERE id = :id"),
                {"id": str(task_id)},
            )

        claim = await _claim_snapshot(harness)
        claimed = await _task_row(harness, task_id)
        assert claimed.stage == "queued"
        assert claimed.completed_units == 0
        assert claimed.total_units is None
        assert claimed.progress_updated_at is not None

        async with harness.factory() as session, session.begin():
            assert (
                await update_task_progress(
                    session,
                    task_id=claim.id,
                    claim_token=claim.claim_token,
                    attempt_count=claim.attempt_count,
                    target_version=claim.target_version,
                    stage="embedding",
                    completed_units=3,
                    total_units=9,
                )
                is True
            )
        progressed = await _task_row(harness, task_id)
        assert progressed.stage == "embedding"
        assert progressed.completed_units == 3
        assert progressed.total_units == 9

        async with harness.factory() as session, session.begin():
            assert await settle_task_success(session, claim.id, claim.claim_token) is True
        settled = await _task_row(harness, task_id)
        assert settled.status == "succeeded"
        assert settled.stage == "done"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_task_progress_rejects_stale_claims_and_late_attempts(postgres_database_url: str) -> None:
    """Progress is verified against claim token, attempt, and target version;
    a late update from a lost attempt matches no row and reports False."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        task_id = await _seed_task(harness, project_id, uuid.uuid4())
        first = await _claim_snapshot(harness)

        async def _attempt(claim: KnowledgeTaskClaim, **overrides: object) -> bool:
            values = {
                "task_id": claim.id,
                "claim_token": claim.claim_token,
                "attempt_count": claim.attempt_count,
                "target_version": claim.target_version,
            }
            values.update(overrides)
            async with harness.factory() as session, session.begin():
                return await update_task_progress(
                    session,
                    stage="embedding",
                    completed_units=1,
                    total_units=4,
                    **values,  # type: ignore[arg-type]
                )

        assert await _attempt(first, claim_token=uuid.uuid4()) is False
        assert await _attempt(first, attempt_count=first.attempt_count + 1) is False
        assert await _attempt(first, target_version=99) is False
        assert await _attempt(first) is True

        # The lease expires and another worker re-claims: attempt two.
        async with harness.factory() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_tasks SET lease_until = now() - interval '1 second' WHERE id = :id"),
                {"id": str(task_id)},
            )
        second = await _claim_snapshot(harness)
        assert second.id == task_id and second.attempt_count == 2
        fresh = await _task_row(harness, task_id)
        assert fresh.completed_units == 0 and fresh.stage == "queued"

        # The first attempt's progress arrives late: no row may match.
        assert await _attempt(first) is False
        untouched = await _task_row(harness, task_id)
        assert untouched.completed_units == 0 and untouched.stage == "queued"

        # The current attempt still reports normally.
        assert await _attempt(second) is True
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Delete handlers
# ---------------------------------------------------------------------------


def _handler_claim(
    task_id: uuid.UUID,
    project_id: uuid.UUID,
    resource_id: uuid.UUID,
    kind: str,
    *,
    storage_key: str | None = None,
) -> KnowledgeTaskClaim:
    return KnowledgeTaskClaim(
        id=task_id,
        project_id=project_id,
        resource_id=resource_id,
        kind=kind,
        target_version=None,
        claim_token=uuid.uuid4(),
        attempt_count=1,
        max_attempts=3,
        storage_key=storage_key,
    )


async def _claimed_deletion(
    harness: _Harness,
    project_id: uuid.UUID,
    resource_id: uuid.UUID,
    kind: str,
    *,
    storage_key: str | None = None,
) -> KnowledgeTaskClaim:
    await _seed_task(
        harness,
        project_id,
        resource_id,
        kind=kind,
        target_version=None,
        storage_key=storage_key,
    )
    claim = await _claim_snapshot(harness, lease_seconds=600)
    assert (claim.project_id, claim.resource_id, claim.kind) == (
        project_id,
        resource_id,
        kind,
    )
    return claim


@pytest.mark.asyncio
async def test_delete_document_handler_removes_object_then_row(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id)
        document_id = await _seed_document(harness, project_id, base_id, status="deleting", segments=2)
        claim = await _claimed_deletion(harness, project_id, document_id, "delete_document")
        handler = KnowledgeDocumentDeletionHandler(
            session_factory=harness.factory,
            object_store=harness.store,  # type: ignore[arg-type]
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        await handler(claim)

        assert harness.store.objects == {}
        async with harness.factory() as session:
            assert await session.get(KnowledgeDocumentRow, document_id) is None
            remaining_segments = await session.scalar(select(func.count()).select_from(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_document_id == document_id))
            assert int(remaining_segments or 0) == 0

        # Idempotent: the document is already gone.
        await handler(claim)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_delete_document_handler_skips_documents_not_marked_deleting(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id)
        document_id = await _seed_document(harness, project_id, base_id, status="ready")
        claim = await _claimed_deletion(harness, project_id, document_id, "delete_document")
        handler = KnowledgeDocumentDeletionHandler(
            session_factory=harness.factory,
            object_store=harness.store,  # type: ignore[arg-type]
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        await handler(claim)

        assert harness.store.deleted == []
        async with harness.factory() as session:
            assert await session.get(KnowledgeDocumentRow, document_id) is not None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_delete_document_object_handler_uses_exact_key_and_removes_tombstone(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id)
        document_id = await _seed_document(
            harness,
            project_id,
            base_id,
            status="deleting",
        )
        orphan_key = await _document_key(harness, document_id)
        same_document_other_key = f"projects/{project_id}/knowledge/{uuid.uuid4()}/{document_id}.pdf"
        harness.store.objects[orphan_key] = b"late put"
        harness.store.objects[same_document_other_key] = b"must remain"
        handler = KnowledgeDocumentObjectDeletionHandler(
            session_factory=harness.factory,
            object_store=harness.store,  # type: ignore[arg-type]
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )
        claim = await _claimed_deletion(
            harness,
            project_id,
            document_id,
            "delete_document_object",
            storage_key=orphan_key,
        )

        await handler(claim)

        assert orphan_key not in harness.store.objects
        assert same_document_other_key in harness.store.objects
        async with harness.factory() as session:
            assert await session.get(KnowledgeDocumentRow, document_id) is None
    finally:
        await harness.engine.dispose()


@pytest.mark.parametrize(
    "forged_key",
    (
        "projects/00000000-0000-4000-8000-000000000001/knowledge/00000000-0000-4000-8000-000000000002/{document_id}.pdf",
        "projects/{project_id}/knowledge/not-a-base/{document_id}.pdf",
        "projects/{project_id}/knowledge/00000000-0000-4000-8000-000000000002/00000000-0000-4000-8000-000000000003.pdf",
    ),
)
@pytest.mark.asyncio
async def test_document_object_delete_rejects_forged_storage_authority(
    postgres_database_url: str,
    forged_key: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        document_id = uuid.uuid4()
        key = forged_key.format(
            project_id=project_id,
            document_id=document_id,
        )
        harness.store.objects[key] = b"must not be deleted"
        handler = KnowledgeDocumentObjectDeletionHandler(
            session_factory=harness.factory,
            object_store=harness.store,  # type: ignore[arg-type]
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        with pytest.raises(KnowledgeError):
            await handler(
                _handler_claim(
                    uuid.uuid4(),
                    project_id,
                    document_id,
                    "delete_document_object",
                    storage_key=key,
                )
            )

        assert harness.store.objects[key] == b"must not be deleted"
        assert harness.store.deleted == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_delete_base_handler_drains_documents_then_deletes_the_base(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id, status="deleting")
        for status in ("ready", "processing", "failed"):
            await _seed_document(
                harness,
                project_id,
                base_id,
                status=status,
                segments=1 if status == "ready" else 0,
                error_message="失败" if status == "failed" else None,
            )
        claim = await _claimed_deletion(harness, project_id, base_id, "delete_knowledge_base")
        handler = KnowledgeBaseDeletionHandler(
            session_factory=harness.factory,
            object_store=harness.store,  # type: ignore[arg-type]
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        await handler(claim)

        assert harness.store.objects == {}
        assert len(harness.store.deleted) == 3
        async with harness.factory() as session:
            assert await session.get(KnowledgeBaseRow, base_id) is None
            remaining = await session.scalar(select(func.count()).select_from(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == base_id))
            assert int(remaining or 0) == 0

        # Idempotent for an already-deleted base.
        await handler(claim)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_delete_base_handler_storage_failure_keeps_the_base_for_retry(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id, status="deleting")
        blocked_document = await _seed_document(harness, project_id, base_id)
        async with harness.factory() as session:
            blocked_key = (await session.get(KnowledgeDocumentRow, blocked_document)).storage_key  # type: ignore[union-attr]
        harness.store.fail_delete_keys.add(blocked_key)
        claim = await _claimed_deletion(harness, project_id, base_id, "delete_knowledge_base")
        handler = KnowledgeBaseDeletionHandler(
            session_factory=harness.factory,
            object_store=harness.store,  # type: ignore[arg-type]
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        with pytest.raises(KnowledgeError) as error:
            await handler(claim)
        assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE

        async with harness.factory() as session:
            base = await session.get(KnowledgeBaseRow, base_id)
            assert base is not None and base.status == "deleting"
            assert await session.get(KnowledgeDocumentRow, blocked_document) is not None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_empty_base_delete_checks_bucket_versioning_before_row_delete(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id, status="deleting")
        harness.store.fail_versioning_check = True
        handler = KnowledgeBaseDeletionHandler(
            session_factory=harness.factory,
            object_store=harness.store,  # type: ignore[arg-type]
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )
        claim = await _claimed_deletion(harness, project_id, base_id, "delete_knowledge_base")

        with pytest.raises(KnowledgeError):
            await handler(claim)

        async with harness.factory() as session:
            assert await session.get(KnowledgeBaseRow, base_id) is not None
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Project purge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_project_knowledge_removes_everything_and_is_idempotent(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            other_project = await _seed_project(session, uuid.uuid4().hex[:8])
        first_base = await _seed_base(harness, project_id)
        second_base = await _seed_base(harness, project_id, status="deleting")
        for base_id in (first_base, second_base):
            document_id = await _seed_document(harness, project_id, base_id, segments=2)
            await _seed_task(harness, project_id, document_id)
        other_base = await _seed_base(harness, other_project)
        other_document = await _seed_document(harness, other_project, other_base, segments=1)
        orphan_key = f"projects/{project_id}/knowledge/{uuid.uuid4()}/{uuid.uuid4()}.pdf"
        harness.store.objects[orphan_key] = b"orphan"
        await _mark_project_pending_deletion(harness, project_id)

        await purge_project_knowledge(
            harness.factory,
            harness.store,  # type: ignore[arg-type]
            quota=harness.quota,
            project_cleanup_check=is_knowledge_project_pending_deletion,
            project_id=project_id,
        )

        async with harness.factory() as session:
            for model in (KnowledgeBaseRow, KnowledgeDocumentRow, KnowledgeTaskRow, KnowledgeSegmentRow):
                remaining = await session.scalar(select(func.count()).select_from(model).where(model.project_id == project_id))
                assert int(remaining or 0) == 0, model.__name__
            assert await session.get(KnowledgeBaseRow, other_base) is not None
            assert await session.get(KnowledgeDocumentRow, other_document) is not None
        assert set(harness.store.objects) == {
            (await _document_key(harness, other_document)),
        }

        # Idempotent: purging again with nothing left succeeds.
        await purge_project_knowledge(
            harness.factory,
            harness.store,  # type: ignore[arg-type]
            quota=harness.quota,
            project_cleanup_check=is_knowledge_project_pending_deletion,
            project_id=project_id,
        )
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_project_purge_defers_recent_upload_without_touching_storage_or_rows(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id)
        document_id = await _seed_document(
            harness,
            project_id,
            base_id,
            status="uploading",
        )
        await _mark_project_pending_deletion(harness, project_id)

        completed = await purge_project_knowledge(
            harness.factory,
            harness.store,  # type: ignore[arg-type]
            quota=harness.quota,
            project_cleanup_check=is_knowledge_project_pending_deletion,
            project_id=project_id,
        )

        assert completed is False
        assert harness.store.deleted == []
        assert harness.store.versioning_checks == 0
        async with harness.factory() as session:
            document = await session.get(KnowledgeDocumentRow, document_id)
            assert document is not None and document.status == "uploading"
            assert await session.get(KnowledgeBaseRow, base_id) is not None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_project_purge_converts_stale_upload_then_cleans_on_next_attempt(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id)
        document_id = await _seed_document(
            harness,
            project_id,
            base_id,
            status="uploading",
        )
        async with harness.factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, document_id)
            assert document is not None
            document.updated_at = datetime.now(UTC) - timedelta(days=2)
        await _mark_project_pending_deletion(harness, project_id)

        first = await purge_project_knowledge(
            harness.factory,
            harness.store,  # type: ignore[arg-type]
            quota=harness.quota,
            project_cleanup_check=is_knowledge_project_pending_deletion,
            project_id=project_id,
        )

        assert first is False
        assert harness.store.deleted == []
        assert harness.store.versioning_checks == 0
        async with harness.factory() as session:
            document = await session.get(KnowledgeDocumentRow, document_id)
            task = await session.scalar(
                select(KnowledgeTaskRow).where(
                    KnowledgeTaskRow.project_id == project_id,
                    KnowledgeTaskRow.resource_id == document_id,
                    KnowledgeTaskRow.kind == "delete_document_object",
                    KnowledgeTaskRow.status == "queued",
                )
            )
            assert document is not None
            assert document.status == "deleting"
            assert document.version == 2
            assert task is not None
            assert task.storage_key == document.storage_key

        second = await purge_project_knowledge(
            harness.factory,
            harness.store,  # type: ignore[arg-type]
            quota=harness.quota,
            project_cleanup_check=is_knowledge_project_pending_deletion,
            project_id=project_id,
        )

        assert second is True
        assert harness.store.objects == {}
        async with harness.factory() as session:
            assert await session.get(KnowledgeDocumentRow, document_id) is None
            assert await session.get(KnowledgeBaseRow, base_id) is None
            tasks = await session.scalar(select(func.count()).select_from(KnowledgeTaskRow).where(KnowledgeTaskRow.project_id == project_id))
            assert int(tasks or 0) == 0
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_purge_project_checks_bucket_versioning_before_deleting_rows(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id)
        await _mark_project_pending_deletion(harness, project_id)
        harness.store.fail_versioning_check = True

        with pytest.raises(KnowledgeError) as error:
            await purge_project_knowledge(
                harness.factory,
                harness.store,  # type: ignore[arg-type]
                quota=harness.quota,
                project_cleanup_check=is_knowledge_project_pending_deletion,
                project_id=project_id,
            )

        assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE
        assert harness.store.versioning_checks == 1
        async with harness.factory() as session:
            assert await session.get(KnowledgeBaseRow, base_id) is not None
    finally:
        await harness.engine.dispose()


async def _document_key(harness: _Harness, document_id: uuid.UUID) -> str:
    async with harness.factory() as session:
        row = await session.get(KnowledgeDocumentRow, document_id)
        assert row is not None
        return row.storage_key


# ---------------------------------------------------------------------------
# User retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_document_bumps_version_and_queues_a_new_ingest_task(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    service = _documents_service(harness)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id)
        document_id = await _seed_document(harness, project_id, base_id, status="failed", error_message="解析失败")

        from actweave_knowledge.extraction.contracts import ProcessingProfile
        from parsing_test_helpers import make_chunk_profile, make_parse_profile

        async with harness.factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, document_id)
            document.parsing_profile = ProcessingProfile(parse=make_parse_profile(Path(document.original_name).suffix), chunk=make_chunk_profile()).model_dump(mode="json")
        view = await service.retry_document(project_id, document_id)

        assert view.status == "queued"
        assert view.version == 2
        assert view.error_message is None
        async with harness.factory() as session:
            task = await session.scalar(
                select(KnowledgeTaskRow).where(
                    KnowledgeTaskRow.resource_id == document_id,
                    KnowledgeTaskRow.kind == "ingest_document",
                    KnowledgeTaskRow.status == "queued",
                )
            )
            assert task is not None
            assert task.target_version == 2
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_retry_document_rejects_wrong_states_and_scopes(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    service = _documents_service(harness)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            other_project = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id)

        ready_document = await _seed_document(harness, project_id, base_id, status="ready")
        with pytest.raises(KnowledgeError) as error:
            await service.retry_document(project_id, ready_document)
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST

        failed_document = await _seed_document(harness, project_id, base_id, status="failed", error_message="失败")
        with pytest.raises(KnowledgeError) as error:
            await service.retry_document(other_project, failed_document)
        assert error.value.code == KNOWLEDGE_NOT_FOUND

        disabled_base = await _seed_base(harness, project_id, status="disabled")
        disabled_document = await _seed_document(harness, project_id, disabled_base, status="failed", error_message="失败")
        with pytest.raises(KnowledgeError) as error:
            await service.retry_document(project_id, disabled_document)
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert "active" in error.value.message
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Segment preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_document_segments_returns_only_the_current_version(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    service = _documents_service(harness)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            other_project = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id)
        document_id = await _seed_document(harness, project_id, base_id, status="ready", version=2)
        async with harness.factory() as session, session.begin():
            for version, positions in ((1, 2), (2, 5)):
                for position in range(1, positions + 1):
                    session.add(
                        KnowledgeSegmentRow(
                            id=uuid.uuid4(),
                            project_id=project_id,
                            knowledge_base_id=base_id,
                            knowledge_document_id=document_id,
                            document_version=version,
                            position=position,
                            content=f"v{version} 分段 {position}",
                            source_position={"page": position},
                            embedding=[0.5] * 8,
                        )
                    )

        views, total = await service.list_document_segments(project_id, document_id, page=1, page_size=3)
        assert total == 5
        assert [view.position for view in views] == [1, 2, 3]
        assert all(view.document_version == 2 for view in views)
        assert views[0].content == "v2 分段 1"
        assert views[0].source_position == {"page": 1}

        second_page, _ = await service.list_document_segments(project_id, document_id, page=2, page_size=3)
        assert [view.position for view in second_page] == [4, 5]

        with pytest.raises(KnowledgeError) as error:
            await service.list_document_segments(other_project, document_id)
        assert error.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Delete services (mark + task management)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_document_service_marks_deleting_and_manages_open_tasks(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    service = _documents_service(harness)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id)
        document_id = await _seed_document(harness, project_id, base_id, status="processing")

        view = await service.delete_document(project_id, document_id)
        assert view.status == "deleting"
        assert view.version == 2  # in-flight ingest becomes a late result
        assert view.error_message is None
        assert view.delete_error is None

        # A second call while the delete task is open creates no duplicate.
        await service.delete_document(project_id, document_id)
        async with harness.factory() as session:
            open_tasks = await session.scalar(
                select(func.count())
                .select_from(KnowledgeTaskRow)
                .where(
                    KnowledgeTaskRow.resource_id == document_id,
                    KnowledgeTaskRow.kind == "delete_document",
                    KnowledgeTaskRow.status.in_(("queued", "running", "retry_wait")),
                )
            )
            assert int(open_tasks or 0) == 1

        # Simulate a finally-failed delete: the error becomes visible, and the
        # next delete call opens a fresh task which hides it again.
        async with harness.factory() as session, session.begin():
            task = await session.scalar(select(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == document_id))
            assert task is not None
            task.status = "failed"
            task.attempt_count = 3
            task.error_message = "对象存储删除失败"
            task.finished_at = datetime.now(UTC)
        failed_view = await service.get_document(project_id, document_id)
        assert failed_view.delete_error == "对象存储删除失败"

        retried_view = await service.delete_document(project_id, document_id)
        assert retried_view.status == "deleting"
        assert retried_view.version == 2  # no extra bump for an already-marked document
        assert retried_view.delete_error is None
        async with harness.factory() as session:
            open_tasks = await session.scalar(
                select(func.count())
                .select_from(KnowledgeTaskRow)
                .where(
                    KnowledgeTaskRow.resource_id == document_id,
                    KnowledgeTaskRow.kind == "delete_document",
                    KnowledgeTaskRow.status.in_(("queued", "running", "retry_wait")),
                )
            )
            assert int(open_tasks or 0) == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_delete_base_service_marks_deleting_and_manages_open_tasks(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    service = _bases_service(harness)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = await _seed_base(harness, project_id)
        await _seed_document(harness, project_id, base_id)

        view = await service.delete_knowledge_base(project_id, base_id)
        assert view.status == "deleting"
        assert view.document_count == 1
        assert view.delete_error is None

        await service.delete_knowledge_base(project_id, base_id)
        async with harness.factory() as session:
            open_tasks = await session.scalar(
                select(func.count())
                .select_from(KnowledgeTaskRow)
                .where(
                    KnowledgeTaskRow.resource_id == base_id,
                    KnowledgeTaskRow.kind == "delete_knowledge_base",
                    KnowledgeTaskRow.status.in_(("queued", "running", "retry_wait")),
                )
            )
            assert int(open_tasks or 0) == 1

        with pytest.raises(KnowledgeError) as error:
            await service.delete_knowledge_base(uuid.uuid4(), base_id)
        assert error.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()
