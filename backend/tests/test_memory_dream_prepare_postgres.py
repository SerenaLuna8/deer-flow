from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from support.private_thread_seed import PrivateThreadSeed, seed_private_thread_database
from support.system_model_seed import (
    frozen_system_model_execution,
    seed_system_model_config,
)

from app.personalization.repository import AccountPersonalizationRepository
from app.private_work.errors import PrivateWorkUnavailable
from app.private_work.memory_dream_prepare_service import MemoryDreamPrepareService
from app.projects.context import resolve_project_context_in_transaction
from deerflow.agents.memory.dream import (
    DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
    DREAM_PROMPT_VERSION,
    EMPTY_MEMORY_DOCUMENT,
)
from deerflow.persistence.jobs.model import JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import JobClaim, JobOwnerRef, JobRepository
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    MemoryDreamPrepareRunRow,
    MemoryDreamRunRow,
    MemoryHistoryEntryRow,
)
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryDreamAdmissionRecord,
    MemoryDreamFrozenRuntime,
)
from deerflow.persistence.private_work.memory_dream_prepare_repository import (
    MemoryDreamPrepareConflict,
    MemoryDreamPrepareRepository,
)
from deerflow.persistence.system_runtime_settings import SystemRuntimePolicyRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow


def _owner_ref(_owner_user_id: str) -> JobOwnerRef:
    return JobOwnerRef(key_id="memory-prepare-test", hmac_hex="f" * 64)


def _jobs(session) -> JobRepository:
    return JobRepository(session, owner_ref_hasher=_owner_ref)


def _scope(seed: PrivateThreadSeed) -> MemoryDocumentScope:
    return MemoryDocumentScope(
        project_id=uuid.UUID(seed.owner_a.resource_scope.project_id),
        owner_user_id=seed.owner_a.resource_scope.owner_user_id,
    )


async def _lock_scope(session, seed: PrivateThreadSeed) -> None:
    await resolve_project_context_in_transaction(
        session,
        uuid.UUID(seed.owner_a.resource_scope.owner_user_id),
        uuid.UUID(seed.owner_a.resource_scope.project_id),
        "memory-dream-prepare-postgres",
        lock=True,
    )


async def _add_thread(
    session,
    seed: PrivateThreadSeed,
    label: str,
) -> str:
    scope = _scope(seed)
    thread_id = f"prepare-{label}-{uuid.uuid4().hex[:12]}"
    session.add(
        ThreadMetaRow(
            thread_id=thread_id,
            assistant_id=str(seed.project_agent_id),
            owner_user_id=scope.owner_user_id,
            display_name=f"Dream prepare {label}",
            status="idle",
            metadata_json={},
            project_id=scope.project_id,
            agent_asset_id=seed.project_agent_id,
            agent_scope="project",
        )
    )
    await session.flush()
    return thread_id


async def _admit(
    session,
    seed: PrivateThreadSeed,
    *,
    thread_id: str,
    operation_id: uuid.UUID,
    now: datetime,
    max_attempts: int = 3,
):
    await _lock_scope(session, seed)
    return await MemoryDreamPrepareRepository(
        session,
        jobs=_jobs(session),
    ).admit(
        _scope(seed),
        thread_id=thread_id,
        operation_id=operation_id,
        request_id="memory-dream-prepare-postgres",
        now=now,
        max_attempts=max_attempts,
    )


async def _claim_prepare(
    seed: PrivateThreadSeed,
    *,
    now: datetime,
) -> JobClaim:
    worker_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="memory-dream-prepare-postgres",
                capabilities_json=["memory_dream_prepare"],
                max_concurrent_jobs=1,
                draining=False,
                started_at=now,
                heartbeat_at=now,
            )
        )
    async with seed.factory() as session, session.begin():
        jobs = _jobs(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"memory_dream_prepare"}),
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


async def _claim_dream(
    seed: PrivateThreadSeed,
    *,
    now: datetime,
    mark_running: bool = True,
) -> JobClaim:
    worker_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="memory-dream-prepare-postgres",
                capabilities_json=["memory_dream"],
                max_concurrent_jobs=1,
                draining=False,
                started_at=now,
                heartbeat_at=now,
            )
        )
    async with seed.factory() as session, session.begin():
        jobs = _jobs(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"memory_dream"}),
            lease_seconds=60,
            now=now,
        )
        assert claim is not None
        if mark_running:
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
                now=now,
            )
        return claim


async def _wait_for_lock(seed: PrivateThreadSeed, backend_pid: int) -> None:
    for _ in range(300):
        async with seed.factory() as session:
            wait_type = await session.scalar(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"),
                {"pid": backend_pid},
            )
        if wait_type == "Lock":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("backend did not reach the forced lock wait")


async def _frozen_dream(
    session,
    seed: PrivateThreadSeed,
    *,
    now: datetime,
) -> tuple[MemoryDreamFrozenRuntime, uuid.UUID]:
    scope = _scope(seed)
    preference = await AccountPersonalizationRepository(session).read_memory(uuid.UUID(scope.owner_user_id))
    model_id = uuid.uuid4()
    await seed_system_model_config(
        session,
        model_id=model_id,
        owner_user_id=scope.owner_user_id,
        display_name="Memory prepare PostgreSQL model",
        provider_model="memory-prepare-test",
    )
    source_run_id = str(uuid.uuid4())
    tagged_text = "- [durable] Prepared Dream child admission evidence."
    session.add(
        MemoryHistoryEntryRow(
            id=uuid.uuid4(),
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            namespace=scope.namespace,
            thread_id="prepare-child-history",
            origin="tool",
            source_run_id=source_run_id,
            source_checkpoint_id=None,
            committed_checkpoint_id=None,
            source_digest=hashlib.sha256(source_run_id.encode()).hexdigest(),
            status="pending",
            tagged_text=tagged_text,
            content_digest=hashlib.sha256(tagged_text.encode()).hexdigest(),
            preference_version=preference.version,
            snip_prompt_version="remember-tool-v1",
            created_at=now,
        )
    )
    await session.flush()
    policy_version_id = await session.scalar(sa.select(SystemRuntimePolicyRow.current_version_id).where(SystemRuntimePolicyRow.section == "memory_document"))
    assert isinstance(policy_version_id, uuid.UUID)
    return (
        MemoryDreamFrozenRuntime(
            preference_version=preference.version,
            policy_revision=1,
            model_execution=frozen_system_model_execution(
                model_id=model_id,
                provider_model="memory-prepare-test",
            ),
            prompt_version=DREAM_PROMPT_VERSION,
        ),
        policy_version_id,
    )


async def _settled_prepare_with_queued_child(
    seed: PrivateThreadSeed,
    *,
    now: datetime,
    label: str,
):
    async with seed.factory() as session, session.begin():
        thread_id = await _add_thread(session, seed, label)
        frozen, policy_version_id = await _frozen_dream(
            session,
            seed,
            now=now,
        )
    async with seed.factory() as session, session.begin():
        admission = await _admit(
            session,
            seed,
            thread_id=thread_id,
            operation_id=uuid.uuid4(),
            now=now,
        )
    claim = await _claim_prepare(seed, now=now + timedelta(seconds=1))
    async with seed.factory() as session, session.begin():
        await _lock_scope(session, seed)
        thread = await session.scalar(
            sa.select(ThreadMetaRow)
            .where(
                ThreadMetaRow.project_id == _scope(seed).project_id,
                ThreadMetaRow.owner_user_id == _scope(seed).owner_user_id,
                ThreadMetaRow.thread_id == thread_id,
            )
            .with_for_update(of=ThreadMetaRow)
        )
        assert thread is not None
        repository = MemoryDreamPrepareRepository(
            session,
            jobs=_jobs(session),
        )
        await repository.set_phase(
            _scope(seed),
            job_id=claim.job_id,
            lease_token=claim.lease_token,
            phase="verifying",
            now=now + timedelta(seconds=2),
        )
        child = await MemoryDocumentRepository(
            session,
            jobs=_jobs(session),
        ).admit_dream(
            _scope(seed),
            trigger="manual_dream",
            frozen=frozen,
            initial_content=EMPTY_MEMORY_DOCUMENT,
            initial_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
            sections_policy_version_id=policy_version_id,
            now=now + timedelta(seconds=2),
        )
        assert child.disposition == "queued" and child.job_id is not None
        await repository.link_dream(
            _scope(seed),
            job_id=claim.job_id,
            lease_token=claim.lease_token,
            admitted=child,
            now=now + timedelta(seconds=2),
        )
        await repository.settle_success(
            _scope(seed),
            job_id=claim.job_id,
            lease_token=claim.lease_token,
            now=now + timedelta(seconds=2),
        )
    return admission, child


@pytest.mark.postgres
@pytest.mark.anyio
async def test_prepare_operation_conflict_and_immediate_cancel_release_thread(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    now = datetime.now(UTC)
    operation_id = uuid.uuid4()
    try:
        async with seed.factory() as session, session.begin():
            first_thread = await _add_thread(session, seed, "first")
            second_thread = await _add_thread(session, seed, "second")
        async with seed.factory() as session, session.begin():
            first = await _admit(
                session,
                seed,
                thread_id=first_thread,
                operation_id=operation_id,
                now=now,
            )
        async with seed.factory() as session, session.begin():
            concurrent = await _admit(
                session,
                seed,
                thread_id=first_thread,
                operation_id=uuid.uuid4(),
                now=now + timedelta(milliseconds=100),
            )
            assert concurrent.disposition == "already_running"
            assert concurrent.record.job_id == first.record.job_id
        async with seed.factory() as session:
            latest = await MemoryDreamPrepareRepository(session).read_latest(
                _scope(seed),
                thread_id=first_thread,
            )
            assert latest.job_id == first.record.job_id
        async with seed.factory() as session, session.begin():
            with pytest.raises(MemoryDreamPrepareConflict):
                await _admit(
                    session,
                    seed,
                    thread_id=second_thread,
                    operation_id=operation_id,
                    now=now + timedelta(seconds=1),
                )
        async with seed.factory() as session, session.begin():
            await _lock_scope(session, seed)
            cancelled = await MemoryDreamPrepareRepository(
                session,
                jobs=_jobs(session),
            ).request_cancel(
                _scope(seed),
                job_id=first.record.job_id,
                reason="user_cancelled",
                now=now + timedelta(seconds=2),
            )
            assert cancelled.phase == "cancelled"
            assert cancelled.job_status == "cancelled"
        async with seed.factory() as session, session.begin():
            replacement = await _admit(
                session,
                seed,
                thread_id=first_thread,
                operation_id=uuid.uuid4(),
                now=now + timedelta(seconds=3),
            )
            assert replacement.record.job_id != first.record.job_id
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_prepare_terminal_failure_releases_active_thread(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    now = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            thread_id = await _add_thread(session, seed, "dead")
        async with seed.factory() as session, session.begin():
            admission = await _admit(
                session,
                seed,
                thread_id=thread_id,
                operation_id=uuid.uuid4(),
                now=now,
                max_attempts=2,
            )
        claim = await _claim_prepare(seed, now=now + timedelta(seconds=1))
        assert claim.job_id == admission.record.job_id
        async with seed.factory() as session, session.begin():
            await _lock_scope(session, seed)
            await MemoryDreamPrepareRepository(
                session,
                jobs=_jobs(session),
            ).retry_or_dead(
                _scope(seed),
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                public_error_code="MEMORY_DREAM_PREPARE_TEST_FAILED",
                retry_initial_seconds=5,
                retry_max_seconds=5,
                now=now + timedelta(seconds=2),
            )
        async with seed.factory() as session:
            retry_job = await session.get(JobRow, claim.job_id)
            retry_row = await session.get(MemoryDreamPrepareRunRow, claim.job_id)
            assert retry_job is not None and retry_job.status == "retry_wait"
            assert retry_row is not None and retry_row.completed_at is None
        claim = await _claim_prepare(seed, now=now + timedelta(seconds=8))
        async with seed.factory() as session, session.begin():
            await _lock_scope(session, seed)
            await MemoryDreamPrepareRepository(
                session,
                jobs=_jobs(session),
            ).retry_or_dead(
                _scope(seed),
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                public_error_code="MEMORY_DREAM_PREPARE_TEST_FAILED",
                retry_initial_seconds=5,
                retry_max_seconds=5,
                now=now + timedelta(seconds=9),
            )
        async with seed.factory() as session:
            job = await session.get(JobRow, claim.job_id)
            row = await session.get(MemoryDreamPrepareRunRow, claim.job_id)
            assert job is not None and job.status == "dead"
            assert row is not None and row.phase == "failed"
            assert row.completed_at is not None
        async with seed.factory() as session, session.begin():
            replacement = await _admit(
                session,
                seed,
                thread_id=thread_id,
                operation_id=uuid.uuid4(),
                now=now + timedelta(seconds=3),
            )
            assert replacement.record.job_status == "queued"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_prepare_progress_is_monotonic_and_duplicate_checkpoint_is_idempotent(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    now = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            thread_id = await _add_thread(session, seed, "progress")
        async with seed.factory() as session, session.begin():
            admission = await _admit(
                session,
                seed,
                thread_id=thread_id,
                operation_id=uuid.uuid4(),
                now=now,
            )
        claim = await _claim_prepare(seed, now=now + timedelta(seconds=1))
        async with seed.factory() as session, session.begin():
            repository = MemoryDreamPrepareRepository(
                session,
                jobs=_jobs(session),
            )
            await repository.record_pass(
                _scope(seed),
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                checkpoint_id="checkpoint-1",
                now=now + timedelta(seconds=2),
            )
            await repository.record_pass(
                _scope(seed),
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                checkpoint_id="checkpoint-1",
                now=now + timedelta(seconds=3),
            )
            await repository.record_pass(
                _scope(seed),
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                checkpoint_id="checkpoint-2",
                now=now + timedelta(seconds=4),
            )
            with pytest.raises(MemoryDreamPrepareConflict):
                await repository.record_pass(
                    _scope(seed),
                    job_id=claim.job_id,
                    lease_token="lost-lease",
                    checkpoint_id="checkpoint-3",
                    now=now + timedelta(seconds=5),
                )
        async with seed.factory() as session:
            row = await session.get(
                MemoryDreamPrepareRunRow,
                admission.record.job_id,
            )
            assert row is not None
            assert row.compacted_passes == 2
            assert row.last_checkpoint_id == "checkpoint-2"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_prepare_nothing_pending_settles_parent_successfully(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    now = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            thread_id = await _add_thread(session, seed, "nothing")
        async with seed.factory() as session, session.begin():
            admission = await _admit(
                session,
                seed,
                thread_id=thread_id,
                operation_id=uuid.uuid4(),
                now=now,
            )
        claim = await _claim_prepare(seed, now=now + timedelta(seconds=1))
        async with seed.factory() as session, session.begin():
            await _lock_scope(session, seed)
            await session.execute(
                sa.select(ThreadMetaRow.thread_id)
                .where(
                    ThreadMetaRow.project_id == _scope(seed).project_id,
                    ThreadMetaRow.owner_user_id == _scope(seed).owner_user_id,
                    ThreadMetaRow.thread_id == thread_id,
                )
                .with_for_update(of=ThreadMetaRow)
            )
            repository = MemoryDreamPrepareRepository(
                session,
                jobs=_jobs(session),
            )
            await repository.link_dream(
                _scope(seed),
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                admitted=MemoryDreamAdmissionRecord(
                    disposition="nothing_pending",
                    job_id=None,
                    history_count=0,
                ),
                now=now + timedelta(seconds=2),
            )
            await repository.settle_success(
                _scope(seed),
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                now=now + timedelta(seconds=2),
            )
        async with seed.factory() as session:
            row = await session.get(
                MemoryDreamPrepareRunRow,
                admission.record.job_id,
            )
            assert row is not None and row.phase == "succeeded"
            assert row.result_disposition == "nothing_pending"
            assert row.dream_job_id is None and row.history_count == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_prepare_queued_child_settles_parent_successfully(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    now = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            thread_id = await _add_thread(session, seed, "child")
            frozen, policy_version_id = await _frozen_dream(
                session,
                seed,
                now=now,
            )
        async with seed.factory() as session, session.begin():
            admission = await _admit(
                session,
                seed,
                thread_id=thread_id,
                operation_id=uuid.uuid4(),
                now=now,
            )
        claim = await _claim_prepare(seed, now=now + timedelta(seconds=1))
        assert claim.job_id == admission.record.job_id
        async with seed.factory() as session, session.begin():
            await _lock_scope(session, seed)
            thread = await session.scalar(
                sa.select(ThreadMetaRow)
                .where(
                    ThreadMetaRow.project_id == _scope(seed).project_id,
                    ThreadMetaRow.owner_user_id == _scope(seed).owner_user_id,
                    ThreadMetaRow.thread_id == thread_id,
                )
                .with_for_update(of=ThreadMetaRow)
            )
            assert thread is not None
            repository = MemoryDreamPrepareRepository(
                session,
                jobs=_jobs(session),
            )
            await repository.set_phase(
                _scope(seed),
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                phase="verifying",
                now=now + timedelta(seconds=2),
            )
            child = await MemoryDocumentRepository(
                session,
                jobs=_jobs(session),
            ).admit_dream(
                _scope(seed),
                trigger="manual_dream",
                frozen=frozen,
                initial_content=EMPTY_MEMORY_DOCUMENT,
                initial_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
                sections_policy_version_id=policy_version_id,
                now=now + timedelta(seconds=2),
            )
            assert child.disposition == "queued" and child.job_id is not None
            await repository.link_dream(
                _scope(seed),
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                admitted=child,
                now=now + timedelta(seconds=2),
            )
            await repository.settle_success(
                _scope(seed),
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                now=now + timedelta(seconds=2),
            )
        async with seed.factory() as session:
            parent_job = await session.get(JobRow, claim.job_id)
            parent = await session.get(MemoryDreamPrepareRunRow, claim.job_id)
            child_job = await session.get(JobRow, child.job_id)
            assert parent_job is not None and parent_job.status == "succeeded"
            assert parent is not None and parent.phase == "succeeded"
            assert parent.result_disposition == "queued"
            assert parent.dream_job_id == child.job_id
            assert child_job is not None and child_job.status == "queued"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize("child_initial_status", ["queued", "retry_wait"])
async def test_prepare_cancel_terminal_parent_atomically_releases_queued_child(
    migrated_postgres_database_url: str,
    child_initial_status: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    now = datetime.now(UTC)

    class _Audit:
        def __init__(self) -> None:
            self.settled: list[dict[str, object]] = []

        async def memory_dream_settled(self, _session, **kwargs) -> bool:
            self.settled.append(kwargs)
            return True

    audit = _Audit()
    try:
        admission, child = await _settled_prepare_with_queued_child(
            seed,
            now=now,
            label=f"cancel-{child_initial_status}",
        )
        if child_initial_status == "retry_wait":
            child_claim = await _claim_dream(
                seed,
                now=now + timedelta(seconds=3),
            )
            assert child_claim.job_id == child.job_id
            async with seed.factory() as session, session.begin():
                await _lock_scope(session, seed)
                release = await MemoryDocumentRepository(
                    session,
                    jobs=_jobs(session),
                ).release_dream(
                    _scope(seed),
                    job_id=child_claim.job_id,
                    lease_token=child_claim.lease_token,
                    now=now + timedelta(seconds=4),
                    cancelled=False,
                    public_error_code="MEMORY_DREAM_PREPARE_RETRY_TEST",
                    retry_initial_seconds=60,
                    retry_max_seconds=60,
                )
                assert release.disposition == "retry_wait"

        async with seed.factory() as session:
            child_job = await session.get(JobRow, child.job_id)
            assert child_job is not None
            assert child_job.status == child_initial_status

        result = await MemoryDreamPrepareService(
            seed.factory,
            job_repository_builder=_jobs,
            audit=audit,
        ).cancel(seed.owner_a, admission.record.job_id)
        assert result.job_status == "succeeded"
        repeated = await MemoryDreamPrepareService(
            seed.factory,
            job_repository_builder=_jobs,
            audit=audit,
        ).cancel(seed.owner_a, admission.record.job_id)
        assert repeated.job_status == "succeeded"

        async with seed.factory() as session:
            child_job = await session.get(JobRow, child.job_id)
            document = await session.get(
                MemoryDocumentRow,
                (
                    _scope(seed).project_id,
                    _scope(seed).owner_user_id,
                    _scope(seed).namespace,
                ),
            )
            child_run = await session.get(MemoryDreamRunRow, child.job_id)
            child_history = tuple((await session.execute(sa.select(MemoryHistoryEntryRow).where(MemoryHistoryEntryRow.dream_job_id == child.job_id))).scalars())
            assert child_job is not None and child_job.status == "cancelled"
            assert document is not None and document.active_dream_job_id is None
            assert child_run is not None and child_run.completed_at is None
            assert child_history == ()
            pending_count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(MemoryHistoryEntryRow)
                .where(
                    MemoryHistoryEntryRow.project_id == _scope(seed).project_id,
                    MemoryHistoryEntryRow.owner_user_id == _scope(seed).owner_user_id,
                    MemoryHistoryEntryRow.namespace == _scope(seed).namespace,
                    MemoryHistoryEntryRow.status == "pending",
                    MemoryHistoryEntryRow.dream_job_id.is_(None),
                )
            )
            assert pending_count == 1
        assert len(audit.settled) == 1
        assert audit.settled[0]["job_id"] == child.job_id
        assert audit.settled[0]["disposition"] == "cancelled"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize("child_initial_status", ["leased", "running"])
async def test_prepare_cancel_terminal_parent_requests_running_child_cooperatively(
    migrated_postgres_database_url: str,
    child_initial_status: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    now = datetime.now(UTC)

    class _Audit:
        def __init__(self) -> None:
            self.settled: list[dict[str, object]] = []

        async def memory_dream_settled(self, _session, **kwargs) -> bool:
            self.settled.append(kwargs)
            return True

    audit = _Audit()
    try:
        admission, child = await _settled_prepare_with_queued_child(
            seed,
            now=now,
            label="cancel-running",
        )
        child_claim = await _claim_dream(
            seed,
            now=now + timedelta(seconds=3),
            mark_running=child_initial_status == "running",
        )
        assert child_claim.job_id == child.job_id

        result = await MemoryDreamPrepareService(
            seed.factory,
            job_repository_builder=_jobs,
            audit=audit,
        ).cancel(seed.owner_a, admission.record.job_id)
        assert result.job_status == "succeeded"

        async with seed.factory() as session:
            child_job = await session.get(JobRow, child.job_id)
            document = await session.get(
                MemoryDocumentRow,
                (
                    _scope(seed).project_id,
                    _scope(seed).owner_user_id,
                    _scope(seed).namespace,
                ),
            )
            processing_count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(MemoryHistoryEntryRow)
                .where(
                    MemoryHistoryEntryRow.dream_job_id == child.job_id,
                    MemoryHistoryEntryRow.status == "processing",
                )
            )
            assert child_job is not None
            assert child_job.status == child_initial_status
            assert child_job.cancel_requested_at is not None
            assert child_job.cancel_reason == "dream_prepare_cancelled"
            assert document is not None
            assert document.active_dream_job_id == child.job_id
            assert processing_count == 1
        assert audit.settled == []
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_prepare_cancel_child_audit_failure_rolls_back_release(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    now = datetime.now(UTC)

    class _FailingAudit:
        async def memory_dream_settled(self, _session, **_kwargs) -> None:
            raise RuntimeError("forced audit failure")

    try:
        admission, child = await _settled_prepare_with_queued_child(
            seed,
            now=now,
            label="cancel-audit-rollback",
        )
        with pytest.raises(PrivateWorkUnavailable):
            await MemoryDreamPrepareService(
                seed.factory,
                job_repository_builder=_jobs,
                audit=_FailingAudit(),
            ).cancel(seed.owner_a, admission.record.job_id)

        async with seed.factory() as session:
            child_job = await session.get(JobRow, child.job_id)
            document = await session.get(
                MemoryDocumentRow,
                (
                    _scope(seed).project_id,
                    _scope(seed).owner_user_id,
                    _scope(seed).namespace,
                ),
            )
            processing_count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(MemoryHistoryEntryRow)
                .where(
                    MemoryHistoryEntryRow.dream_job_id == child.job_id,
                    MemoryHistoryEntryRow.status == "processing",
                )
            )
            assert child_job is not None and child_job.status == "queued"
            assert document is not None
            assert document.active_dream_job_id == child.job_id
            assert processing_count == 1
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_prepare_final_and_admission_share_thread_then_row_lock_order(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    now = datetime.now(UTC)
    final_holds_thread = asyncio.Event()
    release_final = asyncio.Event()
    backend_pids: dict[str, int] = {}
    final_task = None
    admission_task = None
    try:
        async with seed.factory() as session, session.begin():
            thread_id = await _add_thread(session, seed, "interleave")
        async with seed.factory() as session, session.begin():
            initial = await _admit(
                session,
                seed,
                thread_id=thread_id,
                operation_id=uuid.uuid4(),
                now=now,
            )
        claim = await _claim_prepare(seed, now=now + timedelta(seconds=1))
        assert claim.job_id == initial.record.job_id

        async def final_phase() -> None:
            async with seed.factory() as session, session.begin():
                backend_pids["final"] = int(await session.scalar(text("SELECT pg_backend_pid()")))
                await session.execute(
                    sa.select(ThreadMetaRow.thread_id)
                    .where(
                        ThreadMetaRow.project_id == _scope(seed).project_id,
                        ThreadMetaRow.owner_user_id == _scope(seed).owner_user_id,
                        ThreadMetaRow.thread_id == thread_id,
                    )
                    .with_for_update(of=ThreadMetaRow)
                )
                final_holds_thread.set()
                await release_final.wait()
                await MemoryDreamPrepareRepository(
                    session,
                    jobs=_jobs(session),
                ).set_phase(
                    _scope(seed),
                    job_id=claim.job_id,
                    lease_token=claim.lease_token,
                    phase="verifying",
                    now=now + timedelta(seconds=2),
                )

        async def competing_admission():
            async with seed.factory() as session, session.begin():
                backend_pids["admission"] = int(await session.scalar(text("SELECT pg_backend_pid()")))
                return await MemoryDreamPrepareRepository(
                    session,
                    jobs=_jobs(session),
                ).admit(
                    _scope(seed),
                    thread_id=thread_id,
                    operation_id=uuid.uuid4(),
                    request_id="memory-dream-prepare-interleave",
                    now=now + timedelta(seconds=2),
                )

        final_task = asyncio.create_task(final_phase())
        await asyncio.wait_for(final_holds_thread.wait(), timeout=2)
        admission_task = asyncio.create_task(competing_admission())
        while "admission" not in backend_pids:
            await asyncio.sleep(0)
        await asyncio.wait_for(
            _wait_for_lock(seed, backend_pids["admission"]),
            timeout=4,
        )
        release_final.set()
        _, raced = await asyncio.wait_for(
            asyncio.gather(final_task, admission_task),
            timeout=5,
        )
        assert raced.disposition == "already_running"
        assert raced.record.job_id == claim.job_id
        assert backend_pids["final"] != backend_pids["admission"]
    finally:
        release_final.set()
        for task in (final_task, admission_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (final_task, admission_task) if task is not None),
            return_exceptions=True,
        )
        await seed.engine.dispose()
