from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.run_closure import add_sealed_test_run

from app.private_work import run_skill_tree_materializer as materializer_module
from app.private_work.retention_jobs import RetentionJobAdmission
from app.private_work.retention_purge import (
    RetentionCandidate,
    RetentionExecutionActive,
    RetentionPurgeRepository,
)
from app.private_work.run_repository import PrivateRunRepository
from app.private_work.run_skill_tree_materializer import (
    MaterializationAttemptIdentity,
    RunSkillTreeMaterializer,
)
from app.private_work.run_skill_tree_orphan_reaper import RunSkillTreeOrphanReaper
from app.reliability.jobs import PrivateRunJobRepository
from app.reliability.workers import WorkerRegistry
from deerflow.config import paths as paths_module
from deerflow.config.paths import Paths
from deerflow.config.worker_config import WorkerConfig
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import (
    EnqueueJob,
    JobRepository,
    JobScope,
    JobUnstartedClaimRelease,
    RetentionPurgeJobAuthority,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.user.private_lifecycle import AccountPrivateGeneration
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.sandbox.local.local_sandbox_provider import LocalSandboxProvider
from deerflow.sandbox.sandbox_provider import (
    Orphaned,
    ProviderRunMountLease,
    ProviderRunMountOwnerAbsentProof,
    ProviderRunMountOwnerUnknown,
)


@dataclass(frozen=True, slots=True)
class _OwnerScope:
    user_id: str
    project_id: uuid.UUID
    membership_id: uuid.UUID


class _UnknownMountOwnerProvider:
    async def ensure_run_readonly_mount_owner_absent_async(
        self,
        owner_id: uuid.UUID,
        *,
        persisted_lease: ProviderRunMountLease | None,
    ) -> ProviderRunMountOwnerUnknown:
        return ProviderRunMountOwnerUnknown(
            owner_id=owner_id,
            provider_kind=(persisted_lease.provider_kind if persisted_lease is not None else None),
            reason_code="owner_readback_unknown",
        )


class _MismatchedMountOwnerProvider:
    async def ensure_run_readonly_mount_owner_absent_async(
        self,
        owner_id: uuid.UUID,
        *,
        persisted_lease: ProviderRunMountLease | None,
    ) -> ProviderRunMountOwnerAbsentProof:
        del owner_id
        return ProviderRunMountOwnerAbsentProof(
            owner_id=uuid.uuid4(),
            provider_kind=(persisted_lease.provider_kind if persisted_lease is not None else "local"),
        )


class _RuntimeSkillTreeSlot:
    def __init__(self) -> None:
        self.tree = None

    def adopt_materialized_skill_tree(self, tree) -> None:
        self.tree = tree


async def _seed_owner(session, label: str) -> _OwnerScope:
    user_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    await session.execute(
        text(
            """INSERT INTO users (
                   id, email, username, system_role, created_at,
                   needs_setup, token_version
               ) VALUES (
                   :user_id, :email, :username, 'user', now(), false, 1
               )"""
        ),
        {
            "user_id": user_id,
            "email": f"{label}@example.invalid",
            "username": f"owner_{label}",
        },
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (
                   :project_id, :slug, :display_name, :user_id
               )"""
        ),
        {
            "project_id": project_id,
            "slug": f"owner-{label}",
            "display_name": label,
            "user_id": user_id,
        },
    )
    await session.execute(
        text(
            """INSERT INTO project_memberships (
                   id, project_id, user_id, role, status
               ) VALUES (
                   :membership_id, :project_id, :user_id, 'admin', 'active'
               )"""
        ),
        {
            "membership_id": membership_id,
            "project_id": project_id,
            "user_id": user_id,
        },
    )
    return _OwnerScope(user_id, project_id, membership_id)


async def _enqueue_memory_seal(
    session,
    scope: _OwnerScope,
    *,
    priority: int,
) -> uuid.UUID:
    return await JobRepository(session).enqueue(
        EnqueueJob(
            job_type="memory_seal",
            scope=JobScope(scope.project_id, scope.user_id),
            owner_private_generation=AccountPrivateGeneration(
                owner_user_id=scope.user_id,
                generation=1,
            ),
            namespace=f"thread-{scope.user_id}",
            idempotency_key=hashlib.sha256(f"memory-seal:{scope.user_id}".encode()).hexdigest(),
            run_id=None,
            occurrence_id=None,
            max_attempts=3,
            priority=priority,
        )
    )


async def _enqueue_private_run(
    session,
    scope: _OwnerScope,
) -> tuple[uuid.UUID, str]:
    agent_id = uuid.uuid4()
    thread_id = f"retention-mount-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex
    await session.execute(
        text(
            """INSERT INTO agents (
                   id, scope, project_id, slug, display_name,
                   status, created_by_user_id
               ) VALUES (
                   :agent_id, 'project', :project_id, :slug,
                   'Retention Mount Agent', 'active', :user_id
               )"""
        ),
        {
            "agent_id": agent_id,
            "project_id": scope.project_id,
            "slug": f"retention-mount-{agent_id.hex}",
            "user_id": scope.user_id,
        },
    )
    await session.execute(
        text(
            """INSERT INTO threads_meta (
                   thread_id, owner_user_id, status, metadata_json,
                   created_at, updated_at, project_id, agent_asset_id,
                   agent_scope
               ) VALUES (
                   :thread_id, :owner_user_id, 'idle', '{}'::json,
                   now(), now(), :project_id, :agent_id, 'project'
               )"""
        ),
        {
            "thread_id": thread_id,
            "owner_user_id": scope.user_id,
            "project_id": scope.project_id,
            "agent_id": agent_id,
        },
    )
    await add_sealed_test_run(
        session,
        RunRow(
            run_id=run_id,
            thread_id=thread_id,
            assistant_id=str(agent_id),
            owner_user_id=scope.user_id,
            status="pending",
            multitask_strategy="reject",
            metadata_json={},
            kwargs_json={},
            origin_trace_id=trace_id,
            project_id=scope.project_id,
        ),
    )
    generation = AccountPrivateGeneration(
        owner_user_id=scope.user_id,
        generation=1,
    )
    job = await PrivateRunJobRepository(session).enqueue(
        scope=JobScope(scope.project_id, scope.user_id),
        run_id=run_id,
        origin_trace_id=trace_id,
        account_private_generation=generation,
    )
    await PrivateRunRepository(session).attach_job(
        scope=PrivateResourceScope(
            project_id=str(scope.project_id),
            owner_user_id=scope.user_id,
            membership_version=1,
        ),
        run_id=run_id,
        job_id=job.job_id,
    )
    return job.job_id, run_id


async def _create_provider_lifecycle_owner(
    materialization_root: Path,
    identity: MaterializationAttemptIdentity,
    *,
    state: str,
) -> uuid.UUID:
    materializer = RunSkillTreeMaterializer(
        materialization_root=materialization_root,
        worker_config=WorkerConfig(),
    )
    builder = await materializer.begin_attempt(identity)
    owner_id = builder.owner_id
    await builder.write_file(
        "custom/exact/SKILL.md",
        b"---\nname: exact\n---\n",
    )
    pending = await builder.publish(manifests=(), skills=())
    if state == "materialized":
        return owner_id
    runtime = pending.transfer_to(_RuntimeSkillTreeSlot())
    await runtime.persist_mount_acquiring()
    if state == "acquiring":
        return owner_id
    lease = ProviderRunMountLease(
        owner_id=owner_id,
        provider_kind="local",
        sandbox_id=f"local-run:{owner_id.hex}",
        mount_lease_id=uuid.uuid4().hex,
    )
    await runtime.persist_mount_mounted(lease)
    if state == "mounted":
        return owner_id
    await runtime.finalize(
        Orphaned.from_lease(
            lease,
            reason_code="release_readback_unknown",
            last_lifecycle_state="mounted",
        )
    )
    if state != "release_pending":
        raise ValueError("unsupported provider lifecycle state")
    return owner_id


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "job_type",
        "owner_user",
        "generation",
        "resource_kind",
        "effective_at",
        "membership",
        "namespace",
    ),
    [
        pytest.param(
            "memory_seal",
            True,
            None,
            None,
            None,
            False,
            "thread-null-generation",
            id="ordinary-null-generation",
        ),
        pytest.param(
            "memory_seal",
            False,
            1,
            None,
            None,
            False,
            "thread-null-owner",
            id="ordinary-null-owner",
        ),
        pytest.param(
            "memory_seal",
            True,
            1,
            "account",
            datetime(2026, 8, 25, tzinfo=UTC),
            False,
            "thread-stray-retention",
            id="ordinary-stray-retention-fields",
        ),
        pytest.param(
            "retention_purge",
            False,
            1,
            None,
            datetime(2026, 8, 25, tzinfo=UTC),
            False,
            None,
            id="retention-null-kind",
        ),
        pytest.param(
            "retention_purge",
            False,
            1,
            "project",
            None,
            False,
            None,
            id="project-null-effective-at",
        ),
        pytest.param(
            "retention_purge",
            False,
            None,
            "project",
            datetime(2026, 8, 25, tzinfo=UTC),
            False,
            None,
            id="project-null-generation",
        ),
        pytest.param(
            "retention_purge",
            True,
            1,
            "project",
            datetime(2026, 8, 25, tzinfo=UTC),
            False,
            None,
            id="project-owner-present",
        ),
        pytest.param(
            "retention_purge",
            False,
            1,
            "project",
            datetime(2026, 8, 25, tzinfo=UTC),
            True,
            None,
            id="project-membership-present",
        ),
        pytest.param(
            "retention_purge",
            True,
            1,
            "former_owner",
            datetime(2026, 8, 25, tzinfo=UTC),
            False,
            None,
            id="former-owner-null-membership",
        ),
        pytest.param(
            "retention_purge",
            False,
            1,
            "former_owner",
            datetime(2026, 8, 25, tzinfo=UTC),
            True,
            None,
            id="former-owner-null-owner",
        ),
        pytest.param(
            "retention_purge",
            False,
            1,
            "account",
            datetime(2026, 8, 25, tzinfo=UTC),
            False,
            None,
            id="account-null-owner",
        ),
        pytest.param(
            "retention_purge",
            True,
            1,
            "account",
            datetime(2026, 8, 25, tzinfo=UTC),
            True,
            None,
            id="account-membership-present",
        ),
    ],
)
async def test_jobs_schema_rejects_null_durable_authority_coordinates(
    postgres_database_url: str,
    job_type: str,
    owner_user: bool,
    generation: int | None,
    resource_kind: str | None,
    effective_at: datetime | None,
    membership: bool,
    namespace: str | None,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_owner(
                session,
                "nullgen" if job_type == "memory_seal" else "nullkind",
            )
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    await session.execute(
                        text(
                            """INSERT INTO jobs (
                                   id, job_type, project_id, owner_user_id,
                                   owner_private_generation,
                                   retention_resource_kind,
                                   retention_effective_at,
                                   retention_membership_id,
                                   namespace, idempotency_key, max_attempts
                               ) VALUES (
                                   :id, :job_type, :project_id, :owner_user_id,
                                   :generation, :resource_kind, :effective_at,
                                   :membership_id, :namespace, :idempotency_key, 1
                               )"""
                        ),
                        {
                            "id": uuid.uuid4(),
                            "job_type": job_type,
                            "project_id": scope.project_id,
                            "owner_user_id": scope.user_id if owner_user else None,
                            "generation": generation,
                            "resource_kind": resource_kind,
                            "effective_at": effective_at,
                            "membership_id": (scope.membership_id if membership else None),
                            "namespace": namespace,
                            "idempotency_key": hashlib.sha256(f"invalid:{job_type}".encode()).hexdigest(),
                        },
                    )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_account_retention_phase_a_persists_typed_fence_and_cancels_scope(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    effective_at = now - timedelta(seconds=1)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_owner(session, "accountretention")
            second_project_id = uuid.uuid4()
            second_membership_id = uuid.uuid4()
            await session.execute(
                text(
                    """INSERT INTO projects (
                           id, slug, display_name, created_by_user_id
                       ) VALUES (
                           :project_id, :slug, 'Second', :user_id
                       )"""
                ),
                {
                    "project_id": second_project_id,
                    "slug": f"account-retention-{uuid.uuid4().hex[:8]}",
                    "user_id": scope.user_id,
                },
            )
            await session.execute(
                text(
                    """INSERT INTO project_memberships (
                           id, project_id, user_id, role, status
                       ) VALUES (
                           :membership_id, :project_id, :user_id,
                           'admin', 'active'
                       )"""
                ),
                {
                    "membership_id": second_membership_id,
                    "project_id": second_project_id,
                    "user_id": scope.user_id,
                },
            )
            ordinary_job_id = await _enqueue_memory_seal(
                session,
                scope,
                priority=0,
            )
            coordinator_id = await RetentionJobAdmission.admit_account(
                session,
                owner_user_id=scope.user_id,
                deletion_effective_at=effective_at,
                now=now,
            )

        async with factory() as session, session.begin():
            user = await session.execute(
                text(
                    """SELECT private_retention_state,
                              private_retention_generation,
                              private_retention_effective_at
                         FROM users WHERE id=:user_id"""
                ),
                {"user_id": scope.user_id},
            )
            state, generation, observed_effective_at = user.one()
            assert (state, generation, observed_effective_at) == (
                "pending_deletion",
                2,
                effective_at,
            )
            ordinary = await session.get(JobRow, ordinary_job_id)
            coordinator = await session.get(JobRow, coordinator_id)
            assert ordinary is not None and ordinary.status == "cancelled"
            assert coordinator is not None
            assert coordinator.retention_resource_kind == "account"
            assert coordinator.owner_private_generation == 2
            assert coordinator.retention_effective_at == effective_at
            assert coordinator.retention_membership_id is None

            candidate = RetentionCandidate.account(
                owner_user_id=scope.user_id,
                project_ids=tuple(sorted((scope.project_id, second_project_id))),
                account_private_generation=2,
                retention_until=effective_at,
                idempotency_key=coordinator.idempotency_key,
                request_id="account-retention-test",
            )
            scopes = await RetentionPurgeRepository().verify_still_eligible(
                session,
                candidate,
                now=now,
                coordinator_job_id=coordinator_id,
            )
            assert scopes == tuple(
                (project_id, scope.user_id)
                for project_id in sorted(
                    (scope.project_id, second_project_id),
                )
            )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retention_admission_persists_exact_project_and_former_owner_authority(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    project_deadline = now + timedelta(days=30)
    former_owner_deadline = now + timedelta(days=7)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            project_scope = await _seed_owner(session, "projectretention")
            former_owner_scope = await _seed_owner(session, "formerretention")
            project_ordinary_job_id = await _enqueue_memory_seal(
                session,
                project_scope,
                priority=0,
            )
            former_owner_ordinary_job_id = await _enqueue_memory_seal(
                session,
                former_owner_scope,
                priority=0,
            )
            await session.execute(
                text(
                    """UPDATE projects
                          SET status='pending_deletion',
                              deletion_effective_at=:deadline
                        WHERE id=:project_id"""
                ),
                {
                    "project_id": project_scope.project_id,
                    "deadline": project_deadline,
                },
            )
            await session.execute(
                text(
                    """UPDATE project_memberships
                          SET status='left', ended_at=:now,
                              end_reason='left', retention_until=:deadline
                        WHERE id=:membership_id"""
                ),
                {
                    "membership_id": former_owner_scope.membership_id,
                    "now": now,
                    "deadline": former_owner_deadline,
                },
            )
            project_job_id = await RetentionJobAdmission.admit_project(
                session,
                project_id=project_scope.project_id,
                deletion_effective_at=project_deadline,
                now=now,
            )
            former_owner_job_id = await RetentionJobAdmission.admit_former_owner(
                session,
                project_id=former_owner_scope.project_id,
                owner_user_id=former_owner_scope.user_id,
                membership_id=former_owner_scope.membership_id,
                activation_generation=1,
                retention_until=former_owner_deadline,
            )

        async with factory() as session:
            project_job = await session.get(JobRow, project_job_id)
            former_owner_job = await session.get(JobRow, former_owner_job_id)
            project_ordinary_job = await session.get(
                JobRow,
                project_ordinary_job_id,
            )
            former_owner_ordinary_job = await session.get(
                JobRow,
                former_owner_ordinary_job_id,
            )
            assert project_job is not None
            assert project_job.retention_resource_kind == "project"
            assert project_job.owner_user_id is None
            assert project_job.owner_private_generation == 1
            assert project_job.retention_effective_at == project_deadline
            assert project_job.retention_membership_id is None
            assert former_owner_job is not None
            assert former_owner_job.retention_resource_kind == "former_owner"
            assert former_owner_job.owner_user_id == former_owner_scope.user_id
            assert former_owner_job.owner_private_generation == 1
            assert former_owner_job.retention_effective_at == former_owner_deadline
            assert former_owner_job.retention_membership_id == former_owner_scope.membership_id
            assert project_ordinary_job is not None
            assert project_ordinary_job.status == "cancelled"
            assert project_ordinary_job.cancel_reason == "project_retention_pending"
            assert former_owner_ordinary_job is not None
            assert former_owner_ordinary_job.status == "cancelled"
            assert former_owner_ordinary_job.cancel_reason == "former_owner_retention_pending"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_early_former_owner_purge_ignores_dormant_deadline_coordinator(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    retention_until = now + timedelta(days=30)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_owner(session, "earlyretention")
            await session.execute(
                text(
                    """UPDATE project_memberships
                          SET status='left', ended_at=:now,
                              end_reason='left', retention_until=:retention_until
                        WHERE id=:membership_id"""
                ),
                {
                    "membership_id": scope.membership_id,
                    "now": now,
                    "retention_until": retention_until,
                },
            )
            deadline_job_id = await RetentionJobAdmission.admit_former_owner(
                session,
                project_id=scope.project_id,
                owner_user_id=scope.user_id,
                membership_id=scope.membership_id,
                activation_generation=1,
                retention_until=retention_until,
            )
            early_job_id = await RetentionJobAdmission.admit_early_delete(
                session,
                project_id=scope.project_id,
                owner_user_id=scope.user_id,
                membership_id=scope.membership_id,
                activation_generation=1,
                retention_until=retention_until,
                now=now,
            )

        async with factory() as session, session.begin():
            deadline_job = await session.get(JobRow, deadline_job_id)
            early_job = await session.get(JobRow, early_job_id)
            assert deadline_job is not None and early_job is not None
            assert deadline_job.available_at == retention_until
            scopes = await RetentionPurgeRepository().verify_still_eligible(
                session,
                RetentionCandidate.former_owner(
                    project_id=scope.project_id,
                    owner_user_id=scope.user_id,
                    membership_id=scope.membership_id,
                    activation_generation=1,
                    retention_until=retention_until,
                    idempotency_key=early_job.idempotency_key,
                    request_id="early-retention-test",
                    early_delete=True,
                    eligibility_at=now,
                ),
                now=now,
                coordinator_job_id=early_job_id,
            )
            assert scopes == ((scope.project_id, scope.user_id),)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_account_retention_phase_b_blocks_an_unsettled_active_attempt(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    phase_a_at = now + timedelta(seconds=1)
    effective_at = now - timedelta(seconds=1)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_owner(session, "attemptretention")
            ordinary_job_id = await _enqueue_memory_seal(
                session,
                scope,
                priority=0,
            )
            worker_id = uuid.uuid4()
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="retention-attempt-test",
                    capabilities_json=["memory_seal"],
                    max_concurrent_jobs=1,
                )
            )

        async with factory() as session, session.begin():
            claim = await JobRepository(session).claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"memory_seal"}),
                lease_seconds=60,
                now=phase_a_at,
            )
            assert claim is not None and claim.job_id == ordinary_job_id
            coordinator_id = await RetentionJobAdmission.admit_account(
                session,
                owner_user_id=scope.user_id,
                deletion_effective_at=effective_at,
                now=phase_a_at,
            )

        async with factory() as session, session.begin():
            ordinary = await session.get(JobRow, ordinary_job_id)
            coordinator = await session.get(JobRow, coordinator_id)
            assert ordinary is not None and coordinator is not None
            candidate = RetentionCandidate.account(
                owner_user_id=scope.user_id,
                project_ids=(scope.project_id,),
                account_private_generation=coordinator.owner_private_generation,
                retention_until=effective_at,
                idempotency_key=coordinator.idempotency_key,
                request_id="attempt-retention-test",
            )
            with pytest.raises(RetentionExecutionActive) as leased_error:
                await RetentionPurgeRepository().verify_still_eligible(
                    session,
                    candidate,
                    now=phase_a_at,
                    coordinator_job_id=coordinator_id,
                )
            assert leased_error.value.retry_after == ordinary.lease_expires_at

        async with factory() as session, session.begin():
            ordinary = await session.get(JobRow, ordinary_job_id, with_for_update=True)
            assert ordinary is not None
            ordinary.status = "cancelled"
            ordinary.completed_at = phase_a_at
            ordinary.lease_owner_id = None
            ordinary.lease_token_hash = None
            ordinary.lease_expires_at = None
            ordinary.heartbeat_at = None

        async with factory() as session, session.begin():
            with pytest.raises(RetentionExecutionActive) as attempt_error:
                await RetentionPurgeRepository().verify_still_eligible(
                    session,
                    candidate,
                    now=phase_a_at,
                    coordinator_job_id=coordinator_id,
                )
            assert attempt_error.value.retry_after is None

        async with factory() as session, session.begin():
            attempt = await session.get(JobAttemptRow, claim.attempt_id, with_for_update=True)
            assert attempt is not None
            attempt.outcome = "cancelled"
            attempt.finished_at = phase_a_at
            scopes = await RetentionPurgeRepository().verify_still_eligible(
                session,
                candidate,
                now=phase_a_at,
                coordinator_job_id=coordinator_id,
            )
            assert scopes == ((scope.project_id, scope.user_id),)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_account_retention_phase_b_requires_matching_provider_absent_proof(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    effective_at = now - timedelta(seconds=1)
    materialization_paths = Paths(tmp_path)
    monkeypatch.setattr(paths_module, "_paths", materialization_paths)
    materialization_root = materialization_paths.run_skill_materialization_root()
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_owner(session, "mountproof")
            private_job_id, run_id = await _enqueue_private_run(session, scope)
            worker_id = uuid.uuid4()
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="retention-mount-proof-test",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=1,
                )
            )

        phase_at = datetime.now(UTC) + timedelta(seconds=1)
        async with factory() as session, session.begin():
            claim = await JobRepository(session).claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=60,
                now=phase_at,
            )
            assert claim is not None and claim.job_id == private_job_id

        identity = MaterializationAttemptIdentity(
            job_id=uuid.UUID(str(claim.job_id)),
            attempt_id=uuid.UUID(str(claim.attempt_id)),
            worker_id=uuid.UUID(str(worker_id)),
        )
        async with factory() as session, session.begin():
            job = await session.get(JobRow, private_job_id, with_for_update=True)
            attempt = await session.get(
                JobAttemptRow,
                claim.attempt_id,
                with_for_update=True,
            )
            run = await session.get(RunRow, run_id, with_for_update=True)
            assert job is not None and attempt is not None and run is not None
            job.status = "cancelled"
            job.cancel_requested_at = phase_at
            job.cancel_reason = "retention_mount_proof_test"
            job.completed_at = phase_at
            job.lease_owner_id = None
            job.lease_token_hash = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            attempt.outcome = "cancelled"
            attempt.finished_at = phase_at
            run.status = "error"
            run.completed_at = phase_at
            coordinator_id = await RetentionJobAdmission.admit_account(
                session,
                owner_user_id=scope.user_id,
                deletion_effective_at=effective_at,
                now=phase_at,
            )

        async with factory() as session:
            coordinator = await session.get(JobRow, coordinator_id)
            assert coordinator is not None
            candidate = RetentionCandidate.account(
                owner_user_id=scope.user_id,
                project_ids=(scope.project_id,),
                account_private_generation=coordinator.owner_private_generation,
                retention_until=effective_at,
                idempotency_key=coordinator.idempotency_key,
                request_id="mount-proof-retention-test",
            )

        monkeypatch.setattr(
            materializer_module,
            "_utc_now",
            lambda: datetime.now(UTC) - timedelta(minutes=1),
        )
        for state in ("acquiring", "mounted", "release_pending"):
            owner_id = await _create_provider_lifecycle_owner(
                materialization_root,
                identity,
                state=state,
            )
            unknown = await RunSkillTreeOrphanReaper(
                engine=engine,
                materialization_root=materialization_root,
                provider=(_UnknownMountOwnerProvider() if state == "acquiring" else _MismatchedMountOwnerProvider()),
                grace_seconds=0,
            ).reap_startup()
            assert unknown.preserved_unknown == 1
            assert (materialization_root / owner_id.hex).is_dir()

            async with factory() as session, session.begin():
                with pytest.raises(RetentionExecutionActive):
                    await RetentionPurgeRepository().verify_still_eligible(
                        session,
                        candidate,
                        now=phase_at,
                        coordinator_job_id=coordinator_id,
                    )

            reaped = await RunSkillTreeOrphanReaper(
                engine=engine,
                materialization_root=materialization_root,
                provider=LocalSandboxProvider(),
                grace_seconds=0,
            ).reap_startup()
            assert reaped.deleted_provider_absent == 1
            assert not (materialization_root / owner_id.hex).exists()

        unrelated_owner_id = await _create_provider_lifecycle_owner(
            materialization_root,
            MaterializationAttemptIdentity(
                job_id=uuid.uuid4(),
                attempt_id=uuid.uuid4(),
                worker_id=uuid.uuid4(),
            ),
            state="acquiring",
        )
        unrelated_unknown = await RunSkillTreeOrphanReaper(
            engine=engine,
            materialization_root=materialization_root,
            provider=_UnknownMountOwnerProvider(),
            grace_seconds=0,
        ).reap_startup()
        assert unrelated_unknown.preserved_unknown == 1
        materialized_owner_id = await _create_provider_lifecycle_owner(
            materialization_root,
            identity,
            state="materialized",
        )

        async with factory() as session, session.begin():
            scopes = await RetentionPurgeRepository().verify_still_eligible(
                session,
                candidate,
                now=phase_at,
                coordinator_job_id=coordinator_id,
            )
            assert scopes == ((scope.project_id, scope.user_id),)

        final_reap = await RunSkillTreeOrphanReaper(
            engine=engine,
            materialization_root=materialization_root,
            provider=LocalSandboxProvider(),
            grace_seconds=0,
        ).reap_startup()
        assert final_reap.deleted_provider_absent == 1
        assert final_reap.deleted_never_acquired == 1
        assert not (materialization_root / unrelated_owner_id.hex).exists()
        assert not (materialization_root / materialized_owner_id.hex).exists()
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_claim_skips_closed_and_stale_generations_after_share_lock_prefix(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []

    def capture(_connection, _cursor, statement, *_args) -> None:
        statements.append(" ".join(statement.split()))

    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            stale = await _seed_owner(session, "stale")
            closed = await _seed_owner(session, "closed")
            active = await _seed_owner(session, "active")
            stale_job_id = await _enqueue_memory_seal(
                session,
                stale,
                priority=30,
            )
            closed_job_id = await _enqueue_memory_seal(
                session,
                closed,
                priority=20,
            )
            active_job_id = await _enqueue_memory_seal(
                session,
                active,
                priority=10,
            )
            await session.execute(
                text(
                    """UPDATE users
                          SET private_retention_generation=2
                        WHERE id=:user_id"""
                ),
                {"user_id": stale.user_id},
            )
            await session.execute(
                text(
                    """UPDATE users
                          SET private_retention_state='pending_deletion',
                              private_retention_generation=2,
                              private_retention_effective_at=:effective_at
                        WHERE id=:user_id"""
                ),
                {
                    "user_id": closed.user_id,
                    "effective_at": datetime.now(UTC) + timedelta(days=30),
                },
            )
            worker_id = uuid.uuid4()
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="owner-generation-test",
                    capabilities_json=["memory_seal"],
                    max_concurrent_jobs=1,
                )
            )

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with factory() as session, session.begin():
            claim = await JobRepository(session).claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"memory_seal"}),
                lease_seconds=60,
            )
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

        assert claim is not None
        assert claim.job_id == active_job_id
        normalized = [statement.lower() for statement in statements]
        project_lock = next(index for index, statement in enumerate(normalized) if "from projects" in statement and "for share" in statement)
        membership_lock = next(index for index, statement in enumerate(normalized) if "from project_memberships" in statement and "for share" in statement)
        user_lock = next(index for index, statement in enumerate(normalized) if "from users" in statement and "for share" in statement)
        job_lock = next(index for index, statement in enumerate(normalized) if "from jobs" in statement and "for update" in statement)
        assert project_lock < membership_lock < user_lock < job_lock

        async with factory() as session:
            rows = {row.id: (row.status, row.owner_private_generation) for row in (await session.execute(sa.select(JobRow).where(JobRow.id.in_((stale_job_id, closed_job_id, active_job_id))))).scalars()}
        assert rows == {
            stale_job_id: ("queued", 1),
            closed_job_id: ("queued", 1),
            active_job_id: ("leased", 1),
        }
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retention_claim_uses_typed_exception_without_active_user_guard(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        effective_at = datetime.now(UTC) + timedelta(days=30)
        async with factory() as session, session.begin():
            scope = await _seed_owner(session, "retention")
            await session.execute(
                text(
                    """UPDATE projects
                          SET status='pending_deletion'
                        WHERE id=:project_id"""
                ),
                {"project_id": scope.project_id},
            )
            await session.execute(
                text(
                    """UPDATE project_memberships
                          SET status='left', ended_at=now(), end_reason='left',
                              retention_until=:effective_at
                        WHERE id=:membership_id"""
                ),
                {
                    "membership_id": scope.membership_id,
                    "effective_at": effective_at,
                },
            )
            await session.execute(
                text(
                    """UPDATE users
                          SET private_retention_state='pending_deletion',
                              private_retention_generation=2,
                              private_retention_effective_at=:effective_at
                        WHERE id=:user_id"""
                ),
                {
                    "user_id": scope.user_id,
                    "effective_at": effective_at,
                },
            )
            job_id = await JobRepository(session).enqueue(
                EnqueueJob(
                    job_type="retention_purge",
                    scope=JobScope(scope.project_id, scope.user_id),
                    owner_private_generation=RetentionPurgeJobAuthority(
                        resource_kind="former_owner",
                        project_id=scope.project_id,
                        owner_user_id=scope.user_id,
                        generation=1,
                        effective_at=effective_at,
                        membership_id=scope.membership_id,
                    ),
                    idempotency_key=hashlib.sha256(b"retention-claim").hexdigest(),
                    run_id=None,
                    occurrence_id=None,
                    max_attempts=5,
                )
            )
            worker_id = uuid.uuid4()
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="retention-generation-test",
                    capabilities_json=["retention_purge"],
                    max_concurrent_jobs=1,
                )
            )

        async with factory() as session, session.begin():
            claim = await JobRepository(session).claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"retention_purge"}),
                lease_seconds=60,
            )

        assert claim is not None
        assert claim.job_id == job_id
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retention_claim_skips_stale_typed_authority_for_every_resource_kind(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    effective_at = now - timedelta(seconds=1)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            project_scope = await _seed_owner(session, "staleprojectretention")
            former_scope = await _seed_owner(session, "staleformerretention")
            account_scope = await _seed_owner(session, "staleaccountretention")
            await session.execute(
                text(
                    """UPDATE projects
                          SET status='pending_deletion',
                              deletion_effective_at=:effective_at
                        WHERE id=:project_id"""
                ),
                {
                    "project_id": project_scope.project_id,
                    "effective_at": effective_at,
                },
            )
            await session.execute(
                text(
                    """UPDATE project_memberships
                          SET status='left', ended_at=:now,
                              end_reason='left', retention_until=:effective_at
                        WHERE id=:membership_id"""
                ),
                {
                    "membership_id": former_scope.membership_id,
                    "now": now,
                    "effective_at": effective_at,
                },
            )
            await RetentionJobAdmission.admit_project(
                session,
                project_id=project_scope.project_id,
                deletion_effective_at=effective_at,
                now=now,
            )
            await RetentionJobAdmission.admit_former_owner(
                session,
                project_id=former_scope.project_id,
                owner_user_id=former_scope.user_id,
                membership_id=former_scope.membership_id,
                activation_generation=1,
                retention_until=effective_at,
            )
            await RetentionJobAdmission.admit_account(
                session,
                owner_user_id=account_scope.user_id,
                deletion_effective_at=effective_at,
                now=now,
            )
            await session.execute(
                text(
                    """UPDATE projects
                          SET membership_version=membership_version + 1
                        WHERE id=:project_id"""
                ),
                {"project_id": project_scope.project_id},
            )
            await session.execute(
                text(
                    """UPDATE project_memberships
                          SET activation_generation=activation_generation + 1
                        WHERE id=:membership_id"""
                ),
                {"membership_id": former_scope.membership_id},
            )
            await session.execute(
                text(
                    """UPDATE users
                          SET private_retention_state='active',
                              private_retention_generation=private_retention_generation + 1,
                              private_retention_effective_at=NULL
                        WHERE id=:user_id"""
                ),
                {"user_id": account_scope.user_id},
            )
            worker_id = uuid.uuid4()
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="stale-retention-authority-test",
                    capabilities_json=["retention_purge"],
                    max_concurrent_jobs=1,
                )
            )

        async with factory() as session, session.begin():
            claim = await JobRepository(session).claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"retention_purge"}),
                lease_seconds=60,
                now=now + timedelta(seconds=1),
            )

        assert claim is None
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_begin_execution_stamps_only_the_exact_current_attempt_once(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    claimed_at = datetime(2035, 1, 2, 3, 4, tzinfo=UTC)
    first_execution_at = claimed_at + timedelta(seconds=2)
    later_execution_at = claimed_at + timedelta(seconds=5)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            owner = await _seed_owner(session, "attempt")
            agent_id = uuid.uuid4()
            thread_id = f"attempt-{uuid.uuid4()}"
            run_id = str(uuid.uuid4())
            trace_id = uuid.uuid4().hex
            await session.execute(
                text(
                    """INSERT INTO agents (
                           id, scope, project_id, slug, display_name,
                           status, created_by_user_id
                       ) VALUES (
                           :agent_id, 'project', :project_id, 'attempt-agent',
                           'Attempt Agent', 'active', :user_id
                       )"""
                ),
                {
                    "agent_id": agent_id,
                    "project_id": owner.project_id,
                    "user_id": owner.user_id,
                },
            )
            await session.execute(
                text(
                    """INSERT INTO threads_meta (
                           thread_id, owner_user_id, status, metadata_json,
                           created_at, updated_at, project_id, agent_asset_id,
                           agent_scope
                       ) VALUES (
                           :thread_id, :owner_user_id, 'idle', '{}'::json,
                           now(), now(), :project_id, :agent_id, 'project'
                       )"""
                ),
                {
                    "thread_id": thread_id,
                    "owner_user_id": owner.user_id,
                    "project_id": owner.project_id,
                    "agent_id": agent_id,
                },
            )
            run = await add_sealed_test_run(
                session,
                RunRow(
                    run_id=run_id,
                    thread_id=thread_id,
                    assistant_id=str(agent_id),
                    owner_user_id=owner.user_id,
                    status="pending",
                    multitask_strategy="reject",
                    metadata_json={},
                    kwargs_json={},
                    origin_trace_id=trace_id,
                    project_id=owner.project_id,
                ),
            )
            generation = AccountPrivateGeneration(
                owner_user_id=owner.user_id,
                generation=1,
            )
            job = await PrivateRunJobRepository(session).enqueue(
                scope=JobScope(owner.project_id, owner.user_id),
                run_id=run_id,
                origin_trace_id=trace_id,
                account_private_generation=generation,
            )
            await PrivateRunRepository(session).attach_job(
                scope=PrivateResourceScope(
                    project_id=str(owner.project_id),
                    owner_user_id=owner.user_id,
                    membership_version=1,
                ),
                run_id=run_id,
                job_id=job.job_id,
            )
            worker_id = uuid.uuid4()
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="attempt-facts-test",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=1,
                )
            )

        async with factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=60,
                now=claimed_at,
            )
            assert claim is not None
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
                now=claimed_at + timedelta(seconds=1),
            )
            attempt = await session.get(JobAttemptRow, claim.attempt_id)
            assert attempt is not None
            assert attempt.execution_started_at is None

        scope = PrivateResourceScope(
            project_id=str(owner.project_id),
            owner_user_id=owner.user_id,
            membership_version=1,
        )
        async with factory() as session, session.begin():
            repository = PrivateRunRepository(session)
            await repository.begin_execution(
                scope=scope,
                run_id=run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                origin_trace_id=trace_id,
                now=first_execution_at,
            )
            await repository.begin_execution(
                scope=scope,
                run_id=run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                origin_trace_id=trace_id,
                now=later_execution_at,
            )

        async with factory() as session:
            attempt = await session.get(JobAttemptRow, claim.attempt_id)
            run = await session.get(RunRow, run_id)
            assert attempt is not None and run is not None
            assert attempt.execution_started_at == first_execution_at
            assert run.execution_started_at == first_execution_at
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_release_unstarted_claim_is_exact_and_preserves_attempt_history(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    released_at = datetime(2035, 2, 3, 4, 5, tzinfo=UTC)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_owner(session, "release")
            job_id = await _enqueue_memory_seal(session, scope, priority=0)
            first_worker_id = uuid.uuid4()
            second_worker_id = uuid.uuid4()
            session.add_all(
                [
                    WorkerNodeRow(
                        id=worker_id,
                        version="release-test",
                        capabilities_json=["memory_seal"],
                        max_concurrent_jobs=1,
                    )
                    for worker_id in (first_worker_id, second_worker_id)
                ]
            )

        async with factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=first_worker_id,
                capabilities=frozenset({"memory_seal"}),
                lease_seconds=60,
                now=released_at - timedelta(seconds=1),
            )
            assert claim is not None and claim.job_id == job_id

        async with factory() as session, session.begin():
            jobs = JobRepository(session)
            assert (
                await jobs.release_unstarted_claim(
                    claim.job_id,
                    lease_token="wrong-token",
                    attempt_id=claim.attempt_id,
                    expected_worker_id=first_worker_id,
                    now=released_at,
                )
                is False
            )
            assert (
                await jobs.release_unstarted_claim(
                    claim.job_id,
                    lease_token=claim.lease_token,
                    attempt_id=uuid.uuid4(),
                    expected_worker_id=first_worker_id,
                    now=released_at,
                )
                is False
            )
            assert (
                await jobs.release_unstarted_claim(
                    claim.job_id,
                    lease_token=claim.lease_token,
                    attempt_id=claim.attempt_id,
                    expected_worker_id=second_worker_id,
                    now=released_at,
                )
                is False
            )
            released = await jobs.release_unstarted_claim(
                claim.job_id,
                lease_token=claim.lease_token,
                attempt_id=claim.attempt_id,
                expected_worker_id=first_worker_id,
                now=released_at,
            )
            assert released == JobUnstartedClaimRelease(
                disposition="requeued",
            )

        async with factory() as session, session.begin():
            next_claim = await JobRepository(session).claim_next(
                worker_id=second_worker_id,
                capabilities=frozenset({"memory_seal"}),
                lease_seconds=60,
                now=released_at + timedelta(seconds=1),
            )
            assert next_claim is not None
            assert next_claim.job_id == job_id
            assert next_claim.attempt_id != claim.attempt_id

        async with factory() as session:
            job = await session.get(JobRow, job_id)
            attempts = (await session.execute(sa.select(JobAttemptRow).where(JobAttemptRow.job_id == job_id).order_by(JobAttemptRow.attempt_number))).scalars().all()
            assert job is not None
            assert job.attempt_count == 2
            assert [attempt.outcome for attempt in attempts] == ["retry", None]
            assert attempts[0].finished_at == released_at
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_release_unstarted_claim_settles_cancel_but_rejects_started_attempt(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2035, 3, 4, 5, 6, tzinfo=UTC)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            cancel_scope = await _seed_owner(session, "cancel")
            started_scope = await _seed_owner(session, "started")
            cancel_job_id = await _enqueue_memory_seal(
                session,
                cancel_scope,
                priority=20,
            )
            started_job_id = await _enqueue_memory_seal(
                session,
                started_scope,
                priority=10,
            )
            worker_id = uuid.uuid4()
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="release-guard-test",
                    capabilities_json=["memory_seal"],
                    max_concurrent_jobs=1,
                )
            )

        async with factory() as session, session.begin():
            jobs = JobRepository(session)
            cancel_claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"memory_seal"}),
                lease_seconds=60,
                now=now,
            )
            assert cancel_claim is not None
            assert cancel_claim.job_id == cancel_job_id
            assert await jobs.request_cancel(
                JobScope(cancel_scope.project_id, cancel_scope.user_id),
                cancel_job_id,
                reason="worker_stopping",
                now=now + timedelta(seconds=1),
            )
            released = await jobs.release_unstarted_claim(
                cancel_job_id,
                lease_token=cancel_claim.lease_token,
                attempt_id=cancel_claim.attempt_id,
                expected_worker_id=worker_id,
                now=now + timedelta(seconds=2),
            )
            assert released == JobUnstartedClaimRelease(
                disposition="cancelled",
            )

        async with factory() as session, session.begin():
            jobs = JobRepository(session)
            started_claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"memory_seal"}),
                lease_seconds=60,
                now=now + timedelta(seconds=3),
            )
            assert started_claim is not None
            assert started_claim.job_id == started_job_id
            attempt = await session.get(
                JobAttemptRow,
                started_claim.attempt_id,
                with_for_update=True,
            )
            assert attempt is not None
            attempt.execution_started_at = now + timedelta(seconds=4)
            await session.flush()
            assert (
                await jobs.release_unstarted_claim(
                    started_job_id,
                    lease_token=started_claim.lease_token,
                    attempt_id=started_claim.attempt_id,
                    expected_worker_id=worker_id,
                    now=now + timedelta(seconds=5),
                )
                is False
            )

        async with factory() as session:
            cancel_job = await session.get(JobRow, cancel_job_id)
            cancel_attempt = await session.get(
                JobAttemptRow,
                cancel_claim.attempt_id,
            )
            started_job = await session.get(JobRow, started_job_id)
            assert cancel_job is not None and cancel_attempt is not None
            assert started_job is not None
            assert cancel_job.status == "cancelled"
            assert cancel_attempt.outcome == "cancelled"
            assert started_job.status == "leased"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_registry_persists_exact_execution_domain_affinity(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    worker_id = uuid.uuid4()
    affinity = "a" * 64
    registered_at = datetime(2035, 4, 5, 6, 7, tzinfo=UTC)
    try:
        await _install_full_schema(engine)
        registry = WorkerRegistry(factory, version="affinity-test")
        await registry.register(
            worker_id,
            frozenset({"private_run"}),
            2,
            execution_domain_affinity=affinity,
            now=registered_at,
        )

        async with factory() as session:
            worker = await session.get(WorkerNodeRow, worker_id)
            assert worker is not None
            assert worker.execution_domain_affinity == affinity
            assert worker.heartbeat_at == registered_at
    finally:
        await engine.dispose()
