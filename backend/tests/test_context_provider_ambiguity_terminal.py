from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from support.private_thread_seed import seed_private_thread_database

from app.private_work.authorization import PrivateRunAuthorizationService
from app.private_work.context import PrivateWorkContext
from app.private_work.execution_profile import (
    RUN_EXECUTION_PROFILE_KWARG,
    EffectiveRunExecutionProfile,
    RequestedRunExecutionProfile,
    persisted_run_execution_profile,
)
from app.private_work.run_admission import (
    PersistedRunSnapshot,
    PrivateRunAdmissionService,
)
from app.private_work.run_repository import (
    PrivateRunCreate,
    PrivateRunRecord,
    PrivateRunRepository,
)
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.private_work.workload_profile import (
    RUN_WORKLOAD_PROFILE_KWARG,
    EffectiveRunWorkloadProfile,
    RequestedRunWorkloadProfile,
    persisted_run_workload_profile,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.execution import (
    AgentExecutionResult,
    PrivateRunExecution,
    PrivateRunJobHandler,
    RunAgentPrivateExecutor,
    TransientExecutionError,
)
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    MaterializedAgentRuntimePolicy,
)
from app.worker.service import JobLeaseAuthority
from deerflow.agents.memory.snip import SnipArchiveContext
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.error_codes import ContextProviderCallAmbiguousError
from deerflow.persistence.jobs.model import JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import (
    JobClaim,
    JobOwnerRef,
    JobRepository,
    JobScope,
)
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.run.model import RunRow
from deerflow.runtime.events.models import StreamFrame, StreamLeaseProof
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.sandbox.sandbox import AuthorizationRevoked

_MODEL_REF = "00000000-0000-4000-8000-000000000401"


def _execution() -> tuple[PrivateRunExecution, PrivateWorkContext]:
    now = datetime.now(UTC)
    requested = RequestedRunExecutionProfile(model_name=_MODEL_REF)
    effective = EffectiveRunExecutionProfile(
        model_name=_MODEL_REF,
        thinking_enabled=False,
        reasoning_effort="none",
        supports_vision=False,
    )
    run = PrivateRunRecord(
        run_id=str(uuid.uuid4()),
        thread_id=str(uuid.uuid4()),
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        assistant_id=str(uuid.uuid4()),
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={
            "input": {"messages": []},
            RUN_EXECUTION_PROFILE_KWARG: persisted_run_execution_profile(
                requested,
                effective,
            ),
            RUN_WORKLOAD_PROFILE_KWARG: persisted_run_workload_profile(
                RequestedRunWorkloadProfile(),
                EffectiveRunWorkloadProfile(name="interactive"),
            ),
        },
        origin_trace_id="a" * 32,
        error=None,
        model_name=_MODEL_REF,
        created_at=now,
        updated_at=now,
    )
    context = PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.UUID(run.owner_user_id),
            project_id=run.project_id,
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="context-provider-ambiguity-test",
        )
    )
    return (
        PrivateRunExecution(
            context=context,
            run=run,
            snapshot=PersistedRunSnapshot(
                assets=(),
                mcp_secrets=(),
                catalog_generation=1,
            ),
            checkpoint_namespace="",
            graph_input={"messages": []},
            command=None,
            config={},
            interrupt_before=None,
            interrupt_after=None,
            stream_mode=["values"],
            stream_subgraphs=False,
        ),
        context,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_lost", "authorization_revoked", "expected"),
    [
        (False, False, "ambiguity"),
        (True, False, "lease_lost"),
        (False, True, "authorization_revoked"),
        (False, False, "settlement_lease_lost"),
        (False, False, "settlement_authorization_revoked"),
        (False, False, "settlement_storage_error"),
    ],
    ids=[
        "valid-lease",
        "lease-lost",
        "authorization-revoked",
        "settlement-lease-lost",
        "settlement-authorization-revoked",
        "settlement-storage-error",
    ],
)
async def test_provider_ambiguity_settles_lead_context_before_terminal_error(
    monkeypatch: pytest.MonkeyPatch,
    lease_lost: bool,
    authorization_revoked: bool,
    expected: str,
) -> None:
    execution, context = _execution()
    model = ModelConfig(
        name=_MODEL_REF,
        display_name="Ambiguity test",
        description="",
        use="support.fake_models:FakeVisionBridgeChatModel",
        model="ambiguity-test",
        max_input_tokens=64_000,
    )
    model._system_model_config_id = uuid.uuid4()
    model._system_model_payload_checksum = "a" * 64
    model._system_provider_adapter = "openai"
    app_config = AppConfig(
        models=[model],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )
    observed: list[str] = []
    boundary_ref: dict[str, object] = {}

    class RuntimePolicy:
        async def materialize_run_snapshot_envelope(self, **_kwargs):
            return MaterializedAgentRuntimePolicy(
                schema_version=1,
                value=AgentRuntimePolicyValue(
                    title={"enabled": False},
                    summarization={
                        "keep": {"type": "tokens", "value": 8_000},
                    },
                ),
            )

    class Models:
        async def materialize_snapshot(self, **_kwargs):
            return model

    class Runtime:
        model_ref = model.name
        skill_root = None

        def borrow_materialized_skill_tree(self):
            return None

        async def aclose(self) -> None:
            return None

    class Assets:
        async def materialize(self, *_args, **_kwargs):
            return Runtime()

    class Checkpointer:
        def for_context(self, _context, *, thread_kind: str):
            assert thread_kind == "chat"
            return SimpleNamespace(
                set_authorization_boundary=lambda _boundary: None,
                set_context_evidence_observer=lambda _observer: None,
            )

    class ContextEvidenceObserver:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def record_settled(self) -> None:
            boundary = boundary_ref["value"]
            if expected == "settlement_lease_lost":
                boundary.lease_lost = True
                raise AuthorizationRevoked
            if expected == "settlement_authorization_revoked":
                boundary.authorization_revoked = True
                raise AuthorizationRevoked
            if expected == "settlement_storage_error":
                raise RuntimeError("Context Projection settlement unavailable")
            observed.append("settled")

    class Boundary:
        cancel_requested = False
        ambiguous_side_effect = False

        def __init__(self, *_args, **_kwargs) -> None:
            self.lease_lost = lease_lost
            self.authorization_revoked = authorization_revoked
            boundary_ref["value"] = self

        def bind_abort_event(self, _abort_event) -> None:
            return None

        def request_local_cancel(self) -> None:
            self.cancel_requested = True

    async def runner(*_args, **_kwargs):
        raise ContextProviderCallAmbiguousError(
            "Provider dispatch outcome is ambiguous",
        )

    monkeypatch.setattr(
        "app.reliability.run_execution.preparation.PrivateRunContextEvidenceObserver",
        ContextEvidenceObserver,
    )
    monkeypatch.setattr(
        "app.reliability.run_execution.executor.PrivateRunExecutionBoundary",
        Boundary,
    )
    executor = RunAgentPrivateExecutor(
        lambda: None,
        app_config=app_config,
        bridge=SimpleNamespace(),
        project_checkpointer=Checkpointer(),
        store=SimpleNamespace(),
        event_store=SimpleNamespace(),
        asset_runtime=Assets(),
        model_materializer=Models(),
        runtime_policy_materializer=RuntimePolicy(),
        agent_factory=object(),
        runner=runner,
    )
    archive_context = SnipArchiveContext(
        enabled=False,
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        namespace="default",
        preference_version=1,
        summary_model=None,
    )

    async def memory_archive_context(*_args, **_kwargs):
        return archive_context

    monkeypatch.setattr(
        executor,
        "_memory_archive_context",
        memory_archive_context,
    )
    claim = JobClaim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token="lease",
        job_type="private_run",
        scope=JobScope(context.project_id, str(context.user_id)),
        run_id=execution.run.run_id,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        origin_trace_id=execution.run.origin_trace_id,
    )
    authority = JobLeaseAuthority(lambda: None, claim, lease_seconds=30)

    if expected == "ambiguity":
        result = await executor.execute(execution, authority)
        assert result.status == "failed"
        assert result.public_error_code == "CONTEXT_PROVIDER_CALL_AMBIGUOUS"
        assert result.durable_terminal is True
        assert observed == ["settled"]
    elif expected in {"lease_lost", "settlement_lease_lost"}:
        with pytest.raises(
            TransientExecutionError,
            match="EXECUTION_AUTHORITY_UNAVAILABLE",
        ):
            await executor.execute(execution, authority)
        assert observed == []
    elif expected in {
        "authorization_revoked",
        "settlement_authorization_revoked",
    }:
        result = await executor.execute(execution, authority)
        assert result.status == "cancelled"
        assert observed == []
    else:
        with pytest.raises(
            TransientExecutionError,
            match="PRIVATE_RUN_EXECUTION_FAILED",
        ) as caught:
            await executor.execute(execution, authority)
        assert caught.value.attempt_usage is not None
        assert observed == []


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "execution_outcome",
    [
        "durable",
        "late-cancel",
        "authorization-revoked",
        "settlement-transient",
        "recovered-timeout",
    ],
)
async def test_durable_job_settlement_repairs_only_missing_ambiguity_terminal(
    migrated_postgres_database_url: str,
    execution_outcome: str,
) -> None:
    seed = await seed_private_thread_database(
        migrated_postgres_database_url,
    )
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    worker_id = uuid.uuid4()

    class AmbiguousExecutor:
        async def execute(self, _execution, authority):
            if execution_outcome == "recovered-timeout":
                raise AssertionError(
                    "recovered stream terminal must not re-enter the graph",
                )
            if execution_outcome == "settlement-transient":
                raise TransientExecutionError(
                    "PRIVATE_RUN_EXECUTION_FAILED",
                )
            if execution_outcome == "authorization-revoked":
                async with seed.factory() as session, session.begin():
                    revoked = await PrivateRunAuthorizationService.mark_revoked(
                        session,
                        project_id=seed.owner_a.project_id,
                        owner_user_id=str(seed.owner_a.user_id),
                    )
                    assert revoked == (run_id,)
            if execution_outcome == "late-cancel":
                async with seed.factory() as session, session.begin():
                    requested = await PrivateRunRepository(
                        session,
                    ).request_cancel(
                        scope=seed.owner_a_scope,
                        thread_id=thread_id,
                        run_id=run_id,
                        job_id=claim.job_id,
                        reason="user_stop",
                    )
                    assert requested == "requested"
            authority.cancel_requested = execution_outcome == "late-cancel"
            return AgentExecutionResult.failed(
                "CONTEXT_PROVIDER_CALL_AMBIGUOUS",
                retryable=False,
                durable_terminal=True,
            )

    recovered_terminal_id: str | None = None
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(
                    seed.project_agent_id,
                    "project",
                ),
            )
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="context-ambiguity-terminal-test",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=1,
                )
            )
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(
                run_id=run_id,
                kwargs={"input": {"messages": []}},
            ),
        )
        async with seed.factory() as session, session.begin():
            job = await session.get(JobRow, admitted.job.job_id)
            assert job is not None
            job.priority = 32_767
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
            )
            assert claim is not None
            assert claim.job_id == admitted.job.job_id
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )
            await PrivateRunRepository(session).begin_execution(
                scope=seed.owner_a_scope,
                run_id=run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                origin_trace_id=claim.origin_trace_id,
            )
            if execution_outcome == "recovered-timeout":
                recovered = await DbRunEventStore(
                    seed.factory,
                    run_event_notify_enabled=False,
                ).append_stream_frame(
                    session,
                    scope=seed.owner_a_scope,
                    thread_id=thread_id,
                    run_id=run_id,
                    frame=StreamFrame.end(status="timeout"),
                    lease=StreamLeaseProof(
                        job_id=claim.job_id,
                        lease_token=claim.lease_token,
                    ),
                )
                recovered_terminal_id = recovered.id

        authority = SimpleNamespace(
            bind_heartbeat_callback=lambda _callback: None,
            cancel_requested=False,
        )
        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=AmbiguousExecutor(),
            job_repository_builder=lambda session: JobRepository(
                session,
                owner_ref_hasher=lambda _owner: JobOwnerRef(
                    "test",
                    "0" * 64,
                ),
            ),
        )(
            claim,
            authority,
        )
        await settlement.commit()

        store = DbRunEventStore(
            seed.factory,
            run_event_notify_enabled=False,
        )
        async with seed.factory() as session:
            run = await session.get(RunRow, run_id)
            job = await session.get(JobRow, claim.job_id)
            terminal = await store.get_stream_terminal(
                session,
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                run_id=run_id,
            )
            if execution_outcome == "settlement-transient":
                assert run is not None and run.status == "pending"
                assert run.error is None
                assert job is not None and job.status == "retry_wait"
                assert terminal is None
                return
            if execution_outcome == "authorization-revoked":
                assert run is not None and run.status == "interrupted"
                assert run.error == "authorization_revoked"
                assert job is not None and job.status == "cancelled"
                assert terminal is not None
                assert terminal.data == {"status": "interrupted"}
                return
            if execution_outcome == "recovered-timeout":
                assert run is not None and run.status == "error"
                assert run.error == "AGENT_EXECUTION_FAILED"
                assert job is not None and job.status == "dead"
                assert terminal is not None
                assert terminal.id == recovered_terminal_id
                assert terminal.data == {"status": "timeout"}
                return
            assert run is not None and run.status == "error"
            assert run.error == "CONTEXT_PROVIDER_CALL_AMBIGUOUS"
            assert job is not None and job.status == "dead"
            assert terminal is not None
            assert terminal.data == {
                "status": "error",
                "error_code": "CONTEXT_PROVIDER_CALL_AMBIGUOUS",
            }
            terminal_id = terminal.id

        async with seed.factory() as session, session.begin():
            repaired = await store.ensure_settled_stream_terminal(
                session,
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                run_id=run_id,
                status="error",
                error_code="CONTEXT_PROVIDER_CALL_AMBIGUOUS",
            )
            terminal_count = await session.scalar(
                select(func.count())
                .select_from(RunEventRow)
                .where(
                    RunEventRow.run_id == run_id,
                    RunEventRow.category == "stream",
                    RunEventRow.event_type == "stream.end",
                )
            )
            assert repaired.id == terminal_id
            assert terminal_count == 1
    finally:
        await seed.engine.dispose()
