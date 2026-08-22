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

from app.personalization.repository import (
    AccountMemoryResetCounts,
    AccountPersonalizationRepository,
)
from app.personalization.service import (
    AccountPersonalizationService,
    AccountPersonalizationUnavailable,
)
from app.projects.context import resolve_project_context_in_transaction
from app.worker import memory_dream as memory_dream_worker_module
from app.worker.memory_dream import MemoryDreamJobHandler
from app.worker.memory_dream_prepare import MemoryDreamPrepareJobHandler
from deerflow.agents.memory.dream import (
    DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
    DREAM_PROMPT_VERSION,
    EMPTY_MEMORY_DOCUMENT,
)
from deerflow.persistence.jobs.model import JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import (
    EnqueueJob,
    JobClaim,
    JobOwnerRef,
    JobRepository,
    JobScope,
)
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    MemoryDreamPrepareRunRow,
    MemoryDreamRunRow,
    MemoryEpisodeRow,
    MemoryHistoryEntryRow,
    RunMemoryContextSnapshotRow,
)
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryDreamAdmissionRecord,
    MemoryDreamFrozenRuntime,
)
from deerflow.persistence.private_work.memory_dream_prepare_repository import (
    MemoryDreamPrepareRepository,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.shared_assets.agent_model import AgentRow
from deerflow.persistence.system_runtime_settings import SystemRuntimePolicyRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow


def _owner_ref(_owner_user_id: str) -> JobOwnerRef:
    return JobOwnerRef(key_id="memory-reset-test", hmac_hex="f" * 64)


def _jobs(session) -> JobRepository:
    return JobRepository(session, owner_ref_hasher=_owner_ref)


def _scope(seed: PrivateThreadSeed) -> MemoryDocumentScope:
    resource = seed.owner_a.resource_scope
    return MemoryDocumentScope(
        project_id=uuid.UUID(resource.project_id),
        owner_user_id=resource.owner_user_id,
    )


async def _memory_document_policy_version_id(session) -> uuid.UUID:
    version_id = await session.scalar(
        sa.select(SystemRuntimePolicyRow.current_version_id).where(
            SystemRuntimePolicyRow.section == "memory_document",
        )
    )
    assert isinstance(version_id, uuid.UUID)
    return version_id


async def _add_owner_project(
    session,
    *,
    owner_user_id: str,
    label: str,
) -> uuid.UUID:
    project_id = uuid.uuid4()
    session.add(
        ProjectRow(
            id=project_id,
            slug=f"memory-reset-{label}-{project_id.hex[:8]}",
            display_name=f"Memory reset {label}",
            created_by_user_id=owner_user_id,
        )
    )
    await session.flush()
    session.add(
        ProjectMembershipRow(
            id=uuid.uuid4(),
            project_id=project_id,
            user_id=owner_user_id,
            role="admin",
            status="active",
            version=1,
        )
    )
    await session.flush()
    return project_id


async def _add_thread(
    session,
    *,
    project_id: uuid.UUID,
    owner_user_id: str,
    label: str,
) -> str:
    agent = AgentRow(
        id=uuid.uuid4(),
        scope="project",
        project_id=project_id,
        slug=f"memory-reset-{label}-{uuid.uuid4().hex[:8]}",
        display_name=f"Memory reset {label}",
        created_by_user_id=owner_user_id,
    )
    session.add(agent)
    await session.flush()
    thread_id = f"memory-reset-{label[:16]}-{uuid.uuid4().hex[:16]}"
    session.add(
        ThreadMetaRow(
            thread_id=thread_id,
            assistant_id=str(agent.id),
            owner_user_id=owner_user_id,
            display_name=f"Memory reset {label}",
            status="idle",
            metadata_json={},
            project_id=project_id,
            agent_asset_id=agent.id,
            agent_scope="project",
        )
    )
    await session.flush()
    return thread_id


async def _admit_prepare(
    session,
    scope: MemoryDocumentScope,
    *,
    thread_id: str,
    now: datetime,
    max_attempts: int = 3,
) -> uuid.UUID:
    admission = await MemoryDreamPrepareRepository(
        session,
        jobs=_jobs(session),
    ).admit(
        scope,
        thread_id=thread_id,
        operation_id=uuid.uuid4(),
        request_id="memory-reset-postgres-test",
        now=now,
        max_attempts=max_attempts,
    )
    assert admission.disposition == "queued"
    assert admission.record.job_status == "queued"
    return admission.record.job_id


async def _add_snapshot_only_scope(
    session,
    seed: PrivateThreadSeed,
    *,
    namespace: str,
    now: datetime,
) -> None:
    scope = _scope(seed)
    thread_id = f"memory-reset-{uuid.uuid4()}"
    run_id = f"memory-reset-{uuid.uuid4()}"
    session.add(
        ThreadMetaRow(
            thread_id=thread_id,
            assistant_id=str(seed.project_agent_id),
            owner_user_id=scope.owner_user_id,
            display_name="Memory reset snapshot",
            status="idle",
            metadata_json={},
            project_id=scope.project_id,
            agent_asset_id=seed.project_agent_id,
            agent_scope="project",
        )
    )
    await session.flush()
    session.add(
        RunRow(
            run_id=run_id,
            thread_id=thread_id,
            assistant_id=str(seed.project_agent_id),
            owner_user_id=scope.owner_user_id,
            status="pending",
            model_name="memory-reset-test",
            multitask_strategy="reject",
            metadata_json={},
            kwargs_json={},
            origin_trace_id="a" * 32,
            project_id=scope.project_id,
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    session.add(
        RunMemoryContextSnapshotRow(
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            run_id=run_id,
            namespace=namespace,
            document_version=1,
            content=EMPTY_MEMORY_DOCUMENT,
            content_digest=hashlib.sha256(
                EMPTY_MEMORY_DOCUMENT.encode(),
            ).hexdigest(),
            sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
            created_at=now,
        )
    )


async def _add_test_model(
    session,
    *,
    owner_user_id: str,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    model_id = uuid.uuid4()
    checksum = hashlib.sha256(model_id.bytes).hexdigest()
    await seed_system_model_config(
        session,
        model_id=model_id,
        owner_user_id=owner_user_id,
        display_name="Memory reset PostgreSQL model",
        provider_model="memory-reset-test",
    )
    return model_id, model_id, checksum


def _pending_tool_history(
    scope: MemoryDocumentScope,
    *,
    preference_version: int,
    now: datetime,
) -> MemoryHistoryEntryRow:
    tagged_text = "- [durable] Memory reset PostgreSQL lock-order evidence."
    source_id = str(uuid.uuid4())
    return MemoryHistoryEntryRow(
        id=uuid.uuid4(),
        project_id=scope.project_id,
        owner_user_id=scope.owner_user_id,
        namespace=scope.namespace,
        thread_id=f"memory-reset-{uuid.uuid4()}",
        origin="tool",
        source_run_id=source_id,
        source_checkpoint_id=None,
        committed_checkpoint_id=None,
        source_digest=hashlib.sha256(source_id.encode()).hexdigest(),
        status="pending",
        tagged_text=tagged_text,
        content_digest=hashlib.sha256(tagged_text.encode()).hexdigest(),
        preference_version=preference_version,
        snip_prompt_version="remember-tool-v1",
        created_at=now,
    )


async def _prepare_dream_input(
    seed: PrivateThreadSeed,
    scope: MemoryDocumentScope,
    *,
    now: datetime,
) -> tuple[MemoryDreamFrozenRuntime, uuid.UUID, int]:
    async with seed.factory() as session, session.begin():
        preference = await AccountPersonalizationRepository(session).read_memory(
            uuid.UUID(scope.owner_user_id),
        )
        model_id, version_id, checksum = await _add_test_model(
            session,
            owner_user_id=scope.owner_user_id,
        )
        session.add(
            _pending_tool_history(
                scope,
                preference_version=preference.version,
                now=now,
            )
        )
        await session.flush()
        policy_version_id = await _memory_document_policy_version_id(session)
    return (
        MemoryDreamFrozenRuntime(
            preference_version=preference.version,
            policy_revision=1,
            model_execution=frozen_system_model_execution(
                model_id=model_id,
                provider_model="memory-reset-test",
            ),
            prompt_version=DREAM_PROMPT_VERSION,
        ),
        policy_version_id,
        preference.version,
    )


async def _admit_prepared_dream(
    session,
    scope: MemoryDocumentScope,
    *,
    frozen: MemoryDreamFrozenRuntime,
    policy_version_id: uuid.UUID,
    now: datetime,
) -> MemoryDreamAdmissionRecord:
    return await MemoryDocumentRepository(
        session,
        jobs=_jobs(session),
    ).admit_dream(
        scope,
        trigger="manual_dream",
        frozen=frozen,
        initial_content=EMPTY_MEMORY_DOCUMENT,
        initial_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
        sections_policy_version_id=policy_version_id,
        now=now,
    )


async def _seed_queued_dream(
    seed: PrivateThreadSeed,
    scope: MemoryDocumentScope,
    *,
    now: datetime,
) -> tuple[uuid.UUID, int]:
    frozen, policy_version_id, preference_version = await _prepare_dream_input(
        seed,
        scope,
        now=now,
    )
    async with seed.factory() as session, session.begin():
        admission = await _admit_prepared_dream(
            session,
            scope,
            frozen=frozen,
            policy_version_id=policy_version_id,
            now=now,
        )
    assert admission.disposition == "queued"
    assert admission.job_id is not None
    return admission.job_id, preference_version


async def _claim_dream(
    seed: PrivateThreadSeed,
    *,
    now: datetime,
) -> JobClaim:
    worker_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="memory-reset-postgres-test",
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
            now=now + timedelta(seconds=1),
        )
        assert claim is not None
        assert await jobs.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
            now=now + timedelta(seconds=1),
        )
        return claim


async def _claim_prepare(
    seed: PrivateThreadSeed,
    *,
    now: datetime,
    mark_running: bool,
) -> JobClaim:
    worker_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="memory-reset-prepare-postgres-test",
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
            now=now + timedelta(seconds=1),
        )
        assert claim is not None
        if mark_running:
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
                now=now + timedelta(seconds=1),
            )
        return claim


async def _wait_until_backend_waits_for_lock(
    seed: PrivateThreadSeed,
    backend_pid: int,
) -> None:
    for _ in range(300):
        async with seed.factory() as monitor:
            wait_event_type = await monitor.scalar(
                text(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid",
                ),
                {"pid": backend_pid},
            )
        if wait_event_type == "Lock":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Memory reset did not wait for the authority lock")


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_reset_projects_episode_snapshot_and_active_jobs_only(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = _scope(seed)
    now = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            episode_project_id = await _add_owner_project(
                session,
                owner_user_id=scope.owner_user_id,
                label="episode",
            )
            active_job_project_id = await _add_owner_project(
                session,
                owner_user_id=scope.owner_user_id,
                label="active-job",
            )
            prepare_project_id = await _add_owner_project(
                session,
                owner_user_id=scope.owner_user_id,
                label="prepare",
            )
            terminal_job_project_id = await _add_owner_project(
                session,
                owner_user_id=scope.owner_user_id,
                label="terminal-job",
            )
            empty_project_id = await _add_owner_project(
                session,
                owner_user_id=scope.owner_user_id,
                label="empty",
            )
            await _add_snapshot_only_scope(
                session,
                seed,
                namespace="snapshot-only",
                now=now,
            )
            prepare_thread_id = await _add_thread(
                session,
                project_id=prepare_project_id,
                owner_user_id=scope.owner_user_id,
                label="prepare-only",
            )
            prepare_job_id = await _admit_prepare(
                session,
                MemoryDocumentScope(
                    project_id=prepare_project_id,
                    owner_user_id=scope.owner_user_id,
                ),
                thread_id=prepare_thread_id,
                now=now,
            )
            episode_text = "- [durable] episode-only project"
            session.add(
                MemoryEpisodeRow(
                    id=uuid.uuid4(),
                    project_id=episode_project_id,
                    owner_user_id=scope.owner_user_id,
                    namespace="episode-only",
                    thread_id="episode-only-thread",
                    origin="tool",
                    tagged_text=episode_text,
                    content_digest=hashlib.sha256(
                        episode_text.encode(),
                    ).hexdigest(),
                    occurred_at=now,
                    consumed_dream_job_id=uuid.uuid4(),
                    created_at=now,
                )
            )
            jobs = _jobs(session)
            active_job_id = await jobs.enqueue(
                EnqueueJob(
                    job_type="memory_seal",
                    scope=JobScope(active_job_project_id, scope.owner_user_id),
                    namespace="active-job-only",
                    idempotency_key=hashlib.sha256(b"active-job-only").hexdigest(),
                    run_id=None,
                    occurrence_id=None,
                    max_attempts=3,
                    retry_safety="safe",
                )
            )
            terminal_job_id = await jobs.enqueue(
                EnqueueJob(
                    job_type="memory_seal",
                    scope=JobScope(terminal_job_project_id, scope.owner_user_id),
                    namespace="terminal-job-only",
                    idempotency_key=hashlib.sha256(
                        b"terminal-job-only",
                    ).hexdigest(),
                    run_id=None,
                    occurrence_id=None,
                    max_attempts=3,
                    retry_safety="safe",
                )
            )
            terminal_scope = JobScope(
                terminal_job_project_id,
                scope.owner_user_id,
            )
            assert await jobs.request_cancel(
                terminal_scope,
                terminal_job_id,
                reason="test_terminal",
                now=now,
            )
            assert await jobs.settle_requested_cancel(
                terminal_scope,
                terminal_job_id,
                now=now,
            )

        async with seed.factory() as session, session.begin():
            repository = AccountPersonalizationRepository(session)
            preference = await repository.read_memory(uuid.UUID(scope.owner_user_id))
            result = await repository.reset_memory(
                uuid.UUID(scope.owner_user_id),
                expected_version=preference.version,
                now=now + timedelta(seconds=1),
            )

        expected_projects = tuple(
            sorted(
                (
                    scope.project_id,
                    episode_project_id,
                    active_job_project_id,
                    prepare_project_id,
                ),
                key=str,
            )
        )
        assert result.affected_project_ids == expected_projects
        assert terminal_job_project_id not in result.affected_project_ids
        assert empty_project_id not in result.affected_project_ids
        assert result.scopes_reset == 4
        # A durable prepare is Memory state in its own right. Its deletion must
        # be visible in the internal aggregate used by reset audit metadata.
        assert result.prepare_runs == 1
        assert result.snapshots == 1
        assert result.episodes == 1
        assert result.jobs_cancelled == 2
        assert result.settled_dreams == ()

        async with seed.factory() as session:
            remaining_snapshots = await session.scalar(
                sa.select(sa.func.count()).select_from(
                    RunMemoryContextSnapshotRow,
                )
            )
            remaining_episodes = await session.scalar(sa.select(sa.func.count()).select_from(MemoryEpisodeRow))
            active_job = await session.get(JobRow, active_job_id)
            prepare_job = await session.get(JobRow, prepare_job_id)
            prepare_rows = await session.scalar(
                sa.select(sa.func.count()).select_from(
                    MemoryDreamPrepareRunRow,
                )
            )
            terminal_job = await session.get(JobRow, terminal_job_id)
        assert remaining_snapshots == 0
        assert remaining_episodes == 0
        assert active_job is not None
        assert active_job.status == "cancelled"
        assert active_job.cancel_reason == "memory_reset"
        assert active_job.completed_at is not None
        assert prepare_job is not None
        assert prepare_job.status == "cancelled"
        assert prepare_job.cancel_reason == "memory_reset"
        assert prepare_job.completed_at is not None
        assert prepare_rows == 0
        assert terminal_job is not None
        assert terminal_job.status == "cancelled"
        assert terminal_job.cancel_reason == "test_terminal"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_reset_deletes_all_active_prepare_rows_and_preserves_owned_job_settlement(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = _scope(seed)
    now = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            retry_thread = await _add_thread(
                session,
                project_id=scope.project_id,
                owner_user_id=scope.owner_user_id,
                label="retry-prepare",
            )
            retry_job_id = await _admit_prepare(
                session,
                scope,
                thread_id=retry_thread,
                now=now,
            )
        retry_claim = await _claim_prepare(
            seed,
            now=now,
            mark_running=True,
        )
        assert retry_claim.job_id == retry_job_id
        async with seed.factory() as session, session.begin():
            await MemoryDreamPrepareRepository(
                session,
                jobs=_jobs(session),
            ).retry_or_dead(
                scope,
                job_id=retry_job_id,
                lease_token=retry_claim.lease_token,
                public_error_code="MEMORY_DREAM_PREPARE_TEST_RETRY",
                retry_initial_seconds=60,
                retry_max_seconds=60,
                now=now + timedelta(seconds=2),
            )

        async with seed.factory() as session, session.begin():
            leased_thread = await _add_thread(
                session,
                project_id=scope.project_id,
                owner_user_id=scope.owner_user_id,
                label="leased-prepare",
            )
            leased_job_id = await _admit_prepare(
                session,
                scope,
                thread_id=leased_thread,
                now=now,
            )
        leased_claim = await _claim_prepare(
            seed,
            now=now,
            mark_running=False,
        )
        assert leased_claim.job_id == leased_job_id

        async with seed.factory() as session, session.begin():
            running_thread = await _add_thread(
                session,
                project_id=scope.project_id,
                owner_user_id=scope.owner_user_id,
                label="running-prepare",
            )
            running_job_id = await _admit_prepare(
                session,
                scope,
                thread_id=running_thread,
                now=now,
            )
        running_claim = await _claim_prepare(
            seed,
            now=now,
            mark_running=True,
        )
        assert running_claim.job_id == running_job_id

        async with seed.factory() as session, session.begin():
            queued_thread = await _add_thread(
                session,
                project_id=scope.project_id,
                owner_user_id=scope.owner_user_id,
                label="queued-prepare",
            )
            queued_job_id = await _admit_prepare(
                session,
                scope,
                thread_id=queued_thread,
                now=now,
            )
            preference = await AccountPersonalizationRepository(
                session,
            ).read_memory(uuid.UUID(scope.owner_user_id))

        async with seed.factory() as session, session.begin():
            result = await AccountPersonalizationRepository(session).reset_memory(
                uuid.UUID(scope.owner_user_id),
                expected_version=preference.version,
                now=now + timedelta(seconds=3),
            )

        assert result.affected_project_ids == (scope.project_id,)
        assert result.scopes_reset == 1
        assert result.prepare_runs == 4
        assert result.jobs_cancelled == 4
        async with seed.factory() as session:
            jobs = {
                row.id: row
                for row in (
                    await session.execute(
                        sa.select(JobRow).where(
                            JobRow.id.in_(
                                (
                                    retry_job_id,
                                    leased_job_id,
                                    running_job_id,
                                    queued_job_id,
                                )
                            )
                        )
                    )
                ).scalars()
            }
            prepare_count = await session.scalar(
                sa.select(sa.func.count()).select_from(
                    MemoryDreamPrepareRunRow,
                )
            )
        assert prepare_count == 0
        assert jobs[retry_job_id].status == "cancelled"
        assert jobs[queued_job_id].status == "cancelled"
        assert jobs[leased_job_id].status == "leased"
        assert jobs[running_job_id].status == "running"
        assert all(jobs[job_id].cancel_reason == "memory_reset" and jobs[job_id].cancel_requested_at is not None for job_id in jobs)

        # The reset intentionally removes private prepare state immediately.
        # A Worker that still owns the leased/running Job must be able to use
        # its Job lease as the remaining authority and close it cooperatively.
        async with seed.factory() as session, session.begin():
            repository = _jobs(session)
            assert await repository.settle_cancelled(
                leased_job_id,
                lease_token=leased_claim.lease_token,
                now=now + timedelta(seconds=4),
            )
            assert await repository.settle_cancelled(
                running_job_id,
                lease_token=running_claim.lease_token,
                now=now + timedelta(seconds=4),
            )
        async with seed.factory() as session:
            terminal_statuses = tuple((await session.execute(sa.select(JobRow.status).where(JobRow.id.in_((leased_job_id, running_job_id))).order_by(JobRow.id))).scalars())
        assert terminal_statuses == ("cancelled", "cancelled")
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_reset_immediately_settles_a_queued_dream(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = _scope(seed)
    now = datetime.now(UTC)
    try:
        job_id, preference_version = await _seed_queued_dream(
            seed,
            scope,
            now=now,
        )

        async with seed.factory() as session, session.begin():
            result = await AccountPersonalizationRepository(session).reset_memory(
                uuid.UUID(scope.owner_user_id),
                expected_version=preference_version,
                now=now + timedelta(seconds=1),
            )

        assert result.affected_project_ids == (scope.project_id,)
        assert result.scopes_reset == 1
        assert result.documents == 1
        assert result.history_entries == 1
        assert result.dream_runs == 1
        assert result.jobs_cancelled == 1
        assert len(result.settled_dreams) == 1
        assert result.settled_dreams[0].project_id == scope.project_id
        assert result.settled_dreams[0].job_id == job_id

        async with seed.factory() as session:
            job = await session.get(JobRow, job_id)
            preference = await session.get(UserRow, scope.owner_user_id)
            documents = await session.scalar(sa.select(sa.func.count()).select_from(MemoryDocumentRow))
            dream_runs = await session.scalar(sa.select(sa.func.count()).select_from(MemoryDreamRunRow))
            history = await session.scalar(sa.select(sa.func.count()).select_from(MemoryHistoryEntryRow))
        assert job is not None
        assert job.status == "cancelled"
        assert job.cancel_reason == "memory_reset"
        assert job.completed_at is not None
        assert preference is not None
        assert preference.preferences_version == preference_version + 1
        assert documents == 0
        assert dream_runs == 0
        assert history == 0
    finally:
        await seed.engine.dispose()


class _CapturingResetAudit:
    def __init__(self) -> None:
        self.reset_metadata: dict[str, object] | None = None

    async def memory_dream_settled(self, *_args, **_kwargs) -> None:
        raise AssertionError("A prepare Job is not a settled child Dream")

    async def memory_reset_executed(self, _session, **kwargs) -> None:
        self.reset_metadata = dict(kwargs)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_reset_audit_includes_deleted_prepare_count(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = _scope(seed)
    now = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            thread_id = await _add_thread(
                session,
                project_id=scope.project_id,
                owner_user_id=scope.owner_user_id,
                label="audit-prepare",
            )
            prepare_job_id = await _admit_prepare(
                session,
                scope,
                thread_id=thread_id,
                now=now,
            )
            preference = await AccountPersonalizationRepository(
                session,
            ).read_memory(uuid.UUID(scope.owner_user_id))
        audit = _CapturingResetAudit()

        result = await AccountPersonalizationService(
            seed.factory,
            audit=audit,
        ).reset_memory(
            uuid.UUID(scope.owner_user_id),
            expected_version=preference.version,
            request_id="a" * 32,
        )

        assert result.jobs_cancelled == 1
        assert audit.reset_metadata is not None
        assert audit.reset_metadata["affected_project_ids"] == (scope.project_id,)
        assert audit.reset_metadata["scopes_reset"] == 1
        assert audit.reset_metadata["prepare_runs"] == 1
        assert audit.reset_metadata["jobs_cancelled"] == 1
        async with seed.factory() as session:
            job = await session.get(JobRow, prepare_job_id)
            prepare_count = await session.scalar(
                sa.select(sa.func.count()).select_from(
                    MemoryDreamPrepareRunRow,
                )
            )
        assert job is not None and job.status == "cancelled"
        assert prepare_count == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_reset_and_admission_keep_project_before_user_lock_order(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = _scope(seed)
    now = datetime.now(UTC)
    frozen, policy_version_id, preference_version = await _prepare_dream_input(
        seed,
        scope,
        now=now,
    )
    admission_project_locked = asyncio.Event()
    release_admission = asyncio.Event()
    reset_started = asyncio.Event()
    backend_pids: dict[str, int] = {}
    admission_task: asyncio.Task[MemoryDreamAdmissionRecord] | None = None
    reset_task: asyncio.Task[AccountMemoryResetCounts] | None = None

    async def admit() -> MemoryDreamAdmissionRecord:
        async with seed.factory() as session, session.begin():
            backend_pids["admission"] = int(
                await session.scalar(text("SELECT pg_backend_pid()")),
            )
            await resolve_project_context_in_transaction(
                session,
                uuid.UUID(scope.owner_user_id),
                scope.project_id,
                "memory-reset-admission-lock-test",
                lock=True,
            )
            admission_project_locked.set()
            await release_admission.wait()
            preference = await AccountPersonalizationRepository(session).read_memory(
                uuid.UUID(scope.owner_user_id),
                for_update=True,
            )
            assert preference.version == preference_version
            return await _admit_prepared_dream(
                session,
                scope,
                frozen=frozen,
                policy_version_id=policy_version_id,
                now=now,
            )

    async def reset() -> AccountMemoryResetCounts:
        async with seed.factory() as session, session.begin():
            backend_pids["reset"] = int(
                await session.scalar(text("SELECT pg_backend_pid()")),
            )
            reset_started.set()
            return await AccountPersonalizationRepository(session).reset_memory(
                uuid.UUID(scope.owner_user_id),
                expected_version=preference_version,
                now=now + timedelta(seconds=1),
            )

    try:
        admission_task = asyncio.create_task(admit())
        await asyncio.wait_for(admission_project_locked.wait(), timeout=2)
        reset_task = asyncio.create_task(reset())
        await asyncio.wait_for(reset_started.wait(), timeout=2)
        await asyncio.wait_for(
            _wait_until_backend_waits_for_lock(seed, backend_pids["reset"]),
            timeout=4,
        )

        # If reset regresses to User -> Project, it now owns User while waiting
        # for admission's Project; admission then waits for User and PostgreSQL
        # reports a real deadlock instead of both transactions completing.
        release_admission.set()
        admission, result = await asyncio.wait_for(
            asyncio.gather(admission_task, reset_task),
            timeout=5,
        )

        assert admission.disposition == "queued"
        assert admission.job_id is not None
        assert result.jobs_cancelled == 1
        assert backend_pids["admission"] != backend_pids["reset"]
        async with seed.factory() as session:
            job = await session.get(JobRow, admission.job_id)
        assert job is not None and job.status == "cancelled"
    finally:
        release_admission.set()
        for task in (admission_task, reset_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (admission_task, reset_task) if task is not None),
            return_exceptions=True,
        )
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_reset_and_worker_release_share_authority_lock_order(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = _scope(seed)
    now = datetime.now(UTC)
    worker_project_locked = asyncio.Event()
    release_worker = asyncio.Event()
    reset_started = asyncio.Event()
    backend_pids: dict[str, int] = {}
    worker_task: asyncio.Task[None] | None = None
    reset_task: asyncio.Task[AccountMemoryResetCounts] | None = None
    try:
        job_id, preference_version = await _seed_queued_dream(
            seed,
            scope,
            now=now,
        )
        claim = await _claim_dream(seed, now=now)
        assert claim.job_id == job_id

        async def coordinated_scope_validator(session, current_claim, *, lock):
            allowed = await memory_dream_worker_module._default_scope_validator(
                session,
                current_claim,
                lock=lock,
            )
            if lock:
                backend_pids["worker"] = int(
                    await session.scalar(text("SELECT pg_backend_pid()")),
                )
                worker_project_locked.set()
                await release_worker.wait()
            return allowed

        async def reset() -> AccountMemoryResetCounts:
            async with seed.factory() as session, session.begin():
                backend_pids["reset"] = int(
                    await session.scalar(text("SELECT pg_backend_pid()")),
                )
                reset_started.set()
                return await AccountPersonalizationRepository(
                    session,
                ).reset_memory(
                    uuid.UUID(scope.owner_user_id),
                    expected_version=preference_version,
                    now=now + timedelta(seconds=2),
                )

        handler = MemoryDreamJobHandler(
            seed.factory,
            app_config=None,
            runner_factory=lambda _model: object(),
            job_repository_builder=_jobs,
            scope_validator=coordinated_scope_validator,
        )
        settlement = handler._release_settlement(
            claim,
            cancelled=True,
            retryable=False,
        )
        worker_task = asyncio.create_task(settlement.commit())
        await asyncio.wait_for(worker_project_locked.wait(), timeout=2)
        reset_task = asyncio.create_task(reset())
        await asyncio.wait_for(reset_started.wait(), timeout=2)
        await asyncio.wait_for(
            _wait_until_backend_waits_for_lock(seed, backend_pids["reset"]),
            timeout=4,
        )

        release_worker.set()
        worker_result, reset_result = await asyncio.wait_for(
            asyncio.gather(worker_task, reset_task),
            timeout=5,
        )

        assert worker_result is None
        assert reset_result.affected_project_ids == (scope.project_id,)
        assert reset_result.jobs_cancelled == 0
        assert backend_pids["worker"] != backend_pids["reset"]
        async with seed.factory() as session:
            job = await session.get(JobRow, job_id)
            document = await session.get(
                MemoryDocumentRow,
                (scope.project_id, scope.owner_user_id, scope.namespace),
            )
            dream_run = await session.get(MemoryDreamRunRow, job_id)
            history = await session.scalar(sa.select(sa.func.count()).select_from(MemoryHistoryEntryRow))
        assert job is not None and job.status == "cancelled"
        assert document is None
        assert dream_run is None
        assert history == 0
    finally:
        release_worker.set()
        for task in (worker_task, reset_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (worker_task, reset_task) if task is not None),
            return_exceptions=True,
        )
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_reset_and_prepare_cancel_share_project_first_lock_order(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = _scope(seed)
    now = datetime.now(UTC)
    reset_project_locked = asyncio.Event()
    release_reset = asyncio.Event()
    worker_entered_authority = asyncio.Event()
    backend_pids: dict[str, int] = {}
    reset_task: asyncio.Task[AccountMemoryResetCounts] | None = None
    worker_task: asyncio.Task[None] | None = None

    class CoordinatedPrepareRepository(MemoryDreamPrepareRepository):
        async def settle_cancelled(self, *args, **kwargs) -> None:
            await super().settle_cancelled(*args, **kwargs)

    def repository_builder(session, *, jobs):
        return CoordinatedPrepareRepository(session, jobs=jobs)

    class CoordinatedPrepareHandler(MemoryDreamPrepareJobHandler):
        @staticmethod
        async def _lock_settlement_authority(session, current_scope) -> None:
            backend_pids["worker"] = int(
                await session.scalar(text("SELECT pg_backend_pid()")),
            )
            worker_entered_authority.set()
            await MemoryDreamPrepareJobHandler._lock_settlement_authority(
                session,
                current_scope,
            )

    async def reset(preference_version: int) -> AccountMemoryResetCounts:
        async with seed.factory() as session, session.begin():
            await session.execute(sa.select(ProjectRow.id).where(ProjectRow.id == scope.project_id).with_for_update(of=ProjectRow))
            reset_project_locked.set()
            await release_reset.wait()
            return await AccountPersonalizationRepository(session).reset_memory(
                uuid.UUID(scope.owner_user_id),
                expected_version=preference_version,
                now=now + timedelta(seconds=2),
            )

    try:
        async with seed.factory() as session, session.begin():
            thread_id = await _add_thread(
                session,
                project_id=scope.project_id,
                owner_user_id=scope.owner_user_id,
                label="cancel-lock-order",
            )
            prepare_job_id = await _admit_prepare(
                session,
                scope,
                thread_id=thread_id,
                now=now,
            )
            preference = await AccountPersonalizationRepository(
                session,
            ).read_memory(uuid.UUID(scope.owner_user_id))
        claim = await _claim_prepare(
            seed,
            now=now,
            mark_running=True,
        )
        assert claim.job_id == prepare_job_id

        # Construct only the settlement surface: the test must exercise the
        # real Worker fallback without requiring a model or compaction runner.
        handler = object.__new__(CoordinatedPrepareHandler)
        handler._sessions = seed.factory
        handler._repository_builder = repository_builder
        handler._job_repository_builder = _jobs
        settlement = handler._cancel_settlement(claim)

        reset_task = asyncio.create_task(reset(preference.version))
        await asyncio.wait_for(reset_project_locked.wait(), timeout=2)
        worker_task = asyncio.create_task(settlement.commit())
        await asyncio.wait_for(worker_entered_authority.wait(), timeout=2)
        await asyncio.wait_for(
            _wait_until_backend_waits_for_lock(seed, backend_pids["worker"]),
            timeout=4,
        )

        # A correct Worker waits for Project before it can own Prepare. Reset
        # can therefore delete the row and commit; Worker then uses its Job
        # lease fallback. Prepare -> Project instead deadlocks here when reset
        # continues from its already-held Project lock into Prepare.
        release_reset.set()
        reset_result, worker_result = await asyncio.wait_for(
            asyncio.gather(reset_task, worker_task),
            timeout=8,
        )

        assert worker_result is None
        assert reset_result.prepare_runs == 1
        assert reset_result.jobs_cancelled == 1
        async with seed.factory() as session:
            job = await session.get(JobRow, prepare_job_id)
            prepare_row = await session.get(
                MemoryDreamPrepareRunRow,
                prepare_job_id,
            )
        assert job is not None and job.status == "cancelled"
        assert job.cancel_reason == "memory_reset"
        assert prepare_row is None
    finally:
        release_reset.set()
        for task in (reset_task, worker_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (reset_task, worker_task) if task is not None),
            return_exceptions=True,
        )
        await seed.engine.dispose()


class _FailingResetAudit:
    def __init__(self) -> None:
        self.dream_settled = False

    async def memory_dream_settled(self, *_args, **_kwargs) -> None:
        self.dream_settled = True

    async def memory_reset_executed(self, *_args, **_kwargs) -> None:
        raise RuntimeError("audit unavailable")


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_reset_rolls_back_data_job_and_preference_on_audit_failure(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = _scope(seed)
    now = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            prepare_thread_id = await _add_thread(
                session,
                project_id=scope.project_id,
                owner_user_id=scope.owner_user_id,
                label="rollback-prepare",
            )
            prepare_job_id = await _admit_prepare(
                session,
                scope,
                thread_id=prepare_thread_id,
                now=now,
            )
        job_id, preference_version = await _seed_queued_dream(
            seed,
            scope,
            now=now,
        )
        audit = _FailingResetAudit()
        service = AccountPersonalizationService(
            seed.factory,
            audit=audit,
        )

        with pytest.raises(AccountPersonalizationUnavailable):
            await service.reset_memory(
                uuid.UUID(scope.owner_user_id),
                expected_version=preference_version,
                request_id="f" * 32,
            )

        assert audit.dream_settled
        async with seed.factory() as session:
            preference = await session.get(UserRow, scope.owner_user_id)
            job = await session.get(JobRow, job_id)
            prepare_job = await session.get(JobRow, prepare_job_id)
            prepare_row = await session.get(
                MemoryDreamPrepareRunRow,
                prepare_job_id,
            )
            document = await session.get(
                MemoryDocumentRow,
                (scope.project_id, scope.owner_user_id, scope.namespace),
            )
            dream_run = await session.get(MemoryDreamRunRow, job_id)
            history = await session.scalar(sa.select(sa.func.count()).select_from(MemoryHistoryEntryRow))
        assert preference is not None
        assert preference.preferences_version == preference_version
        assert job is not None
        assert job.status == "queued"
        assert job.cancel_requested_at is None
        assert job.cancel_reason is None
        assert prepare_job is not None
        assert prepare_job.status == "queued"
        assert prepare_job.cancel_requested_at is None
        assert prepare_job.cancel_reason is None
        assert prepare_row is not None
        assert prepare_row.completed_at is None
        assert document is not None
        assert document.active_dream_job_id == job_id
        assert dream_run is not None
        assert history == 1
    finally:
        await seed.engine.dispose()
