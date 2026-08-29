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
from actweave_knowledge.persistence.models import KnowledgeTaskRow
from actweave_knowledge.tasks import KnowledgeTaskClaim, KnowledgeTaskWorker
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


async def _seed_task(
    harness: _Harness,
    project_id: uuid.UUID,
    *,
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
                resource_id=uuid.uuid4(),
                kind="ingest_document",
                target_version=1,
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
