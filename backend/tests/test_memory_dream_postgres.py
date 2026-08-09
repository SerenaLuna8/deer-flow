from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from support.private_thread_seed import seed_private_thread_database

from app.private_work.memory_dream_service import (
    MemoryDreamAdmissionService,
    MemoryDreamSchedulerService,
)
from app.system_runtime_settings.models import (
    RuntimePolicySection,
    default_policy_value,
)
from app.system_runtime_settings.repository import SystemRuntimePolicyRepository
from app.system_runtime_settings.validation import canonical_policy_payload
from app.worker import memory_dream as memory_dream_worker_module
from app.worker.memory_dream import MemoryDreamJobHandler
from app.worker.service import JobLeaseAuthority, JobSettlement
from deerflow.agents.memory.dream import (
    DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
    DREAM_PROMPT_VERSION,
    EMPTY_MEMORY_DOCUMENT,
    MemoryDreamInput,
    MemoryDreamResult,
)
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import JobClaim, JobOwnerRef, JobRepository
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    MemoryDocumentVersionRow,
    MemoryDreamRunRow,
    MemoryHistoryEntryRow,
)
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentConflict,
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryDreamFrozenRuntime,
    MemoryDreamTrigger,
    memory_document_digest,
)
from deerflow.persistence.system_runtime_settings import (
    SystemRuntimePolicyRow,
    SystemRuntimePolicyVersionRow,
)


def _owner_ref(_owner_user_id: str) -> JobOwnerRef:
    return JobOwnerRef(key_id="memory-test", hmac_hex="f" * 64)


def _jobs(session) -> JobRepository:
    return JobRepository(session, owner_ref_hasher=_owner_ref)


async def _memory_document_policy_version_id(session) -> uuid.UUID:
    version_id = await session.scalar(
        sa.select(SystemRuntimePolicyRow.current_version_id).where(
            SystemRuntimePolicyRow.section == "memory_document",
        )
    )
    assert isinstance(version_id, uuid.UUID)
    return version_id


class _RecordingDreamRunner:
    def __init__(self, content: str) -> None:
        self.content = content
        self.inputs: list[MemoryDreamInput] = []

    async def run(self, value: MemoryDreamInput) -> MemoryDreamResult:
        self.inputs.append(value)
        return MemoryDreamResult(content=self.content, replaced=True)


async def _claim_dream(
    factory,
    *,
    worker_id: uuid.UUID,
    now: datetime,
) -> JobClaim:
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
async def test_postgres_dream_serializes_oldest_twenty_and_settles_atomically(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = MemoryDocumentScope(
        project_id=uuid.UUID(seed.owner_a.resource_scope.project_id),
        owner_user_id=seed.owner_a.resource_scope.owner_user_id,
    )
    base_time = datetime.now(UTC)
    model_id = uuid.uuid4()
    model_version_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    checksum = "a" * 64
    frozen = MemoryDreamFrozenRuntime(
        preference_version=1,
        policy_revision=7,
        model_config_id=model_id,
        model_version_id=model_version_id,
        model_payload_checksum=checksum,
        prompt_version=DREAM_PROMPT_VERSION,
    )
    history_text = {index: f"- [durable] history-{index:02d}" for index in range(1, 26)}
    try:
        async with seed.engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO system_model_configs
                    (id,logical_name,display_name,description,status,
                     current_version_id,revision,sort_order,created_by_user_id,
                     updated_by_user_id)
                    VALUES (:id,:name,'Dream test','PostgreSQL Dream test',
                            'active',NULL,1,0,:owner,:owner)"""
                ),
                {
                    "id": model_id,
                    "name": f"dream-test-{model_id.hex}",
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
                    VALUES (:id,:model,1,'codex_cli','dream-test',
                            '{}'::jsonb,false,false,false,NULL,NULL,NULL,
                            :checksum,NULL,:owner)"""
                ),
                {
                    "id": model_version_id,
                    "model": model_id,
                    "checksum": checksum,
                    "owner": scope.owner_user_id,
                },
            )
            await connection.execute(
                text(
                    """UPDATE system_model_configs
                    SET current_version_id=:version WHERE id=:model"""
                ),
                {"version": model_version_id, "model": model_id},
            )

        async with seed.factory() as session, session.begin():
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="memory-dream-test",
                    capabilities_json=["memory_dream"],
                    max_concurrent_jobs=1,
                    draining=False,
                    started_at=base_time,
                    heartbeat_at=base_time,
                )
            )
            for index, tagged_text in history_text.items():
                session.add(
                    MemoryHistoryEntryRow(
                        id=uuid.uuid4(),
                        project_id=scope.project_id,
                        owner_user_id=scope.owner_user_id,
                        namespace=scope.namespace,
                        thread_id="memory-dream-pg",
                        source_checkpoint_id=f"source-{index}",
                        committed_checkpoint_id=f"committed-{index}",
                        source_digest=hashlib.sha256(f"source-{index}".encode()).hexdigest(),
                        status="pending",
                        tagged_text=tagged_text,
                        content_digest=hashlib.sha256(tagged_text.encode()).hexdigest(),
                        preference_version=1,
                        snip_prompt_version="snip-prompt-v1",
                        summary_model_ref=model_version_id,
                        created_at=base_time + timedelta(microseconds=index),
                    )
                )

        async def admit(
            *,
            trigger: MemoryDreamTrigger = "manual_dream",
            now: datetime = base_time,
        ):
            async with seed.factory() as session, session.begin():
                sections_policy_version_id = await _memory_document_policy_version_id(session)
                return await MemoryDocumentRepository(
                    session,
                    jobs=_jobs(session),
                ).admit_dream(
                    scope,
                    trigger=trigger,
                    frozen=frozen,
                    initial_content=EMPTY_MEMORY_DOCUMENT,
                    initial_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
                    sections_policy_version_id=sections_policy_version_id,
                    now=now,
                )

        first, second = await asyncio.gather(admit(), admit())
        dispositions = {first.disposition, second.disposition}
        assert dispositions == {"queued", "already_running"}
        assert first.job_id == second.job_id
        assert first.history_count == second.history_count == 20
        job_id = first.job_id
        assert job_id is not None

        async with seed.factory() as session:
            processing = tuple((await session.execute(sa.select(MemoryHistoryEntryRow).where(MemoryHistoryEntryRow.status == "processing").order_by(MemoryHistoryEntryRow.sequence))).scalars())
            pending = tuple((await session.execute(sa.select(MemoryHistoryEntryRow).where(MemoryHistoryEntryRow.status == "pending").order_by(MemoryHistoryEntryRow.sequence))).scalars())
            run = await session.get(MemoryDreamRunRow, job_id)
            assert [row.tagged_text for row in processing] == [history_text[index] for index in range(1, 21)]
            assert [row.tagged_text for row in pending] == [history_text[index] for index in range(21, 26)]
            assert run is not None
            assert (run.history_from, run.history_to, run.history_count) == (
                processing[0].sequence,
                processing[-1].sequence,
                20,
            )
            history_digest = run.history_digest
            history_to = run.history_to

        claim = await _claim_dream(
            seed.factory,
            worker_id=worker_id,
            now=base_time + timedelta(seconds=1),
        )
        assert claim.job_id == job_id
        changed_content = EMPTY_MEMORY_DOCUMENT.replace(
            "# 项目背景",
            "# 项目背景\n\n- 当前项目使用 PostgreSQL。",
        )

        with pytest.raises(MemoryDocumentConflict):
            async with seed.factory() as session, session.begin():
                await MemoryDocumentRepository(
                    session,
                    jobs=_jobs(session),
                ).finalize_dream(
                    scope,
                    job_id=job_id,
                    lease_token="wrong-lease",
                    expected_history_digest=history_digest,
                    expected_base_version=0,
                    expected_base_digest=memory_document_digest(EMPTY_MEMORY_DOCUMENT),
                    expected_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
                    content=changed_content,
                    now=base_time + timedelta(seconds=2),
                )

        async with seed.factory() as session:
            document = await session.get(
                MemoryDocumentRow,
                (scope.project_id, scope.owner_user_id, scope.namespace),
            )
            assert document is not None
            assert (document.version, document.dream_cursor) == (0, 0)
            assert document.active_dream_job_id == job_id
            assert await session.scalar(sa.select(sa.func.count()).select_from(MemoryDocumentVersionRow)) == 0
            assert await session.scalar(sa.select(JobRow.status).where(JobRow.id == job_id)) == "running"
            assert await session.scalar(sa.select(sa.func.count()).select_from(MemoryHistoryEntryRow).where(MemoryHistoryEntryRow.status == "processing")) == 20

        async with seed.factory() as session, session.begin():
            version = await MemoryDocumentRepository(
                session,
                jobs=_jobs(session),
            ).finalize_dream(
                scope,
                job_id=job_id,
                lease_token=claim.lease_token,
                expected_history_digest=history_digest,
                expected_base_version=0,
                expected_base_digest=memory_document_digest(EMPTY_MEMORY_DOCUMENT),
                expected_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
                content=changed_content,
                now=base_time + timedelta(seconds=2),
            )
            assert version.version == 1
            assert version.unified_diff.startswith("--- memory-before.md")

        async with seed.factory() as session:
            document = await session.get(
                MemoryDocumentRow,
                (scope.project_id, scope.owner_user_id, scope.namespace),
            )
            version = await session.get(
                MemoryDocumentVersionRow,
                (scope.project_id, scope.owner_user_id, scope.namespace, 1),
            )
            consumed = tuple((await session.execute(sa.select(MemoryHistoryEntryRow).where(MemoryHistoryEntryRow.dream_job_id == job_id).order_by(MemoryHistoryEntryRow.sequence))).scalars())
            job = await session.get(JobRow, job_id)
            attempt = (await session.execute(sa.select(JobAttemptRow).where(JobAttemptRow.job_id == job_id))).scalar_one()
            assert document is not None and version is not None and job is not None
            assert document.content == version.content == changed_content
            assert document.version == 1
            assert document.dream_cursor == history_to
            assert document.active_dream_job_id is None
            assert version.unified_diff.startswith("--- memory-before.md")
            assert all(row.status == "consumed" and row.tagged_text is None and row.consumed_at is not None for row in consumed)
            assert job.status == "succeeded"
            assert attempt.outcome == "succeeded"

        failure_admission = await admit(
            trigger="auto_dream",
            now=base_time + timedelta(seconds=3),
        )
        assert failure_admission.disposition == "queued"
        assert failure_admission.history_count == 5
        failure_job_id = failure_admission.job_id
        assert failure_job_id is not None
        failure_claim = await _claim_dream(
            seed.factory,
            worker_id=worker_id,
            now=base_time + timedelta(seconds=3),
        )
        assert failure_claim.job_id == failure_job_id

        async with seed.factory() as session, session.begin():
            await MemoryDocumentRepository(
                session,
                jobs=_jobs(session),
            ).release_dream(
                scope,
                job_id=failure_job_id,
                lease_token=failure_claim.lease_token,
                now=base_time + timedelta(seconds=4),
                cancelled=False,
                public_error_code="MEMORY_DREAM_MODEL_FAILED",
                retry_initial_seconds=1,
                retry_max_seconds=1,
            )

        async with seed.factory() as session:
            document = await session.get(
                MemoryDocumentRow,
                (scope.project_id, scope.owner_user_id, scope.namespace),
            )
            retry_job = await session.get(JobRow, failure_job_id)
            assert document is not None and retry_job is not None
            assert document.active_dream_job_id == failure_job_id
            assert retry_job.status == "retry_wait"
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(MemoryHistoryEntryRow)
                    .where(
                        MemoryHistoryEntryRow.dream_job_id == failure_job_id,
                        MemoryHistoryEntryRow.status == "processing",
                    )
                )
                == 5
            )

        for attempt_index in (2, 3):
            retry_claim = await _claim_dream(
                seed.factory,
                worker_id=worker_id,
                now=base_time + timedelta(seconds=4 + attempt_index * 2),
            )
            assert retry_claim.job_id == failure_job_id
            async with seed.factory() as session, session.begin():
                await MemoryDocumentRepository(
                    session,
                    jobs=_jobs(session),
                ).release_dream(
                    scope,
                    job_id=failure_job_id,
                    lease_token=retry_claim.lease_token,
                    now=base_time + timedelta(seconds=5 + attempt_index * 2),
                    cancelled=False,
                    public_error_code="MEMORY_DREAM_MODEL_FAILED",
                    retry_initial_seconds=1,
                    retry_max_seconds=1,
                )

        async with seed.factory() as session:
            document = await session.get(
                MemoryDocumentRow,
                (scope.project_id, scope.owner_user_id, scope.namespace),
            )
            failed_job = await session.get(JobRow, failure_job_id)
            failed_rows = tuple(
                (
                    await session.execute(
                        sa.select(MemoryHistoryEntryRow)
                        .where(
                            MemoryHistoryEntryRow.sequence > history_to,
                        )
                        .order_by(MemoryHistoryEntryRow.sequence)
                    )
                ).scalars()
            )
            assert document is not None and failed_job is not None
            assert document.content == changed_content
            assert document.version == 1
            assert document.active_dream_job_id is None
            assert failed_job.status == "dead"
            assert [row.tagged_text for row in failed_rows] == [history_text[index] for index in range(21, 26)]
            assert all(row.status == "pending" and row.dream_job_id is None for row in failed_rows)

        cooldown_probe = base_time + timedelta(seconds=12)
        async with seed.factory() as session:
            repository = MemoryDocumentRepository(session, jobs=_jobs(session))
            assert not await repository.is_scope_due(
                scope,
                now=cooldown_probe,
                interval_minutes=15,
            )
            assert scope not in await repository.list_due_scopes(
                now=cooldown_probe,
                interval_minutes=15,
            )

        cancelled_admission = await admit(
            trigger="manual_dream",
            now=cooldown_probe,
        )
        assert cancelled_admission.disposition == "queued"
        cancelled_job_id = cancelled_admission.job_id
        assert cancelled_job_id is not None
        cancelled_claim = await _claim_dream(
            seed.factory,
            worker_id=worker_id,
            now=base_time + timedelta(seconds=20),
        )
        async with seed.factory() as session, session.begin():
            await MemoryDocumentRepository(
                session,
                jobs=_jobs(session),
            ).release_dream(
                scope,
                job_id=cancelled_job_id,
                lease_token=cancelled_claim.lease_token,
                now=base_time + timedelta(seconds=21),
                cancelled=True,
            )

        async with seed.factory() as session:
            document = await session.get(
                MemoryDocumentRow,
                (scope.project_id, scope.owner_user_id, scope.namespace),
            )
            cancelled_job = await session.get(JobRow, cancelled_job_id)
            pending_rows = tuple((await session.execute(sa.select(MemoryHistoryEntryRow).where(MemoryHistoryEntryRow.sequence > history_to).order_by(MemoryHistoryEntryRow.sequence))).scalars())
            assert document is not None and cancelled_job is not None
            assert document.content == changed_content
            assert document.active_dream_job_id is None
            assert cancelled_job.status == "cancelled"
            assert [row.tagged_text for row in pending_rows] == [history_text[index] for index in range(21, 26)]
            assert all(row.status == "pending" and row.dream_job_id is None for row in pending_rows)

        interval_elapsed = base_time + timedelta(minutes=15, seconds=21)
        async with seed.factory() as session:
            repository = MemoryDocumentRepository(session, jobs=_jobs(session))
            assert await repository.is_scope_due(
                scope,
                now=interval_elapsed,
                interval_minutes=15,
            )
            assert scope in await repository.list_due_scopes(
                now=interval_elapsed,
                interval_minutes=15,
            )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_scheduler_and_worker_settlement_share_one_deadlock_free_lock_order(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    worker_scope = MemoryDocumentScope(
        project_id=uuid.UUID(seed.owner_a.resource_scope.project_id),
        owner_user_id=seed.owner_a.resource_scope.owner_user_id,
    )
    scheduled_scope = MemoryDocumentScope(
        project_id=worker_scope.project_id,
        owner_user_id=seed.owner_b.resource_scope.owner_user_id,
    )
    now = datetime.now(UTC)
    worker_id = uuid.uuid4()
    model_id = uuid.uuid4()
    model_version_id = uuid.uuid4()
    model_checksum = "d" * 64
    model_name = f"dream-lock-order-{model_id.hex}"
    policy_revision = 2
    worker_project_locked = asyncio.Event()
    release_worker = asyncio.Event()
    scheduler_scope_started = asyncio.Event()
    backend_pids: dict[str, int] = {}
    worker_task: asyncio.Task[None] | None = None
    scheduler_task: asyncio.Task[int] | None = None

    policy_value = default_policy_value(
        RuntimePolicySection.AGENT_RUNTIME,
    ).model_dump(mode="python")
    memory_policy = policy_value["memory"]
    assert isinstance(memory_policy, dict)
    memory_policy.update(
        {
            "model_name": model_name,
            "dream_interval_minutes": 15,
        }
    )
    max_injection_tokens = int(memory_policy["max_injection_tokens"])
    canonical_policy = canonical_policy_payload(
        RuntimePolicySection.AGENT_RUNTIME,
        policy_value,
    )
    frozen = MemoryDreamFrozenRuntime(
        preference_version=1,
        policy_revision=policy_revision,
        model_config_id=model_id,
        model_version_id=model_version_id,
        model_payload_checksum=model_checksum,
        prompt_version=DREAM_PROMPT_VERSION,
    )
    worker_tagged_text = "- [durable] worker settlement lock order"
    scheduled_tagged_text = "- [durable] scheduler admission lock order"
    changed_content = EMPTY_MEMORY_DOCUMENT.replace(
        "# 项目背景",
        "# 项目背景\n\n- Dream lock ordering is deterministic.",
    )

    class CoordinatedAdmission(MemoryDreamAdmissionService):
        async def admit_scheduled_scope(self, session, scope, *, now, require_due=True):
            backend_pids["scheduler"] = int(await session.scalar(text("SELECT pg_backend_pid()")))
            scheduler_scope_started.set()
            return await super().admit_scheduled_scope(
                session,
                scope,
                now=now,
                require_due=require_due,
            )

    async def coordinated_scope_validator(session, claim, *, lock):
        allowed = await memory_dream_worker_module._default_scope_validator(
            session,
            claim,
            lock=lock,
        )
        if lock:
            backend_pids["worker"] = int(await session.scalar(text("SELECT pg_backend_pid()")))
            worker_project_locked.set()
            await release_worker.wait()
        return allowed

    try:
        async with seed.engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO system_model_configs
                    (id,logical_name,display_name,description,status,
                     current_version_id,revision,sort_order,created_by_user_id,
                     updated_by_user_id)
                    VALUES (:id,:name,'Dream lock-order model',
                            'PostgreSQL Dream lock-order test','active',
                            NULL,1,0,:owner,:owner)"""
                ),
                {
                    "id": model_id,
                    "name": model_name,
                    "owner": worker_scope.owner_user_id,
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
                    VALUES (:id,:model,1,'codex_cli','dream-lock-order',
                            '{}'::jsonb,false,false,false,NULL,NULL,NULL,
                            :checksum,NULL,:owner)"""
                ),
                {
                    "id": model_version_id,
                    "model": model_id,
                    "checksum": model_checksum,
                    "owner": worker_scope.owner_user_id,
                },
            )
            await connection.execute(
                text(
                    """UPDATE system_model_configs
                    SET current_version_id=:version WHERE id=:model"""
                ),
                {"version": model_version_id, "model": model_id},
            )

        async with seed.factory() as session, session.begin():
            policies = SystemRuntimePolicyRepository(session)
            state = await policies.catalog_state(for_update=True)
            policy, previous = await policies.current(
                RuntimePolicySection.AGENT_RUNTIME,
                for_update=True,
            )
            await policies.add_version(
                policy,
                SystemRuntimePolicyVersionRow(
                    id=uuid.uuid4(),
                    section=RuntimePolicySection.AGENT_RUNTIME.value,
                    version_number=policy_revision,
                    schema_version=canonical_policy.schema_version,
                    value=canonical_policy.value,
                    payload_checksum=canonical_policy.checksum,
                    supersedes_version_id=previous.id,
                    created_by_user_id=worker_scope.owner_user_id,
                    created_at=now,
                ),
            )
            policy.revision = policy_revision
            policy.updated_by_user_id = worker_scope.owner_user_id
            policy.updated_at = now
            state.revision = int(state.revision) + 1
            state.updated_by_user_id = worker_scope.owner_user_id
            state.updated_at = now
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="memory-dream-lock-order-test",
                    capabilities_json=["memory_dream"],
                    max_concurrent_jobs=1,
                    draining=False,
                    started_at=now,
                    heartbeat_at=now,
                )
            )
            for scope, thread_id, tagged_text in (
                (
                    worker_scope,
                    "memory-dream-worker-lock-order",
                    worker_tagged_text,
                ),
                (
                    scheduled_scope,
                    "memory-dream-scheduler-lock-order",
                    scheduled_tagged_text,
                ),
            ):
                session.add(
                    MemoryHistoryEntryRow(
                        id=uuid.uuid4(),
                        project_id=scope.project_id,
                        owner_user_id=scope.owner_user_id,
                        namespace=scope.namespace,
                        thread_id=thread_id,
                        source_checkpoint_id=f"source-{thread_id}",
                        committed_checkpoint_id=f"committed-{thread_id}",
                        source_digest=hashlib.sha256(thread_id.encode()).hexdigest(),
                        status="pending",
                        tagged_text=tagged_text,
                        content_digest=hashlib.sha256(tagged_text.encode()).hexdigest(),
                        preference_version=1,
                        snip_prompt_version="snip-prompt-v1",
                        summary_model_ref=model_version_id,
                        created_at=now - timedelta(hours=1),
                    )
                )

        async with seed.factory() as session, session.begin():
            sections_policy_version_id = await _memory_document_policy_version_id(session)
            admission = await MemoryDocumentRepository(
                session,
                jobs=_jobs(session),
            ).admit_dream(
                worker_scope,
                trigger="manual_dream",
                frozen=frozen,
                initial_content=EMPTY_MEMORY_DOCUMENT,
                initial_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
                sections_policy_version_id=sections_policy_version_id,
                now=now - timedelta(minutes=30),
            )
        assert admission.disposition == "queued"
        assert admission.job_id is not None
        claim = await _claim_dream(
            seed.factory,
            worker_id=worker_id,
            # JobRepository stamps availability from the real clock. Advance
            # the deterministic claim clock past that insertion instant.
            now=now + timedelta(seconds=1),
        )
        assert claim.job_id == admission.job_id
        async with seed.factory() as session:
            work = await MemoryDocumentRepository(
                session,
                jobs=_jobs(session),
            ).load_dream_work(worker_scope, claim.job_id)
        assert work is not None

        handler = MemoryDreamJobHandler(
            seed.factory,
            app_config=None,
            runner_factory=lambda _model: _RecordingDreamRunner(changed_content),
            job_repository_builder=_jobs,
            scope_validator=coordinated_scope_validator,
        )
        settlement = handler._success_settlement(
            claim,
            work=work,
            content=changed_content,
            max_tokens=max_injection_tokens,
            episode_retention_days=0,
        )
        scheduler = MemoryDreamSchedulerService(
            seed.factory,
            admission=CoordinatedAdmission(job_repository_builder=_jobs),
        )

        worker_task = asyncio.create_task(settlement.commit())
        await asyncio.wait_for(worker_project_locked.wait(), timeout=2)
        scheduler_task = asyncio.create_task(scheduler.admit_due(now=now))
        await asyncio.wait_for(scheduler_scope_started.wait(), timeout=2)
        release_worker.set()
        worker_result, admitted_count = await asyncio.wait_for(
            asyncio.gather(worker_task, scheduler_task),
            timeout=5,
        )

        assert worker_result is None
        assert admitted_count == 1
        assert backend_pids["worker"] != backend_pids["scheduler"]
        async with seed.factory() as session:
            worker_document = await session.get(
                MemoryDocumentRow,
                (
                    worker_scope.project_id,
                    worker_scope.owner_user_id,
                    worker_scope.namespace,
                ),
            )
            scheduled_document = await session.get(
                MemoryDocumentRow,
                (
                    scheduled_scope.project_id,
                    scheduled_scope.owner_user_id,
                    scheduled_scope.namespace,
                ),
            )
            worker_job = await session.get(JobRow, claim.job_id)
        assert worker_document is not None
        assert worker_document.version == 1
        assert worker_document.content == changed_content
        assert worker_job is not None and worker_job.status == "succeeded"
        assert scheduled_document is not None
        assert scheduled_document.active_dream_job_id is not None
    finally:
        release_worker.set()
        for task in (worker_task, scheduler_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (worker_task, scheduler_task) if task is not None),
            return_exceptions=True,
        )
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_postgres_dream_settlement_cancels_when_active_model_version_drifts(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = MemoryDocumentScope(
        project_id=uuid.UUID(seed.owner_a.resource_scope.project_id),
        owner_user_id=seed.owner_a.resource_scope.owner_user_id,
    )
    base_time = datetime.now(UTC)
    worker_id = uuid.uuid4()
    model_id = uuid.uuid4()
    frozen_model_version_id = uuid.uuid4()
    drifted_model_version_id = uuid.uuid4()
    frozen_checksum = "a" * 64
    drifted_checksum = "b" * 64
    model_name = f"dream-settlement-{model_id.hex}"
    policy_revision = 2
    tagged_text = "- [durable] Remember the settlement boundary."
    changed_content = EMPTY_MEMORY_DOCUMENT.replace(
        "# 项目背景",
        "# 项目背景\n\n- Dream 模型已经完成执行。",
    )
    runner = _RecordingDreamRunner(changed_content)

    policy_value = default_policy_value(
        RuntimePolicySection.AGENT_RUNTIME,
    ).model_dump(mode="python")
    memory_policy = policy_value["memory"]
    assert isinstance(memory_policy, dict)
    memory_policy["model_name"] = model_name
    canonical_policy = canonical_policy_payload(
        RuntimePolicySection.AGENT_RUNTIME,
        policy_value,
    )
    frozen = MemoryDreamFrozenRuntime(
        preference_version=1,
        policy_revision=policy_revision,
        model_config_id=model_id,
        model_version_id=frozen_model_version_id,
        model_payload_checksum=frozen_checksum,
        prompt_version=DREAM_PROMPT_VERSION,
    )

    try:
        async with seed.engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO system_model_configs
                    (id,logical_name,display_name,description,status,
                     current_version_id,revision,sort_order,created_by_user_id,
                     updated_by_user_id)
                    VALUES (:id,:name,'Dream settlement model',
                            'PostgreSQL Dream settlement drift test','active',
                            NULL,1,0,:owner,:owner)"""
                ),
                {
                    "id": model_id,
                    "name": model_name,
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
                    VALUES (:id,:model,1,'codex_cli','dream-frozen',
                            '{}'::jsonb,false,false,false,NULL,NULL,NULL,
                            :checksum,NULL,:owner)"""
                ),
                {
                    "id": frozen_model_version_id,
                    "model": model_id,
                    "checksum": frozen_checksum,
                    "owner": scope.owner_user_id,
                },
            )
            await connection.execute(
                text(
                    """UPDATE system_model_configs
                    SET current_version_id=:version WHERE id=:model"""
                ),
                {"version": frozen_model_version_id, "model": model_id},
            )

        async with seed.factory() as session, session.begin():
            policies = SystemRuntimePolicyRepository(session)
            state = await policies.catalog_state(for_update=True)
            policy, previous = await policies.current(
                RuntimePolicySection.AGENT_RUNTIME,
                for_update=True,
            )
            assert int(policy.revision) == 1
            now = datetime.now(UTC)
            await policies.add_version(
                policy,
                SystemRuntimePolicyVersionRow(
                    id=uuid.uuid4(),
                    section=RuntimePolicySection.AGENT_RUNTIME.value,
                    version_number=policy_revision,
                    schema_version=canonical_policy.schema_version,
                    value=canonical_policy.value,
                    payload_checksum=canonical_policy.checksum,
                    supersedes_version_id=previous.id,
                    created_by_user_id=scope.owner_user_id,
                    created_at=now,
                ),
            )
            policy.revision = policy_revision
            policy.updated_by_user_id = scope.owner_user_id
            policy.updated_at = now
            state.revision = int(state.revision) + 1
            state.updated_by_user_id = scope.owner_user_id
            state.updated_at = now
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="memory-dream-model-drift-test",
                    capabilities_json=["memory_dream"],
                    max_concurrent_jobs=1,
                    draining=False,
                    started_at=base_time,
                    heartbeat_at=base_time,
                )
            )
            session.add(
                MemoryHistoryEntryRow(
                    id=uuid.uuid4(),
                    project_id=scope.project_id,
                    owner_user_id=scope.owner_user_id,
                    namespace=scope.namespace,
                    thread_id="memory-dream-model-drift-pg",
                    source_checkpoint_id="source-model-drift",
                    committed_checkpoint_id="committed-model-drift",
                    source_digest=hashlib.sha256(b"source-model-drift").hexdigest(),
                    status="pending",
                    tagged_text=tagged_text,
                    content_digest=hashlib.sha256(tagged_text.encode()).hexdigest(),
                    preference_version=1,
                    snip_prompt_version="snip-prompt-v1",
                    summary_model_ref=frozen_model_version_id,
                    created_at=base_time,
                )
            )

        async with seed.factory() as session, session.begin():
            sections_policy_version_id = await _memory_document_policy_version_id(session)
            admission = await MemoryDocumentRepository(
                session,
                jobs=_jobs(session),
            ).admit_dream(
                scope,
                trigger="manual_dream",
                frozen=frozen,
                initial_content=EMPTY_MEMORY_DOCUMENT,
                initial_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
                sections_policy_version_id=sections_policy_version_id,
                now=base_time,
            )
        assert admission.disposition == "queued"
        assert admission.history_count == 1
        job_id = admission.job_id
        assert job_id is not None

        claim = await _claim_dream(
            seed.factory,
            worker_id=worker_id,
            now=base_time + timedelta(seconds=1),
        )
        assert claim.job_id == job_id
        handler = MemoryDreamJobHandler(
            seed.factory,
            app_config=None,
            runner_factory=lambda _model: runner,
            job_repository_builder=_jobs,
        )
        authority = JobLeaseAuthority(
            seed.factory,
            claim,
            lease_seconds=60,
            repository_builder=_jobs,
        )

        settlement = await handler(claim, authority)
        assert isinstance(settlement, JobSettlement)
        assert settlement.outcome.status == "succeeded"
        assert len(runner.inputs) == 1
        assert runner.inputs[0].history[0].tagged_text == tagged_text

        async with seed.engine.begin() as connection:
            policy_revision_before_drift = int(
                await connection.scalar(
                    text(
                        """SELECT revision FROM system_runtime_policies
                        WHERE section='agent_runtime'"""
                    )
                )
            )
            await connection.execute(
                text(
                    """INSERT INTO system_model_config_versions
                    (id,model_config_id,version_number,provider_adapter,
                     provider_model,settings,supports_thinking,
                     supports_reasoning_effort,supports_vision,credential_id,
                     credential_version_id,credential_env_key,payload_checksum,
                     supersedes_version_id,created_by_user_id)
                    VALUES (:id,:model,2,'codex_cli','dream-drifted',
                            '{}'::jsonb,false,false,false,NULL,NULL,NULL,
                            :checksum,:supersedes,:owner)"""
                ),
                {
                    "id": drifted_model_version_id,
                    "model": model_id,
                    "checksum": drifted_checksum,
                    "supersedes": frozen_model_version_id,
                    "owner": scope.owner_user_id,
                },
            )
            await connection.execute(
                text(
                    """UPDATE system_model_configs
                    SET current_version_id=:version,revision=2,
                        updated_by_user_id=:owner,updated_at=now()
                    WHERE id=:model"""
                ),
                {
                    "version": drifted_model_version_id,
                    "owner": scope.owner_user_id,
                    "model": model_id,
                },
            )
            await connection.execute(
                text(
                    """UPDATE system_model_catalog_state
                    SET revision=revision+1,updated_by_user_id=:owner,
                        updated_at=now() WHERE id=1"""
                ),
                {"owner": scope.owner_user_id},
            )
            current_model = (
                await connection.execute(
                    text(
                        """SELECT c.current_version_id,v.payload_checksum
                        FROM system_model_configs AS c
                        JOIN system_model_config_versions AS v
                          ON v.id=c.current_version_id
                         AND v.model_config_id=c.id
                        WHERE c.id=:model"""
                    ),
                    {"model": model_id},
                )
            ).one()
            policy_revision_after_drift = int(
                await connection.scalar(
                    text(
                        """SELECT revision FROM system_runtime_policies
                        WHERE section='agent_runtime'"""
                    )
                )
            )
        assert policy_revision_before_drift == policy_revision
        assert policy_revision_after_drift == policy_revision
        assert current_model.current_version_id == drifted_model_version_id
        assert current_model.payload_checksum == drifted_checksum

        await settlement.commit()

        async with seed.factory() as session:
            document = await session.get(
                MemoryDocumentRow,
                (scope.project_id, scope.owner_user_id, scope.namespace),
            )
            history = (
                await session.execute(
                    sa.select(MemoryHistoryEntryRow).where(
                        MemoryHistoryEntryRow.project_id == scope.project_id,
                        MemoryHistoryEntryRow.owner_user_id == scope.owner_user_id,
                        MemoryHistoryEntryRow.namespace == scope.namespace,
                    )
                )
            ).scalar_one()
            dream_run = await session.get(MemoryDreamRunRow, job_id)
            job = await session.get(JobRow, job_id)
            attempt = (await session.execute(sa.select(JobAttemptRow).where(JobAttemptRow.job_id == job_id))).scalar_one()
            version_count = int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(MemoryDocumentVersionRow)
                    .where(
                        MemoryDocumentVersionRow.project_id == scope.project_id,
                        MemoryDocumentVersionRow.owner_user_id == scope.owner_user_id,
                        MemoryDocumentVersionRow.namespace == scope.namespace,
                    )
                )
                or 0
            )
            current_policy_revision = int(
                await session.scalar(
                    text(
                        """SELECT revision FROM system_runtime_policies
                        WHERE section='agent_runtime'"""
                    )
                )
            )

        assert document is not None
        assert dream_run is not None
        assert job is not None
        assert document.content == EMPTY_MEMORY_DOCUMENT
        assert document.version == 0
        assert document.dream_cursor == 0
        assert document.active_dream_job_id is None
        assert version_count == 0
        assert dream_run.result_version is None
        assert history.status == "pending"
        assert history.dream_job_id is None
        assert history.tagged_text == tagged_text
        assert job.status == "cancelled"
        assert attempt.outcome == "cancelled"
        assert current_policy_revision == policy_revision
    finally:
        await seed.engine.dispose()
