"""M4 gates: the Knowledge task worker loop and Worker-process integration.

Covers claim-execute-settle against real PostgreSQL, restart recovery of
expired leases, the paired-loop shutdown contract, and the retention-purge
Knowledge gate.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from actweave_knowledge import KNOWLEDGE_PARSE_FAILED, KnowledgeError
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeMetadataFieldRow,
    KnowledgeQueryRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from actweave_knowledge.tasks import KnowledgeTaskClaim, KnowledgeTaskWorker, purge_project_knowledge
from registry_helpers import seed_registry_models
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.knowledge.composition import (
    create_knowledge_worker_resources_from_app_config,
    is_knowledge_project_active,
)
from app.knowledge.worker import run_worker_loops
from app.private_work.retention_jobs import project_retention_key
from app.projects.errors import ProjectDeletionStateConflict
from app.projects.lifecycle_repository import ProjectLifecycleRepository
from app.worker.retention import KnowledgePurgeIncomplete, RetentionPurgeJobHandler
from app.worker.service import JobSettlement
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import JobClaim, JobScope
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Harness:
    def __init__(self, engine, factory) -> None:  # noqa: ANN001
        self.engine = engine
        self.factory = factory


async def _harness(postgres_database_url: str) -> _Harness:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _install_full_schema(engine)
    return _Harness(engine, factory)


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
        {"user_id": user_id, "email": f"{label}@example.invalid", "username": f"m4w_{label}"},
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:project_id, :slug, :display_name, :user_id)"""
        ),
        {"project_id": project_id, "slug": f"m4w-{label}", "display_name": label, "user_id": user_id},
    )
    return project_id


async def _seed_retained_knowledge_graph(
    harness: _Harness,
    *,
    label: str,
) -> tuple[uuid.UUID, str]:
    """Seed every Project-owned Knowledge relation plus one stored-object key."""

    embedding_model_id, _ = await seed_registry_models(harness.factory, dimension=8)
    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, label)
        project = await session.get(ProjectRow, project_id)
        assert project is not None
        base_id = uuid.uuid4()
        document_id = uuid.uuid4()
        segment_id = uuid.uuid4()
        storage_key = f"projects/{project_id}/knowledge/{base_id}/{document_id}.md"
        session.add(
            KnowledgeBaseRow(
                id=base_id,
                project_id=project_id,
                name=f"base-{label}",
                embedding_model_id=embedding_model_id,
            )
        )
        await session.flush()
        session.add_all(
            [
                KnowledgeMetadataFieldRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    knowledge_base_id=base_id,
                    name="department",
                    field_type="string",
                ),
                KnowledgeDocumentRow(
                    id=document_id,
                    project_id=project_id,
                    knowledge_base_id=base_id,
                    name="retained.md",
                    original_name="retained.md",
                    storage_key=storage_key,
                    size_bytes=8,
                    status="ready",
                ),
            ]
        )
        await session.flush()
        session.add(
            KnowledgeSegmentRow(
                id=segment_id,
                project_id=project_id,
                knowledge_base_id=base_id,
                knowledge_document_id=document_id,
                document_version=1,
                position=1,
                content="retained",
                embedding=None,
            )
        )
        await session.flush()
        session.add_all(
            [
                KnowledgeSegmentChildRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    knowledge_base_id=base_id,
                    knowledge_document_id=document_id,
                    knowledge_segment_id=segment_id,
                    document_version=1,
                    position=1,
                    content="retained",
                    embedding=[0.1] * 8,
                ),
                KnowledgeTaskRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    resource_id=document_id,
                    kind="ingest_document",
                    target_version=1,
                ),
                KnowledgeQueryRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    owner_user_id=project.created_by_user_id,
                    knowledge_base_ids=[str(base_id)],
                    query="retained query",
                    source="retrieval_test",
                ),
            ]
        )
    return project_id, storage_key


async def _seed_task(
    harness: _Harness,
    project_id: uuid.UUID,
    *,
    resource_id: uuid.UUID | None = None,
    kind: str = "ingest_document",
    target_version: int | None = 1,
    storage_key: str | None = None,
    status: str = "queued",
    attempt_count: int = 0,
    claim_token: uuid.UUID | None = None,
    lease_until: datetime | None = None,
) -> uuid.UUID:
    task_id = uuid.uuid4()
    async with harness.factory() as session, session.begin():
        session.add(
            KnowledgeTaskRow(
                id=task_id,
                project_id=project_id,
                resource_id=resource_id or uuid.uuid4(),
                kind=kind,
                target_version=target_version,
                storage_key=storage_key,
                status=status,
                attempt_count=attempt_count,
                claim_token=claim_token,
                lease_until=lease_until,
            )
        )
    return task_id


async def _task_row(harness: _Harness, task_id: uuid.UUID) -> KnowledgeTaskRow:
    async with harness.factory() as session:
        row = await session.get(KnowledgeTaskRow, task_id)
        assert row is not None
        return row


async def _wait_until(predicate: Callable[[], Awaitable[bool]], *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was not reached in time")


def _worker(
    harness: _Harness,
    handlers: dict,  # noqa: ANN001
    *,
    task_timeout_seconds: int = 30,
    concurrency: int = 1,
) -> KnowledgeTaskWorker:
    return KnowledgeTaskWorker(
        session_factory=harness.factory,
        handlers=handlers,
        project_active_check=is_knowledge_project_active,
        concurrency=concurrency,
        task_timeout_seconds=task_timeout_seconds,
        poll_interval_seconds=0.05,
        retry_delay_seconds=0,
    )


async def _run_worker_until(
    worker: KnowledgeTaskWorker,
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout: float = 20.0,
) -> None:
    stop_event = asyncio.Event()
    run = asyncio.create_task(worker.run(stop_event))
    try:
        await _wait_until(predicate, timeout=timeout)
    finally:
        stop_event.set()
        await asyncio.wait_for(run, timeout=10)


# ---------------------------------------------------------------------------
# Task worker loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_executes_a_queued_task_and_settles_success(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        task_id = await _seed_task(harness, project_id)
        seen: list[KnowledgeTaskClaim] = []

        async def handler(claim: KnowledgeTaskClaim) -> None:
            seen.append(claim)

        async def settled() -> bool:
            return (await _task_row(harness, task_id)).status == "succeeded"

        await _run_worker_until(_worker(harness, {"ingest_document": handler}), settled)

        assert len(seen) == 1
        assert seen[0].id == task_id
        assert seen[0].attempt_count == 1
        row = await _task_row(harness, task_id)
        assert row.claim_token is None and row.lease_until is None
        assert row.finished_at is not None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_worker_pauses_pending_project_task_and_runs_after_restore(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            project = await session.get(ProjectRow, project_id)
            assert project is not None
            project.status = "pending_deletion"
            project.deletion_requested_at = datetime.now(UTC)
            project.deletion_effective_at = datetime.now(UTC) + timedelta(days=30)
        task_id = await _seed_task(harness, project_id)
        seen: list[KnowledgeTaskClaim] = []

        async def handler(claim: KnowledgeTaskClaim) -> None:
            seen.append(claim)

        async def paused() -> bool:
            return (await _task_row(harness, task_id)).status == "retry_wait"

        await _run_worker_until(
            _worker(harness, {"ingest_document": handler}),
            paused,
        )

        assert seen == []
        row = await _task_row(harness, task_id)
        assert row.attempt_count == 0
        assert row.claim_token is None and row.lease_until is None
        assert row.finished_at is None

        # Simulate the bounded pause elapsing after the user restores Project.
        async with harness.factory() as session, session.begin():
            project = await session.get(ProjectRow, project_id)
            task = await session.get(KnowledgeTaskRow, task_id)
            assert project is not None and task is not None
            project.status = "active"
            project.deletion_requested_at = None
            project.deletion_effective_at = None
            task.available_at = datetime.now(UTC) - timedelta(seconds=1)

        async def settled() -> bool:
            return (await _task_row(harness, task_id)).status == "succeeded"

        await _run_worker_until(
            _worker(harness, {"ingest_document": handler}),
            settled,
        )

        assert [claim.id for claim in seen] == [task_id]
        assert (await _task_row(harness, task_id)).attempt_count == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_project_purge_waits_for_running_task_before_deleting_storage(
    postgres_database_url: str,
) -> None:
    """A live handler and Project physical purge cannot overlap destructively."""

    class _ProjectStore:
        def __init__(self, key: str) -> None:
            self.objects = {key}
            self.deleted: list[str] = []

        async def require_unversioned_bucket(self) -> None:
            return None

        async def delete_many(self, keys: list[str]) -> None:
            for key in keys:
                self.objects.discard(key)
                self.deleted.append(key)

        async def delete_project_objects(self, project_id: uuid.UUID) -> None:
            prefix = f"projects/{project_id}/knowledge/"
            for key in tuple(self.objects):
                if key.startswith(prefix):
                    self.objects.remove(key)
                    self.deleted.append(key)

    harness = await _harness(postgres_database_url)
    stop_event = asyncio.Event()
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    run: asyncio.Task[None] | None = None
    try:
        project_id, storage_key = await _seed_retained_knowledge_graph(
            harness,
            label=uuid.uuid4().hex[:8],
        )
        queued_task_id = await _seed_task(
            harness,
            project_id,
            resource_id=uuid.uuid4(),
        )
        store = _ProjectStore(storage_key)

        async def handler(claim: KnowledgeTaskClaim) -> None:
            del claim
            handler_started.set()
            await release_handler.wait()

        worker = _worker(harness, {"ingest_document": handler})
        run = asyncio.create_task(worker.run(stop_event))
        await asyncio.wait_for(handler_started.wait(), timeout=5)

        async with harness.factory() as session, session.begin():
            project = await session.get(ProjectRow, project_id)
            assert project is not None
            project.status = "pending_deletion"
            project.deletion_requested_at = datetime.now(UTC)
            project.deletion_effective_at = datetime.now(UTC)

        completed = await purge_project_knowledge(
            harness.factory,
            store,  # type: ignore[arg-type] - external object-store boundary
            project_id=project_id,
        )

        assert completed is False
        assert store.deleted == []
        async with harness.factory() as session:
            assert await session.get(KnowledgeTaskRow, queued_task_id) is None
            running = await session.scalar(
                select(KnowledgeTaskRow.status).where(
                    KnowledgeTaskRow.project_id == project_id,
                    KnowledgeTaskRow.kind == "ingest_document",
                )
            )
            remaining_documents = await session.scalar(select(func.count()).select_from(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id))
        assert running == "running"
        assert int(remaining_documents or 0) == 1

        release_handler.set()

        async def handler_settled() -> bool:
            async with harness.factory() as session:
                status = await session.scalar(
                    select(KnowledgeTaskRow.status).where(
                        KnowledgeTaskRow.project_id == project_id,
                        KnowledgeTaskRow.kind == "ingest_document",
                    )
                )
            return status == "succeeded"

        await _wait_until(handler_settled)
        stop_event.set()
        await asyncio.wait_for(run, timeout=10)
        run = None

        assert await purge_project_knowledge(
            harness.factory,
            store,  # type: ignore[arg-type] - external object-store boundary
            project_id=project_id,
        )
        assert store.objects == set()
    finally:
        release_handler.set()
        stop_event.set()
        if run is not None:
            await asyncio.gather(run, return_exceptions=True)
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_worker_claim_carries_exact_object_cleanup_key(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        document_id = uuid.uuid4()
        storage_key = f"projects/{project_id}/knowledge/{uuid.uuid4()}/{document_id}.pdf"
        task_id = await _seed_task(
            harness,
            project_id,
            resource_id=document_id,
            kind="delete_document_object",
            target_version=None,
            storage_key=storage_key,
        )
        seen: list[KnowledgeTaskClaim] = []

        async def handler(claim: KnowledgeTaskClaim) -> None:
            seen.append(claim)

        async def settled() -> bool:
            return (await _task_row(harness, task_id)).status == "succeeded"

        await _run_worker_until(
            _worker(harness, {"delete_document_object": handler}),
            settled,
        )

        assert len(seen) == 1
        assert seen[0].resource_id == document_id
        assert seen[0].storage_key == storage_key
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_worker_retries_failures_and_fails_after_three_attempts(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        task_id = await _seed_task(harness, project_id)
        attempts: list[int] = []

        async def handler(claim: KnowledgeTaskClaim) -> None:
            attempts.append(claim.attempt_count)
            raise KnowledgeError(KNOWLEDGE_PARSE_FAILED, "解析失败")

        async def failed() -> bool:
            return (await _task_row(harness, task_id)).status == "failed"

        await _run_worker_until(_worker(harness, {"ingest_document": handler}), failed)

        assert attempts == [1, 2, 3]
        row = await _task_row(harness, task_id)
        assert row.attempt_count == 3
        assert row.error_message == "解析失败"
        assert row.finished_at is not None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_worker_times_out_a_hung_handler_into_retry(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        task_id = await _seed_task(harness, project_id)
        started = asyncio.Event()

        async def hung_handler(claim: KnowledgeTaskClaim) -> None:
            started.set()
            await asyncio.sleep(3600)

        async def waited() -> bool:
            row = await _task_row(harness, task_id)
            return row.status in {"retry_wait", "failed"} and row.error_message is not None

        await _run_worker_until(_worker(harness, {"ingest_document": hung_handler}, task_timeout_seconds=1), waited)

        assert started.is_set()
        row = await _task_row(harness, task_id)
        assert "超过 1 秒" in (row.error_message or "")
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_worker_recovers_an_expired_lease_after_restart(postgres_database_url: str) -> None:
    """A crashed Worker's running claim is recovered and re-executed."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        task_id = await _seed_task(
            harness,
            project_id,
            status="running",
            attempt_count=1,
            claim_token=uuid.uuid4(),
            lease_until=datetime.now(UTC) - timedelta(seconds=5),
        )
        executed = asyncio.Event()

        async def handler(claim: KnowledgeTaskClaim) -> None:
            executed.set()

        async def settled() -> bool:
            return (await _task_row(harness, task_id)).status == "succeeded"

        await _run_worker_until(_worker(harness, {"ingest_document": handler}), settled)
        assert executed.is_set()
        assert (await _task_row(harness, task_id)).attempt_count == 2
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_worker_claims_nothing_after_the_stop_event(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        task_id = await _seed_task(harness, project_id)

        async def handler(claim: KnowledgeTaskClaim) -> None:  # pragma: no cover - must not run
            raise AssertionError("stop event must prevent claims")

        stop_event = asyncio.Event()
        stop_event.set()
        worker = _worker(harness, {"ingest_document": handler})
        await asyncio.wait_for(worker.run(stop_event), timeout=10)

        assert (await _task_row(harness, task_id)).status == "queued"
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Paired loops (Worker process integration)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_loops_knowledge_failure_stops_the_main_loop() -> None:
    stop_event = asyncio.Event()
    main_finished = asyncio.Event()

    async def run_main() -> None:
        await stop_event.wait()
        main_finished.set()

    async def run_knowledge() -> None:
        raise RuntimeError("knowledge loop crashed")

    with pytest.raises(RuntimeError, match="knowledge loop crashed"):
        await asyncio.wait_for(
            run_worker_loops(run_main=run_main, run_knowledge=run_knowledge, stop_event=stop_event),
            timeout=10,
        )
    assert stop_event.is_set()
    assert main_finished.is_set()


@pytest.mark.asyncio
async def test_run_worker_loops_main_failure_stops_the_knowledge_loop() -> None:
    stop_event = asyncio.Event()
    knowledge_finished = asyncio.Event()

    async def run_main() -> None:
        raise RuntimeError("main loop crashed")

    async def run_knowledge() -> None:
        await stop_event.wait()
        knowledge_finished.set()

    with pytest.raises(RuntimeError, match="main loop crashed"):
        await asyncio.wait_for(
            run_worker_loops(run_main=run_main, run_knowledge=run_knowledge, stop_event=stop_event),
            timeout=10,
        )
    assert knowledge_finished.is_set()


@pytest.mark.asyncio
async def test_run_worker_loops_reports_both_failures_when_loops_die_together(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only one exception can propagate; the other must still reach the log."""

    stop_event = asyncio.Event()

    async def run_main() -> None:
        raise RuntimeError("main loop crashed")

    async def run_knowledge() -> None:
        raise RuntimeError("knowledge loop crashed")

    with caplog.at_level("ERROR", logger="app.knowledge.worker"), pytest.raises(RuntimeError, match="loop crashed"):
        await asyncio.wait_for(
            run_worker_loops(run_main=run_main, run_knowledge=run_knowledge, stop_event=stop_event),
            timeout=10,
        )

    assert any("also failed" in record.message for record in caplog.records)
    logged_failures = [record.exc_info[1] for record in caplog.records if record.exc_info]
    assert any("loop crashed" in str(failure) for failure in logged_failures)


@pytest.mark.asyncio
async def test_run_worker_loops_normal_stop_finishes_both_loops() -> None:
    stop_event = asyncio.Event()
    finished: list[str] = []

    async def run_main() -> None:
        await stop_event.wait()
        finished.append("main")

    async def run_knowledge() -> None:
        await stop_event.wait()
        finished.append("knowledge")

    run = asyncio.create_task(run_worker_loops(run_main=run_main, run_knowledge=run_knowledge, stop_event=stop_event))
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.wait_for(run, timeout=10)

    assert sorted(finished) == ["knowledge", "main"]


# ---------------------------------------------------------------------------
# Retention purge gate
# ---------------------------------------------------------------------------


class _FakeReconciler:
    def __init__(self, sequence: list[str]) -> None:
        self._sequence = sequence

    async def reconcile_once(self) -> None:
        self._sequence.append("reconcile")


class _OneOrNoneResult:
    def __init__(self, row: object) -> None:
        self._row = row

    def one_or_none(self) -> object:
        return self._row


class _FakeGateSession:
    """Answers the gate's job query, then its project FOR SHARE query."""

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    async def execute(self, statement) -> _OneOrNoneResult:  # noqa: ANN001
        return _OneOrNoneResult(self._rows.pop(0))

    async def __aenter__(self) -> _FakeGateSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


_GATE_DEADLINE = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)


def _gate_job_row(project_id: uuid.UUID, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "retention_resource_kind": "project",
        "cancel_requested_at": None,
        "retention_effective_at": _GATE_DEADLINE,
        "owner_private_generation": 3,
        "idempotency_key": project_retention_key(project_id, _GATE_DEADLINE),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _gate_project_row(**overrides: object) -> SimpleNamespace | None:
    values: dict[str, object] = {
        "status": "pending_deletion",
        "deletion_effective_at": _GATE_DEADLINE,
        "membership_version": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _gate_handler(
    *,
    rows: list[object],
    sequence: list[str],
    purge_results: list[bool],
) -> tuple[RetentionPurgeJobHandler, list[uuid.UUID]]:
    """Build a handler instance with only the collaborators the gate touches."""

    purged: list[uuid.UUID] = []

    async def knowledge_purge(project_id: uuid.UUID) -> bool:
        sequence.append("knowledge_purge")
        purged.append(project_id)
        return purge_results.pop(0)

    handler = RetentionPurgeJobHandler.__new__(RetentionPurgeJobHandler)
    handler._sessions = lambda: _FakeGateSession(rows)  # noqa: SLF001
    handler._mount_owner_reconciler = _FakeReconciler(sequence)  # noqa: SLF001
    handler._knowledge_purge = knowledge_purge  # noqa: SLF001
    return handler, purged


def _retention_claim(project_id: uuid.UUID) -> JobClaim:
    return JobClaim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token="lease-token",
        job_type="retention_purge",
        scope=JobScope(project_id=project_id, owner_user_id=str(uuid.uuid4())),
        run_id=None,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
    )


@pytest.mark.asyncio
async def test_retention_purge_runs_knowledge_purge_before_the_governance_commit() -> None:
    sequence: list[str] = []
    project_id = uuid.uuid4()
    rows: list[object] = [_gate_job_row(project_id), _gate_project_row()]
    handler, purged = _gate_handler(rows=rows, sequence=sequence, purge_results=[True])

    settlement = await handler(_retention_claim(project_id), authority=None)

    assert isinstance(settlement, JobSettlement)
    assert sequence == ["reconcile", "knowledge_purge"]
    assert purged == [project_id]


@pytest.mark.asyncio
async def test_retention_purge_stops_when_knowledge_purge_is_incomplete() -> None:
    sequence: list[str] = []
    project_id = uuid.uuid4()
    rows: list[object] = [_gate_job_row(project_id), _gate_project_row()]
    handler, _ = _gate_handler(rows=rows, sequence=sequence, purge_results=[False])

    with pytest.raises(KnowledgePurgeIncomplete):
        await handler(_retention_claim(project_id), authority=None)


@pytest.mark.asyncio
async def test_retention_purge_fails_closed_when_knowledge_cleanup_is_unavailable() -> None:
    """Project retention must never infer that a disabled feature has no data."""

    sequence: list[str] = []
    project_id = uuid.uuid4()
    rows: list[object] = [_gate_job_row(project_id), _gate_project_row()]
    handler, _ = _gate_handler(rows=rows, sequence=sequence, purge_results=[])
    handler._knowledge_purge = None  # noqa: SLF001 - exercise the public handler's fail-closed seam

    with pytest.raises(KnowledgePurgeIncomplete):
        await handler(_retention_claim(project_id), authority=None)


@pytest.mark.asyncio
async def test_disabled_worker_retention_with_preserved_storage_removes_project_knowledge(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling product surfaces must retain the Project cleanup capability."""

    harness = await _harness(postgres_database_url)
    bucket = "retained-knowledge"
    objects: set[tuple[str, str]] = set()

    class _FakeMinio:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def remove_object(self, object_bucket: str, key: str) -> None:
            objects.discard((object_bucket, key))

        def bucket_exists(self, object_bucket: str) -> bool:
            return object_bucket == bucket

        def get_bucket_versioning(self, object_bucket: str) -> SimpleNamespace:
            assert object_bucket == bucket
            return SimpleNamespace(status=None)

        def list_objects(
            self,
            object_bucket: str,
            *,
            prefix: str,
            recursive: bool,
        ) -> list[SimpleNamespace]:
            assert recursive is True
            return [SimpleNamespace(object_name=key) for bucket_name, key in objects if bucket_name == object_bucket and key.startswith(prefix)]

    try:
        label = uuid.uuid4().hex[:8]
        project_id, storage_key = await _seed_retained_knowledge_graph(
            harness,
            label=label,
        )
        objects.add((bucket, storage_key))

        import actweave_knowledge.storage.minio_store as minio_store_module

        import deerflow.persistence.engine as engine_module

        monkeypatch.setattr(minio_store_module, "Minio", _FakeMinio)
        monkeypatch.setattr(engine_module, "get_session_factory", lambda: harness.factory)
        resources = create_knowledge_worker_resources_from_app_config(
            SimpleNamespace(
                model_extra={
                    "knowledge": {
                        "enabled": False,
                        "minio": {
                            "endpoint": "minio.invalid:9000",
                            "bucket": bucket,
                            "access_key": "retained-access",
                            "secret_key": "retained-secret",
                        },
                    }
                }
            )
        )
        assert resources.feature_module is None

        sequence: list[str] = []
        rows: list[object] = [_gate_job_row(project_id), _gate_project_row()]
        handler, _ = _gate_handler(rows=rows, sequence=sequence, purge_results=[])
        handler._knowledge_purge = resources.project_purge  # noqa: SLF001

        settlement = await handler(_retention_claim(project_id), authority=None)

        assert isinstance(settlement, JobSettlement)
        assert objects == set()
        async with harness.factory() as session:
            for model in (
                KnowledgeBaseRow,
                KnowledgeDocumentRow,
                KnowledgeMetadataFieldRow,
                KnowledgeSegmentRow,
                KnowledgeSegmentChildRow,
                KnowledgeTaskRow,
                KnowledgeQueryRow,
            ):
                remaining = await session.scalar(select(func.count()).select_from(model).where(model.project_id == project_id))
                assert int(remaining or 0) == 0, model.__name__
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_disabled_worker_retention_without_storage_retries_when_documents_remain(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing cleanup config cannot turn historical documents into success."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, _ = await _seed_retained_knowledge_graph(
            harness,
            label=uuid.uuid4().hex[:8],
        )
        import deerflow.persistence.engine as engine_module

        monkeypatch.setattr(engine_module, "get_session_factory", lambda: harness.factory)
        resources = create_knowledge_worker_resources_from_app_config(SimpleNamespace(model_extra={"knowledge": {"enabled": False}}))
        assert resources.feature_module is None

        sequence: list[str] = []
        rows: list[object] = [_gate_job_row(project_id), _gate_project_row()]
        handler, _ = _gate_handler(rows=rows, sequence=sequence, purge_results=[])
        handler._knowledge_purge = resources.project_purge  # noqa: SLF001

        with pytest.raises(KnowledgePurgeIncomplete):
            await handler(_retention_claim(project_id), authority=None)

        async with harness.factory() as session:
            remaining_documents = await session.scalar(select(func.count()).select_from(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id))
            assert int(remaining_documents or 0) == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_disabled_worker_retention_without_historical_documents_can_continue(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-disabled deployments must not block ordinary Project retention."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        import deerflow.persistence.engine as engine_module

        monkeypatch.setattr(engine_module, "get_session_factory", lambda: harness.factory)
        resources = create_knowledge_worker_resources_from_app_config(SimpleNamespace(model_extra={"knowledge": {"enabled": False}}))

        sequence: list[str] = []
        rows: list[object] = [_gate_job_row(project_id), _gate_project_row()]
        handler, _ = _gate_handler(rows=rows, sequence=sequence, purge_results=[])
        handler._knowledge_purge = resources.project_purge  # noqa: SLF001

        settlement = await handler(_retention_claim(project_id), authority=None)

        assert isinstance(settlement, JobSettlement)
        assert sequence == ["reconcile"]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_disabled_worker_without_storage_fails_closed_for_object_cleanup_task(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact-key orphan task proves bytes may remain even without a row."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            document_id = uuid.uuid4()
            session.add(
                KnowledgeTaskRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    resource_id=document_id,
                    kind="delete_document_object",
                    target_version=None,
                    storage_key=(f"projects/{project_id}/knowledge/{uuid.uuid4()}/{document_id}.pdf"),
                )
            )

        import deerflow.persistence.engine as engine_module

        monkeypatch.setattr(
            engine_module,
            "get_session_factory",
            lambda: harness.factory,
        )
        resources = create_knowledge_worker_resources_from_app_config(SimpleNamespace(model_extra={"knowledge": {"enabled": False}}))

        assert await resources.project_purge(project_id) is False
        async with harness.factory() as session:
            remaining = await session.scalar(
                select(func.count())
                .select_from(KnowledgeTaskRow)
                .where(
                    KnowledgeTaskRow.project_id == project_id,
                    KnowledgeTaskRow.kind == "delete_document_object",
                )
            )
        assert int(remaining or 0) == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_disabled_worker_without_storage_accepts_succeeded_object_cleanup_proof(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A succeeded exact-key cleanup no longer represents possibly retained bytes."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            document_id = uuid.uuid4()
            session.add(
                KnowledgeTaskRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    resource_id=document_id,
                    kind="delete_document_object",
                    target_version=None,
                    storage_key=(f"projects/{project_id}/knowledge/{uuid.uuid4()}/{document_id}.pdf"),
                    status="succeeded",
                    attempt_count=1,
                    finished_at=datetime.now(UTC),
                )
            )

        import deerflow.persistence.engine as engine_module

        monkeypatch.setattr(
            engine_module,
            "get_session_factory",
            lambda: harness.factory,
        )
        resources = create_knowledge_worker_resources_from_app_config(SimpleNamespace(model_extra={"knowledge": {"enabled": False}}))

        assert await resources.project_purge(project_id) is True
        async with harness.factory() as session:
            remaining = await session.scalar(select(func.count()).select_from(KnowledgeTaskRow).where(KnowledgeTaskRow.project_id == project_id))
        assert int(remaining or 0) == 0
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_retention_purge_skips_knowledge_for_non_project_kinds() -> None:
    sequence: list[str] = []
    project_id = uuid.uuid4()
    rows: list[object] = [_gate_job_row(project_id, retention_resource_kind="account")]
    handler, purged = _gate_handler(rows=rows, sequence=sequence, purge_results=[])

    settlement = await handler(_retention_claim(project_id), authority=None)

    assert isinstance(settlement, JobSettlement)
    assert purged == []
    assert sequence == ["reconcile"]


@pytest.mark.parametrize(
    ("job_overrides", "project_overrides", "reason"),
    [
        pytest.param({"cancel_requested_at": _GATE_DEADLINE}, {}, "cancel requested", id="cancelled"),
        pytest.param({}, {"status": "active", "deletion_effective_at": None}, "project restored", id="restored"),
        pytest.param({}, {"membership_version": 4}, "generation drift", id="generation-drift"),
        pytest.param({}, {"deletion_effective_at": _GATE_DEADLINE + timedelta(days=1)}, "deadline changed", id="deadline-changed"),
        pytest.param({"idempotency_key": "0" * 64}, {}, "idempotency key mismatch", id="key-mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_retention_purge_declines_knowledge_when_not_eligible(
    job_overrides: dict[str, object],
    project_overrides: dict[str, object],
    reason: str,
) -> None:
    """The irreversible Knowledge purge never runs when commit() would not purge."""

    sequence: list[str] = []
    project_id = uuid.uuid4()
    rows: list[object] = [_gate_job_row(project_id, **job_overrides), _gate_project_row(**project_overrides)]
    handler, purged = _gate_handler(rows=rows, sequence=sequence, purge_results=[])

    settlement = await handler(_retention_claim(project_id), authority=None)

    assert isinstance(settlement, JobSettlement), reason
    assert purged == [], reason
    assert sequence == ["reconcile"], reason


@pytest.mark.asyncio
async def test_retention_purge_declines_knowledge_when_the_project_is_gone() -> None:
    sequence: list[str] = []
    project_id = uuid.uuid4()
    rows: list[object] = [_gate_job_row(project_id), None]
    handler, purged = _gate_handler(rows=rows, sequence=sequence, purge_results=[])

    settlement = await handler(_retention_claim(project_id), authority=None)

    assert isinstance(settlement, JobSettlement)
    assert purged == []


@pytest.mark.asyncio
async def test_restore_is_refused_while_a_project_purge_job_is_claimed(postgres_database_url: str) -> None:
    """Restore and a claimed purge are mutually exclusive.

    A claimed purge may already be deleting Knowledge objects outside any
    transaction, so a restore that raced past it would reactivate a project
    whose Knowledge data is gone. The purge Worker's own FOR SHARE gate covers
    the other interleaving.
    """

    harness = await _harness(postgres_database_url)
    try:
        deadline = datetime.now(UTC) + timedelta(days=1)
        user_uuid = uuid.uuid4()
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            await session.execute(
                text(
                    """INSERT INTO users (
                           id, email, username, system_role, created_at,
                           needs_setup, token_version
                       ) VALUES (:user_id, :email, :username, 'user', now(), false, 1)"""
                ),
                {"user_id": str(user_uuid), "email": f"{user_uuid.hex[:8]}@example.invalid", "username": f"restorer_{user_uuid.hex[:8]}"},
            )
            project = await session.get(ProjectRow, project_id)
            assert project is not None
            project.status = "pending_deletion"
            project.deletion_requested_at = datetime.now(UTC)
            project.deletion_effective_at = deadline
            session.add(
                ProjectMembershipRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    user_id=str(user_uuid),
                    role="admin",
                    status="active",
                )
            )
            job_id = uuid.uuid4()
            session.add(
                JobRow(
                    id=job_id,
                    job_type="retention_purge",
                    project_id=project_id,
                    owner_user_id=None,
                    owner_private_generation=1,
                    retention_resource_kind="project",
                    retention_effective_at=deadline,
                    idempotency_key=project_retention_key(project_id, deadline),
                    status="leased",
                    max_attempts=5,
                )
            )

        now = datetime.now(UTC)
        async with harness.factory() as session, session.begin():
            with pytest.raises(ProjectDeletionStateConflict):
                await ProjectLifecycleRepository(session).lock_restore(user_uuid, project_id, now)

        async with harness.factory() as session, session.begin():
            job = await session.get(JobRow, job_id)
            assert job is not None
            job.status = "succeeded"

        async with harness.factory() as session, session.begin():
            project, actor = await ProjectLifecycleRepository(session).lock_restore(user_uuid, project_id, now)
            assert project.id == project_id
            assert actor.user_id == str(user_uuid)
    finally:
        await harness.engine.dispose()
