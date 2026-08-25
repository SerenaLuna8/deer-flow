from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.engine import make_url
from support.private_thread_seed import PrivateThreadSeed, seed_private_thread_database
from support.run_closure import add_sealed_test_run
from support.system_model_seed import (
    seed_system_model_config,
    system_model_payload_checksum,
)

import deerflow.runtime.checkpoint_mode as checkpoint_mode_state
from app.audit.models import resolve_system_audit_context
from app.audit.service import AuditService
from app.private_work.chat_controls import ProjectChatControlService
from app.private_work.checkpoint_state import (
    bind_scoped_checkpoint_state,
    checkpoint_config,
    snapshot_checkpoint_id,
)
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.memory_seal_service import (
    MemorySealAdmissionService,
    MemorySealSchedulerService,
)
from app.private_work.thread_repository import PrivateThreadRepository
from app.private_work.thread_service import PrivateThreadService
from app.reliability.owner_refs import AuditHmacKeyring
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    RuntimePolicySection,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.worker.memory_seal import MemorySealJobHandler
from app.worker.service import JobLeaseAuthority, JobSettlement
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import JobClaim, JobRepository
from deerflow.persistence.private_work.memory_document_model import (
    MemoryHistoryEntryRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow

_CONTINUITY = "The sealed thread remains available through compacted continuity."
_TAGGED_TEXT = "- [durable] Idle sealing archives completed Thread turns before Dream"


def _reset_checkpoint_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        checkpoint_mode_state,
        "_frozen_checkpoint_channel_mode",
        None,
    )
    monkeypatch.setattr(
        checkpoint_mode_state,
        "_frozen_checkpoint_snapshot_frequency",
        None,
    )


def _checkpointer_url(database_url: str) -> str:
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


async def _seed_summary_model(seed: PrivateThreadSeed) -> ModelConfig:
    model_id = uuid.uuid4()
    model_name = str(model_id)
    async with seed.engine.begin() as connection:
        await seed_system_model_config(
            connection,
            model_id=model_id,
            owner_user_id=str(seed.owner_a.user_id),
            display_name="Memory seal test model",
            provider_model="memory-seal-test",
        )

    runtime_model = ModelConfig(
        name=model_name,
        display_name=None,
        description=None,
        use="support.fake_models:GovernedFakeListChatModel",
        model=model_name,
        max_input_tokens=64_000,
        responses=[
            f"<continuity>\n{_CONTINUITY}\n</continuity>\n{_TAGGED_TEXT}",
        ],
        custom_get_token_ids=lambda value: list(range(len(value))),
    )
    runtime_model._system_model_config_id = model_id
    runtime_model._system_model_payload_checksum = system_model_payload_checksum(
        model_id=model_id,
        provider_adapter="vision_bridge_fake",
        provider_model="memory-seal-test",
        settings=None,
        supports_thinking=False,
        supports_reasoning_effort=False,
        supports_vision=False,
    )
    return runtime_model


async def _set_current_summary_model(
    seed: PrivateThreadSeed,
    runtime_model: ModelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind the test model through the same authoritative DB policy seam."""

    monkeypatch.setenv("ACT_WEAVE_AUDIT_ACTIVE_KEY_ID", "test-audit-v1")
    monkeypatch.setenv(
        "ACT_WEAVE_AUDIT_KEYRING_JSON",
        '{"test-audit-v1":"YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="}',
    )
    async with seed.factory() as session, session.begin():
        await session.execute(sa.update(UserRow).where(UserRow.id == str(seed.owner_a.user_id)).values(system_role="system_admin"))
    async with seed.factory() as session:
        policy, revision = await SystemRuntimePolicyMaterializer.materialize_current_with_revision_in_session(
            session,
            RuntimePolicySection.AGENT_RUNTIME,
        )
    assert isinstance(policy, AgentRuntimePolicyValue)
    updated = policy.model_copy(
        update={
            "summarization": policy.summarization.model_copy(
                update={"model_name": runtime_model.name},
            ),
            # The isolated fixture intentionally does not seed the catalog's
            # production default Vision model.
            "vision_bridge": policy.vision_bridge.model_copy(
                update={"model_name": None},
            ),
        },
    )
    service = SystemRuntimePolicyService(
        seed.factory,
        AuditService(seed.factory, AuditHmacKeyring.from_environment()),
    )
    context = resolve_system_audit_context(
        SimpleNamespace(
            id=seed.owner_a.user_id,
            system_role="system_admin",
        ),
        request_id="memory-seal-postgres-policy",
    )
    await service.update_policy(
        context,
        RuntimePolicySection.AGENT_RUNTIME,
        expected_revision=revision,
        value=updated,
    )


def _app_config(database_url: str, runtime_model: ModelConfig) -> AppConfig:
    return AppConfig.model_validate(
        {
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
            },
            "database": {
                "url": database_url,
                "checkpoint_channel_mode": "full",
            },
            "summarization": {
                "enabled": True,
                "model_name": runtime_model.name,
                "trim_tokens_to_summarize": 20_000,
            },
        }
    )


class _ModelMaterializer:
    def __init__(self, runtime_model: ModelConfig) -> None:
        self.runtime_model = runtime_model

    async def materialize_active(self, model_ref: str) -> ModelConfig:
        assert model_ref == self.runtime_model.name
        return self.runtime_model


def _barrier(
    seed: PrivateThreadSeed,
    scoped: ProjectScopedCheckpointer,
    runtime_model: ModelConfig,
) -> ProjectChatControlService:
    return ProjectChatControlService(
        seed.factory,
        scoped,
        PrivateThreadService(seed.factory, scoped),
        object(),  # type: ignore[arg-type]
        model_materializer=_ModelMaterializer(runtime_model),  # type: ignore[arg-type]
    )


async def _seed_due_thread(
    seed: PrivateThreadSeed,
    scoped: ProjectScopedCheckpointer,
    app_config: AppConfig,
    *,
    thread_id: str,
) -> tuple[str, datetime, datetime]:
    async with seed.factory() as session:
        policy, _revision = await SystemRuntimePolicyMaterializer.materialize_current_with_revision_in_session(
            session,
            RuntimePolicySection.AGENT_RUNTIME,
        )
    assert isinstance(policy, AgentRuntimePolicyValue)
    assert policy.memory.enabled is True
    assert policy.memory.idle_seal_minutes > 0

    admitted_at = datetime.now(UTC)
    idle_at = admitted_at - timedelta(
        minutes=policy.memory.idle_seal_minutes + 5,
    )
    async with seed.factory() as session, session.begin():
        session.add(
            ThreadMetaRow(
                thread_id=thread_id,
                assistant_id=str(seed.project_agent_id),
                owner_user_id=str(seed.owner_a.user_id),
                display_name="Memory seal PostgreSQL closure",
                status="idle",
                metadata_json={},
                created_at=idle_at,
                updated_at=idle_at,
                project_id=seed.owner_a.project_id,
                agent_asset_id=seed.project_agent_id,
                agent_scope="project",
            )
        )
        await session.flush()
        await add_sealed_test_run(
            session,
            RunRow(
                run_id=f"settled-{uuid.uuid4().hex}",
                thread_id=thread_id,
                assistant_id=str(seed.project_agent_id),
                owner_user_id=str(seed.owner_a.user_id),
                status="success",
                model_name=app_config.summarization.model_name,
                multitask_strategy="reject",
                metadata_json={},
                kwargs_json={},
                origin_trace_id=uuid.uuid4().hex,
                project_id=seed.owner_a.project_id,
                finalization_status="complete",
                created_at=idle_at,
                updated_at=idle_at,
            ),
        )

    state = bind_scoped_checkpoint_state(
        scoped,
        seed.owner_a,
        app_config,
        as_node="memory_seal_postgres_test",
    )
    await state.aupdate(
        checkpoint_config(thread_id),
        {
            "messages": [
                HumanMessage(
                    id=f"human-{thread_id}",
                    content="Archive this completed idle turn.",
                ),
                AIMessage(
                    id=f"ai-{thread_id}",
                    content="The idle seal will retain continuity and Memory facts.",
                ),
            ],
        },
        as_node="memory_seal_postgres_test",
    )
    source = await state.aget(checkpoint_config(thread_id))
    source_checkpoint_id = snapshot_checkpoint_id(source)
    assert source_checkpoint_id is not None
    return source_checkpoint_id, idle_at, admitted_at


async def _admit_and_claim(
    seed: PrivateThreadSeed,
    *,
    thread_id: str,
    admitted_at: datetime,
) -> JobClaim:
    worker_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="memory-seal-postgres-test",
                capabilities_json=["memory_seal"],
                max_concurrent_jobs=1,
                started_at=admitted_at,
                heartbeat_at=admitted_at,
            )
        )

    async with seed.factory() as session:
        candidates = await MemorySealAdmissionService().list_due_threads(
            session,
            now=admitted_at,
        )
    assert candidates == (
        (
            seed.owner_a.project_id,
            str(seed.owner_a.user_id),
            thread_id,
        ),
    )

    scheduler = MemorySealSchedulerService(seed.factory)
    assert await scheduler.admit_due(now=admitted_at) == 1

    async with seed.factory() as session, session.begin():
        queued = await session.scalar(
            sa.select(JobRow).where(
                JobRow.job_type == "memory_seal",
                JobRow.project_id == seed.owner_a.project_id,
                JobRow.owner_user_id == str(seed.owner_a.user_id),
                JobRow.namespace == thread_id,
            )
        )
        assert queued is not None
        assert queued.status == "queued"
        database_now = await session.scalar(sa.select(sa.func.clock_timestamp()))
        assert isinstance(database_now, datetime)
        # Production claims sample PostgreSQL time. This closure test injects
        # one explicit logical instant at or after the row becomes available so
        # host/database skew does not turn it into a claim-clock test instead.
        claim_at = max(database_now, queued.available_at)
        assert queued.available_at <= claim_at
        jobs = JobRepository(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"memory_seal"}),
            lease_seconds=300,
            now=claim_at,
        )
        assert claim is not None
        assert claim.job_id == queued.id
        assert claim.job_type == "memory_seal"
        assert claim.namespace == thread_id
        assert await jobs.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
            now=claim_at,
        )
    return claim


async def _assert_job_succeeded(
    seed: PrivateThreadSeed,
    claim: JobClaim,
) -> None:
    async with seed.factory() as session:
        job = await session.get(JobRow, claim.job_id)
        attempt = await session.get(JobAttemptRow, claim.attempt_id)
    assert job is not None
    assert job.status == "succeeded"
    assert job.completed_at is not None
    assert attempt is not None
    assert attempt.outcome == "succeeded"


@pytest.mark.postgres
@pytest.mark.anyio
async def test_memory_seal_terminal_failure_waits_for_new_thread_activity(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_checkpoint_mode(monkeypatch)
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"seal-epoch-{uuid.uuid4().hex}"
    try:
        async with AsyncPostgresSaver.from_conn_string(_checkpointer_url(migrated_postgres_database_url)) as raw:
            await raw.setup()
            scoped = ProjectScopedCheckpointer(raw, seed.factory)
            runtime_model = await _seed_summary_model(seed)
            app_config = _app_config(
                migrated_postgres_database_url,
                runtime_model,
            )
            _source_checkpoint_id, _idle_at, admitted_at = await _seed_due_thread(
                seed,
                scoped,
                app_config,
                thread_id=thread_id,
            )
            scheduler = MemorySealSchedulerService(seed.factory)

            assert await scheduler.admit_due(now=admitted_at) == 1
            async with seed.factory() as session, session.begin():
                first_job = await session.scalar(
                    sa.select(JobRow).where(
                        JobRow.job_type == "memory_seal",
                        JobRow.project_id == seed.owner_a.project_id,
                        JobRow.owner_user_id == str(seed.owner_a.user_id),
                        JobRow.namespace == thread_id,
                    )
                )
                assert first_job is not None
                first_key = first_job.idempotency_key
                first_created_at = first_job.created_at
                await session.execute(
                    sa.update(JobRow)
                    .where(JobRow.id == first_job.id)
                    .values(
                        status="dead",
                        public_error_code="MEMORY_SEAL_COMPACTION_DISABLED",
                        completed_at=first_created_at,
                    )
                )

            assert await scheduler.admit_due(now=admitted_at) == 0
            assert await scheduler.admit_due(now=admitted_at) == 0
            async with seed.factory() as session:
                unchanged_count = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(JobRow)
                    .where(
                        JobRow.job_type == "memory_seal",
                        JobRow.project_id == seed.owner_a.project_id,
                        JobRow.owner_user_id == str(seed.owner_a.user_id),
                        JobRow.namespace == thread_id,
                    )
                )
            assert unchanged_count == 1

            activity_at = max(admitted_at, first_created_at) + timedelta(
                seconds=1,
            )
            async with seed.factory() as session, session.begin():
                await add_sealed_test_run(
                    session,
                    RunRow(
                        run_id=f"activity-{uuid.uuid4().hex}",
                        thread_id=thread_id,
                        assistant_id=str(seed.project_agent_id),
                        owner_user_id=str(seed.owner_a.user_id),
                        status="success",
                        model_name=runtime_model.name,
                        multitask_strategy="reject",
                        metadata_json={},
                        kwargs_json={},
                        origin_trace_id=uuid.uuid4().hex,
                        project_id=seed.owner_a.project_id,
                        finalization_status="complete",
                        created_at=activity_at,
                        updated_at=activity_at,
                    ),
                )
                await session.flush()
                await PrivateThreadRepository(session).touch_activity(
                    scope=seed.owner_a.resource_scope,
                    thread_id=thread_id,
                    occurred_at=activity_at,
                )

            async with seed.factory() as session:
                policy, _revision = await SystemRuntimePolicyMaterializer.materialize_current_with_revision_in_session(
                    session,
                    RuntimePolicySection.AGENT_RUNTIME,
                )
            assert isinstance(policy, AgentRuntimePolicyValue)
            next_due_at = activity_at + timedelta(
                minutes=policy.memory.idle_seal_minutes + 1,
            )

            assert await scheduler.admit_due(now=next_due_at) == 1
            async with seed.factory() as session:
                keys = tuple(
                    (
                        await session.execute(
                            sa.select(JobRow.idempotency_key)
                            .where(
                                JobRow.job_type == "memory_seal",
                                JobRow.project_id == seed.owner_a.project_id,
                                JobRow.owner_user_id == str(seed.owner_a.user_id),
                                JobRow.namespace == thread_id,
                            )
                            .order_by(JobRow.created_at, JobRow.id)
                        )
                    ).scalars()
                )
            assert len(keys) == 2
            assert keys[0] == first_key
            assert keys[1] != first_key
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_memory_seal_real_postgres_scheduler_worker_and_archive_closure(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_checkpoint_mode(monkeypatch)
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"seal-e2e-{uuid.uuid4().hex}"
    try:
        async with AsyncPostgresSaver.from_conn_string(_checkpointer_url(migrated_postgres_database_url)) as raw:
            await raw.setup()
            scoped = ProjectScopedCheckpointer(raw, seed.factory)
            runtime_model = await _seed_summary_model(seed)
            await _set_current_summary_model(seed, runtime_model, monkeypatch)
            app_config = _app_config(
                migrated_postgres_database_url,
                runtime_model,
            )
            source_checkpoint_id, idle_at, admitted_at = await _seed_due_thread(
                seed,
                scoped,
                app_config,
                thread_id=thread_id,
            )
            claim = await _admit_and_claim(
                seed,
                thread_id=thread_id,
                admitted_at=admitted_at,
            )

            handler = MemorySealJobHandler(
                seed.factory,
                app_config=app_config,
                barrier=_barrier(seed, scoped, runtime_model),
            )
            authority = JobLeaseAuthority(
                seed.factory,
                claim,
                lease_seconds=300,
            )
            outcome = await handler(claim, authority)
            assert isinstance(outcome, JobSettlement)
            assert outcome.outcome.status == "succeeded"
            await outcome.commit()

            latest = await bind_scoped_checkpoint_state(
                scoped,
                seed.owner_a,
                app_config,
                as_node="memory_seal_postgres_assert",
            ).aget(checkpoint_config(thread_id))
            committed_checkpoint_id = snapshot_checkpoint_id(latest)
            assert committed_checkpoint_id is not None
            assert committed_checkpoint_id != source_checkpoint_id
            assert latest.values["messages"] == []
            assert latest.values["summary_text"] == _CONTINUITY

            async with seed.factory() as session:
                history = tuple(
                    (
                        await session.execute(
                            sa.select(MemoryHistoryEntryRow).where(
                                MemoryHistoryEntryRow.project_id == seed.owner_a.project_id,
                                MemoryHistoryEntryRow.owner_user_id == str(seed.owner_a.user_id),
                                MemoryHistoryEntryRow.thread_id == thread_id,
                            )
                        )
                    ).scalars()
                )
                thread = await session.scalar(
                    sa.select(ThreadMetaRow).where(
                        ThreadMetaRow.project_id == seed.owner_a.project_id,
                        ThreadMetaRow.owner_user_id == str(seed.owner_a.user_id),
                        ThreadMetaRow.thread_id == thread_id,
                    )
                )
            assert len(history) == 1
            assert history[0].status == "pending"
            assert history[0].tagged_text == _TAGGED_TEXT
            assert history[0].source_checkpoint_id == source_checkpoint_id
            assert history[0].committed_checkpoint_id == committed_checkpoint_id
            assert thread is not None
            assert thread.memory_sealed_at is not None
            assert thread.updated_at == idle_at
            sealed_at = thread.memory_sealed_at
            await _assert_job_succeeded(seed, claim)

            async with seed.factory() as session, session.begin():
                await session.execute(
                    sa.update(ThreadMetaRow)
                    .where(
                        ThreadMetaRow.project_id == seed.owner_a.project_id,
                        ThreadMetaRow.owner_user_id == str(seed.owner_a.user_id),
                        ThreadMetaRow.thread_id == thread_id,
                    )
                    .values(display_name="Ordinary Thread update")
                )
            async with seed.factory() as session:
                ordinary_updated_at = await session.scalar(
                    sa.select(ThreadMetaRow.updated_at).where(
                        ThreadMetaRow.project_id == seed.owner_a.project_id,
                        ThreadMetaRow.owner_user_id == str(seed.owner_a.user_id),
                        ThreadMetaRow.thread_id == thread_id,
                    )
                )
            assert ordinary_updated_at is not None
            assert ordinary_updated_at > idle_at

            combined_sealed_at = sealed_at + timedelta(seconds=1)
            async with seed.factory() as session, session.begin():
                await session.execute(
                    sa.update(ThreadMetaRow)
                    .where(
                        ThreadMetaRow.project_id == seed.owner_a.project_id,
                        ThreadMetaRow.owner_user_id == str(seed.owner_a.user_id),
                        ThreadMetaRow.thread_id == thread_id,
                    )
                    .values(
                        memory_sealed_at=combined_sealed_at,
                        display_name="Seal plus semantic update",
                        updated_at=ThreadMetaRow.updated_at,
                    )
                )
            async with seed.factory() as session:
                combined_updated_at = await session.scalar(
                    sa.select(ThreadMetaRow.updated_at).where(
                        ThreadMetaRow.thread_id == thread_id,
                    )
                )
            assert combined_updated_at is not None
            assert combined_updated_at > ordinary_updated_at

            async with seed.factory() as session, session.begin():
                await session.execute(
                    sa.update(ThreadMetaRow)
                    .where(ThreadMetaRow.thread_id == thread_id)
                    .values(
                        memory_sealed_at=combined_sealed_at,
                        updated_at=ThreadMetaRow.updated_at,
                    )
                )
            async with seed.factory() as session:
                no_change_updated_at = await session.scalar(
                    sa.select(ThreadMetaRow.updated_at).where(
                        ThreadMetaRow.thread_id == thread_id,
                    )
                )
            assert no_change_updated_at is not None
            assert no_change_updated_at > combined_updated_at

            async with seed.factory() as session, session.begin():
                await session.execute(
                    sa.update(ThreadMetaRow)
                    .where(ThreadMetaRow.thread_id == thread_id)
                    .values(
                        memory_sealed_at=sealed_at + timedelta(seconds=2),
                        updated_at=idle_at,
                    )
                )
            async with seed.factory() as session:
                explicit_updated_at = await session.scalar(
                    sa.select(ThreadMetaRow.updated_at).where(
                        ThreadMetaRow.thread_id == thread_id,
                    )
                )
            assert explicit_updated_at is not None
            assert explicit_updated_at > no_change_updated_at
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_memory_seal_real_postgres_live_run_preempts_with_noop(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_checkpoint_mode(monkeypatch)
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"seal-preempt-{uuid.uuid4().hex}"
    try:
        async with AsyncPostgresSaver.from_conn_string(_checkpointer_url(migrated_postgres_database_url)) as raw:
            await raw.setup()
            scoped = ProjectScopedCheckpointer(raw, seed.factory)
            runtime_model = await _seed_summary_model(seed)
            app_config = _app_config(
                migrated_postgres_database_url,
                runtime_model,
            )
            source_checkpoint_id, idle_at, admitted_at = await _seed_due_thread(
                seed,
                scoped,
                app_config,
                thread_id=thread_id,
            )
            claim = await _admit_and_claim(
                seed,
                thread_id=thread_id,
                admitted_at=admitted_at,
            )

            async with seed.factory() as session, session.begin():
                await add_sealed_test_run(
                    session,
                    RunRow(
                        run_id=f"live-{uuid.uuid4().hex}",
                        thread_id=thread_id,
                        assistant_id=str(seed.project_agent_id),
                        owner_user_id=str(seed.owner_a.user_id),
                        status="running",
                        model_name=runtime_model.name,
                        multitask_strategy="reject",
                        metadata_json={},
                        kwargs_json={},
                        origin_trace_id=uuid.uuid4().hex,
                        project_id=seed.owner_a.project_id,
                        finalization_status="pending",
                    ),
                )

            handler = MemorySealJobHandler(
                seed.factory,
                app_config=app_config,
                barrier=_barrier(seed, scoped, runtime_model),
            )
            authority = JobLeaseAuthority(
                seed.factory,
                claim,
                lease_seconds=300,
            )
            outcome = await handler(claim, authority)
            assert isinstance(outcome, JobSettlement)
            assert outcome.outcome.status == "succeeded"
            await outcome.commit()

            latest = await bind_scoped_checkpoint_state(
                scoped,
                seed.owner_a,
                app_config,
                as_node="memory_seal_preempt_assert",
            ).aget(checkpoint_config(thread_id))
            assert snapshot_checkpoint_id(latest) == source_checkpoint_id
            assert [message.id for message in latest.values["messages"]] == [
                f"human-{thread_id}",
                f"ai-{thread_id}",
            ]
            assert "summary_text" not in latest.values

            async with seed.factory() as session:
                history_count = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(MemoryHistoryEntryRow)
                    .where(
                        MemoryHistoryEntryRow.project_id == seed.owner_a.project_id,
                        MemoryHistoryEntryRow.owner_user_id == str(seed.owner_a.user_id),
                        MemoryHistoryEntryRow.thread_id == thread_id,
                    )
                )
                thread = await session.scalar(
                    sa.select(ThreadMetaRow).where(
                        ThreadMetaRow.project_id == seed.owner_a.project_id,
                        ThreadMetaRow.owner_user_id == str(seed.owner_a.user_id),
                        ThreadMetaRow.thread_id == thread_id,
                    )
                )
            assert history_count == 0
            assert thread is not None
            assert thread.memory_sealed_at is None
            assert thread.updated_at == idle_at
            await _assert_job_succeeded(seed, claim)
    finally:
        await seed.engine.dispose()
