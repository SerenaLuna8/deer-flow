from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import get_type_hints

import pytest
from langchain_core.messages import BaseMessage
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.errors import PrivateWorkMcpQuotaExceeded
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import (
    PrivateRunCreate,
    PrivateRunRecord,
    PrivateRunRepository,
    PrivateRunSettlement,
    PrivateRunUsageSnapshot,
)
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.reliability.execution import (
    AgentExecutionResult,
    AmbiguousExternalSideEffect,
    LeaseAuthorizedStreamBridge,
    PrivateRunExecutionBoundary,
    PrivateRunJobHandler,
    PrivateRunJobTerminalPort,
    RunAgentPrivateExecutor,
    TransientExecutionError,
)
from app.reliability.workers import WorkerRegistry
from app.worker.service import JobLeaseAuthority, LeaseLost
from deerflow.persistence.jobs.sql import JobOwnerRef, JobRepository
from deerflow.persistence.private_work.file_repository import (
    PrivateFileRepository,
)
from deerflow.persistence.run.sql import RunRepository as TokenRunRepository
from deerflow.runtime import RunStatus
from deerflow.runtime.events.models import (
    StreamLeaseProof,
    StreamWriteAuthorityRequired,
    StreamWriteLeaseLost,
)
from deerflow.runtime.events.stream import PostgresStreamBridge
from deerflow.sandbox.sandbox import AuthorizationRevoked


def test_private_run_repository_return_annotations_match_runtime_contract() -> None:
    assert get_type_hints(PrivateRunRepository.create)["return"] is PrivateRunRecord
    assert get_type_hints(PrivateRunRepository.settle_execution)["return"] is PrivateRunSettlement


async def _admit_and_claim(
    seed,
    *,
    thread_id: str,
    run_kwargs: dict | None = None,
):
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
    kwargs = {
        "input": {"messages": [{"role": "user", "content": "hello"}]},
        "command": None,
        "config": {
            "configurable": {"thread_id": thread_id},
            "context": {},
        },
        "interrupt_before": "*",
        "interrupt_after": [],
        "stream_mode": ["values"],
        "stream_subgraphs": False,
    }
    if run_kwargs is not None:
        kwargs.update(run_kwargs)
    admitted = await PrivateRunAdmissionService(seed.factory).admit(
        seed.owner_a,
        thread_id,
        PrivateRunCreate(
            kwargs=kwargs,
        ),
    )
    worker_id = uuid.uuid4()
    await WorkerRegistry(seed.factory, version="test-m6-private").register(
        worker_id,
        frozenset({"private_run"}),
        1,
    )
    async with seed.factory() as session, session.begin():
        repository = JobRepository(session)
        claim = await repository.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"private_run"}),
            lease_seconds=90,
        )
        assert claim is not None
        assert claim.job_id == admitted.job.job_id
        assert await repository.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
        )
    return admitted, claim


@pytest.mark.postgres
@pytest.mark.anyio
async def test_private_run_handler_reuses_exact_run_snapshot_and_checkpoint(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    captured = []

    class Executor:
        async def execute(self, execution, authority):
            captured.append(execution)
            assert authority.cancel_requested is False
            return AgentExecutionResult.succeeded()

    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-worker-{uuid.uuid4()}",
        )
        handler = PrivateRunJobHandler(seed.factory, executor=Executor())
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()
        await settlement.commit()

        assert len(captured) == 1
        execution = captured[0]
        assert execution.run.run_id == admitted.run.run_id
        assert execution.snapshot == admitted.snapshot
        assert execution.checkpoint_namespace == admitted.run.run_id
        assert execution.interrupt_before == "*"

        async with seed.factory() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT r.status AS run_status,j.status AS job_status,
                        r.execution_lease_token_hash
                        FROM runs r JOIN jobs j ON j.id=r.job_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert tuple(row) == ("success", "succeeded", None)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_mcp_quota_rejection_stays_retry_safe_before_remote_dispatch(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)

    class RejectMcpQuota:
        async def consume_mcp_dispatch(self, context, *, dispatch_id):
            del dispatch_id
            raise PrivateWorkMcpQuotaExceeded(context.request_id)

        async def reserve_file(self, session, context, *, file_id, size):
            del session, context, file_id, size

        async def release_file(self, session, scope, *, file_id, size, request_id):
            del session, scope, file_id, size, request_id

    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-mcp-quota-safe-{uuid.uuid4()}",
        )
        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).begin_execution(
                scope=seed.owner_a.resource_scope,
                run_id=admitted.run.run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
            )
        boundary = PrivateRunExecutionBoundary(
            seed.factory,
            context=seed.owner_a,
            claim=claim,
            quota=RejectMcpQuota(),
        )

        with pytest.raises(PrivateWorkMcpQuotaExceeded):
            await boundary.before_mcp_tool_dispatch()

        assert boundary.ambiguous_side_effect is False
        async with seed.factory() as session:
            retry_safety = await session.scalar(
                text("SELECT retry_safety FROM jobs WHERE id=:job_id"),
                {"job_id": claim.job_id},
            )
        assert retry_safety == "safe"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_mcp_quota_failure_keeps_stable_public_job_code(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)

    class Executor:
        async def execute(self, execution, _authority):
            raise PrivateWorkMcpQuotaExceeded(execution.context.request_id)

    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-mcp-quota-code-{uuid.uuid4()}",
        )
        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        assert settlement.outcome.public_error_code == "PROJECT_MCP_QUOTA_EXCEEDED"
        await settlement.commit()

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT j.status,j.retry_safety,j.public_error_code,r.status
                           FROM jobs j JOIN runs r ON r.job_id=j.id
                           WHERE j.id=:job_id"""
                    ),
                    {"job_id": admitted.job.job_id},
                )
            ).one()
        assert tuple(state) == (
            "retry_wait",
            "safe",
            "PROJECT_MCP_QUOTA_EXCEEDED",
            "pending",
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_stale_job_lease_cannot_publish_private_run_terminal(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)

    class Executor:
        async def execute(self, _execution, _authority):
            return AgentExecutionResult.succeeded(
                attempt_usage=PrivateRunUsageSnapshot(
                    total_input_tokens=12,
                    total_output_tokens=3,
                    total_tokens=15,
                    llm_call_count=1,
                    lead_agent_tokens=10,
                    subagent_tokens=4,
                    middleware_tokens=1,
                    token_usage_by_model={
                        "stale-model": {
                            "input_tokens": 12,
                            "output_tokens": 3,
                            "total_tokens": 15,
                        }
                    },
                ),
            )

    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-stale-{uuid.uuid4()}",
        )
        settlement = await PrivateRunJobHandler(seed.factory, executor=Executor())(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE jobs SET lease_expires_at=:expired
                    WHERE id=:job_id"""
                ),
                {
                    "expired": datetime.now(UTC) - timedelta(seconds=1),
                    "job_id": claim.job_id,
                },
            )

        with pytest.raises(LeaseLost):
            await settlement.commit()

        async with seed.factory() as session:
            states = (
                await session.execute(
                    text(
                        """SELECT r.status,j.status,r.total_tokens
                        FROM runs r JOIN jobs j ON j.id=r.job_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert tuple(states) == ("running", "running", 0)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_transient_failure_requeues_same_run_and_retains_snapshot(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)

    class Executor:
        async def execute(self, _execution, _authority):
            raise TransientExecutionError(
                "MODEL_INITIALIZATION_FAILED",
                attempt_usage=PrivateRunUsageSnapshot(
                    total_input_tokens=13,
                    total_output_tokens=2,
                    total_tokens=15,
                    llm_call_count=1,
                    lead_agent_tokens=15,
                    token_usage_by_model={
                        "transient-model": {
                            "input_tokens": 13,
                            "output_tokens": 2,
                            "total_tokens": 15,
                        }
                    },
                ),
            )

    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-retry-{uuid.uuid4()}",
        )
        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
            retry_initial_seconds=2,
            retry_max_seconds=300,
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        async with seed.factory() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT r.run_id,r.status AS run_status,
                        j.status AS job_status,j.public_error_code,
                        r.total_tokens,r.token_usage_by_model,
                        (SELECT count(*) FROM run_asset_versions a
                         WHERE a.run_id=r.run_id) AS snapshot_count
                        FROM runs r JOIN jobs j ON j.id=r.job_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert row.run_id == admitted.run.run_id
        assert row.run_status == "pending"
        assert row.job_status == "retry_wait"
        assert row.public_error_code == "MODEL_INITIALIZATION_FAILED"
        assert row.total_tokens == 15
        assert row.token_usage_by_model == {
            "transient-model": {
                "input_tokens": 13,
                "output_tokens": 2,
                "total_tokens": 15,
            }
        }
        assert row.snapshot_count == len(admitted.snapshot.assets)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_missing_exact_model_is_terminal_without_retry(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-terminal-model-stale-{uuid.uuid4()}",
        )
        executor = RunAgentPrivateExecutor(
            seed.factory,
            app_config=SimpleNamespace(
                get_model_config=lambda _name: None,
                models=[],
                skills=SimpleNamespace(container_path="/mnt/skills"),
            ),
            bridge=object(),
            project_checkpointer=object(),
            store=object(),
            event_store=object(),
            agent_factory=object(),
        )
        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=executor,
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT r.status,j.status,j.public_error_code,
                        j.attempt_count,
                        (SELECT outcome FROM job_attempts
                         WHERE job_id=j.id ORDER BY attempt_number DESC LIMIT 1),
                        (SELECT count(*) FROM dead_jobs WHERE job_id=j.id)
                        FROM runs r JOIN jobs j ON j.id=r.job_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()

        assert tuple(state) == (
            "error",
            "dead",
            "RUN_ASSET_STALE",
            1,
            "dead",
            1,
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_retry_resumes_latest_confirmed_checkpoint_without_replaying_command(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    executions = []

    class Saver:
        def __init__(self, parent) -> None:
            self.parent = parent

        async def aget_tuple_already_authorized(self, _config, *, session):
            assert session.in_transaction()
            return SimpleNamespace(
                config={
                    "configurable": {
                        "checkpoint_id": self.parent.current,
                    }
                }
            )

    class ProjectCheckpointer:
        current = "checkpoint-before-attempt"

        def for_context(self, _context):
            return Saver(self)

    checkpointer = ProjectCheckpointer()

    class Executor:
        async def execute(self, execution, _authority):
            executions.append(execution)
            if len(executions) == 1:
                checkpointer.current = "checkpoint-after-progress"
                return AgentExecutionResult.failed(
                    "MODEL_TEMPORARILY_UNAVAILABLE",
                    attempt_usage=PrivateRunUsageSnapshot(
                        total_input_tokens=10,
                        total_output_tokens=2,
                        total_tokens=12,
                        llm_call_count=1,
                        lead_agent_tokens=8,
                        subagent_tokens=3,
                        middleware_tokens=1,
                        token_usage_by_model={
                            "retry-model": {
                                "input_tokens": 10,
                                "output_tokens": 2,
                                "total_tokens": 12,
                            }
                        },
                    ),
                )
            return AgentExecutionResult.succeeded(
                attempt_usage=PrivateRunUsageSnapshot(
                    total_input_tokens=20,
                    total_output_tokens=5,
                    total_tokens=25,
                    llm_call_count=2,
                    lead_agent_tokens=20,
                    subagent_tokens=4,
                    middleware_tokens=1,
                    token_usage_by_model={
                        "retry-model": {
                            "input_tokens": 20,
                            "output_tokens": 5,
                            "total_tokens": 25,
                        }
                    },
                ),
            )

    try:
        admitted, first_claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-checkpoint-takeover-{uuid.uuid4()}",
            run_kwargs={
                "command": {"resume": "approved"},
                "config": {
                    "configurable": {
                        "thread_id": "ignored-by-authority",
                        "checkpoint_ns": "old-namespace",
                        "checkpoint_id": "explicit-old-checkpoint",
                        "checkpoint_map": {"": "explicit-old-checkpoint"},
                    }
                },
            },
        )
        handler = PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
            retry_initial_seconds=1,
            project_checkpointer=checkpointer,
        )
        first = await handler(
            first_claim,
            JobLeaseAuthority(seed.factory, first_claim, lease_seconds=90),
        )
        await first.commit()
        async with seed.factory() as session, session.begin():
            await session.execute(
                text("UPDATE jobs SET available_at=now() WHERE id=:job_id"),
                {"job_id": first_claim.job_id},
            )

        second_worker = uuid.uuid4()
        await WorkerRegistry(
            seed.factory,
            version="test-m6-checkpoint-takeover",
        ).register(second_worker, frozenset({"private_run"}), 1)
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            second_claim = await jobs.claim_next(
                worker_id=second_worker,
                capabilities=frozenset({"private_run"}),
                lease_seconds=90,
            )
            assert second_claim is not None
            assert second_claim.job_id == first_claim.job_id
            assert await jobs.mark_running(
                second_claim.job_id,
                lease_token=second_claim.lease_token,
            )

        second = await handler(
            second_claim,
            JobLeaseAuthority(seed.factory, second_claim, lease_seconds=90),
        )
        await second.commit()

        assert executions[0].run.run_id == admitted.run.run_id
        assert executions[0].command == {"resume": "approved"}
        assert executions[0].resume_from_checkpoint is False
        takeover = executions[1]
        assert takeover.run.run_id == admitted.run.run_id
        assert takeover.snapshot == admitted.snapshot
        assert takeover.resume_from_checkpoint is True
        assert takeover.graph_input is None
        assert takeover.command is None
        assert RunAgentPrivateExecutor._graph_input(takeover) is None
        configurable = takeover.config["configurable"]
        assert configurable["checkpoint_ns"] == ""
        assert "checkpoint_id" not in configurable
        assert "checkpoint_map" not in configurable
        async with seed.factory() as session:
            usage = (
                await session.execute(
                    text(
                        """SELECT total_input_tokens,total_output_tokens,
                        total_tokens,llm_call_count,lead_agent_tokens,
                        subagent_tokens,middleware_tokens,token_usage_by_model
                        FROM runs WHERE run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert tuple(usage[:-1]) == (30, 7, 37, 3, 28, 7, 2)
        assert usage.token_usage_by_model == {
            "retry-model": {
                "input_tokens": 30,
                "output_tokens": 7,
                "total_tokens": 37,
            }
        }
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_execution_boundary_rejects_stale_lease_before_side_effect(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    observed = False

    class Executor:
        async def execute(self, execution, _authority):
            nonlocal observed
            boundary = PrivateRunExecutionBoundary(
                seed.factory,
                context=execution.context,
                claim=claim,
            )
            await boundary.before_checkpoint_read()
            async with seed.factory() as session, session.begin():
                await session.execute(
                    text(
                        """UPDATE jobs SET lease_token_hash=:other
                        WHERE id=:job_id"""
                    ),
                    {"other": "f" * 64, "job_id": claim.job_id},
                )
            with pytest.raises(AuthorizationRevoked):
                await boundary.before_tool_call()
            observed = boundary.lease_lost
            raise TransientExecutionError("EXECUTION_AUTHORITY_UNAVAILABLE")

    try:
        _admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-boundary-{uuid.uuid4()}",
        )
        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        assert observed is True
        with pytest.raises(LeaseLost):
            await settlement.commit()
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_stale_worker_cannot_publish_stream_end(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-stale-stream-{uuid.uuid4()}",
        )
        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).begin_execution(
                scope=seed.owner_a.resource_scope,
                run_id=admitted.run.run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
            )
        boundary = PrivateRunExecutionBoundary(
            seed.factory,
            context=seed.owner_a,
            claim=claim,
        )
        raw_bridge = PostgresStreamBridge(seed.factory)
        bridge = LeaseAuthorizedStreamBridge(
            raw_bridge,
            boundary,
            scope=seed.owner_a.resource_scope,
            thread_id=admitted.run.thread_id,
            terminal_status=lambda: "success",
        )
        async with seed.factory() as session, session.begin():
            await session.execute(
                text("UPDATE jobs SET lease_token_hash=:other WHERE id=:job_id"),
                {"other": "c" * 64, "job_id": claim.job_id},
            )

        with pytest.raises(AuthorizationRevoked):
            await bridge.publish_end(admitted.run.run_id)

        frames = await raw_bridge.read_after(
            seed.owner_a.resource_scope,
            admitted.run.thread_id,
            cursor=0,
            limit=10,
            run_id=admitted.run.run_id,
        )
        assert frames == ()
        assert boundary.lease_lost is True
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_durable_stream_append_revalidates_current_project_membership(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-stream-revoked-{uuid.uuid4()}",
        )
        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).begin_execution(
                scope=seed.owner_a.resource_scope,
                run_id=admitted.run.run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
            )
            await session.execute(
                text(
                    """UPDATE project_memberships
                    SET status='removed',version=version+1
                    WHERE id=:membership_id"""
                ),
                {"membership_id": seed.owner_a.membership_id},
            )
        raw_bridge = PostgresStreamBridge(seed.factory)
        boundary = PrivateRunExecutionBoundary(
            seed.factory,
            context=seed.owner_a,
            claim=claim,
        )
        bridge = LeaseAuthorizedStreamBridge(
            raw_bridge,
            boundary,
            scope=seed.owner_a.resource_scope,
            thread_id=admitted.run.thread_id,
            terminal_status=lambda: "success",
        )

        with pytest.raises(AuthorizationRevoked):
            await bridge.publish_end(admitted.run.run_id)

        frames = await raw_bridge.read_after(
            seed.owner_a.resource_scope,
            admitted.run.thread_id,
            cursor=0,
            limit=10,
            run_id=admitted.run.run_id,
        )
        assert frames == ()
        assert boundary.authorization_revoked is True
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_sandbox_restore_authority_check_keeps_job_retry_safe(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)

    class Executor:
        async def execute(self, execution, _authority):
            boundary = PrivateRunExecutionBoundary(
                seed.factory,
                context=execution.context,
                claim=claim,
            )
            await boundary.before_sandbox_restore()
            async with seed.factory() as session:
                retry_safety = await session.scalar(
                    text("SELECT retry_safety FROM jobs WHERE id=:job_id"),
                    {"job_id": claim.job_id},
                )
            assert retry_safety == "safe"
            return AgentExecutionResult.succeeded()

    try:
        _admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-safe-sandbox-restore-{uuid.uuid4()}",
        )
        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_external_side_effect_boundary_persists_unknown_before_call(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)

    class Executor:
        async def execute(self, execution, _authority):
            boundary = PrivateRunExecutionBoundary(
                seed.factory,
                context=execution.context,
                claim=claim,
            )
            await boundary.before_tool_call()
            async with seed.factory() as session:
                retry_safety = await session.scalar(
                    text("SELECT retry_safety FROM jobs WHERE id=:job_id"),
                    {"job_id": claim.job_id},
                )
            assert retry_safety == "unknown"
            return AgentExecutionResult.succeeded()

    try:
        _admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-side-effect-marker-{uuid.uuid4()}",
        )
        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        # The marker committed before the executor was allowed to continue;
        # settlement is deliberately delayed to model a process crash window.
        async with seed.factory() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT r.status,j.status,j.retry_safety
                        FROM runs r JOIN jobs j ON j.id=r.job_id
                        WHERE j.id=:job_id"""
                    ),
                    {"job_id": claim.job_id},
                )
            ).one()
        assert tuple(row) == ("running", "running", "unknown")
        await settlement.commit()
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_expired_ambiguous_job_converges_run_and_staging_atomically(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    staged_id = uuid.uuid4()

    class Executor:
        async def execute(self, execution, _authority):
            boundary = PrivateRunExecutionBoundary(
                seed.factory,
                context=execution.context,
                claim=claim,
            )
            await boundary.before_file_finalization()
            async with seed.factory() as session, session.begin():
                await PrivateFileRepository(session).stage(
                    scope=execution.context.resource_scope,
                    thread_id=execution.run.thread_id,
                    kind="output",
                    logical_path=f"outputs/.deerflow-staging-{staged_id.hex}",
                    media_type="text/plain",
                    created_by_run_id=execution.run.run_id,
                    file_id=staged_id,
                )
            return AgentExecutionResult.succeeded()

    def production_jobs(session):
        return JobRepository(
            session,
            owner_ref_hasher=lambda _owner: JobOwnerRef(
                key_id="m6-test",
                hmac_hex="b" * 64,
            ),
            terminal_port=PrivateRunJobTerminalPort(),
        )

    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-crash-cleanup-{uuid.uuid4()}",
        )
        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
            job_repository_builder=production_jobs,
        )(
            claim,
            JobLeaseAuthority(
                seed.factory,
                claim,
                lease_seconds=90,
                repository_builder=production_jobs,
            ),
        )
        assert settlement.outcome.status == "succeeded"
        async with seed.factory() as session, session.begin():
            await session.execute(
                text("UPDATE jobs SET lease_expires_at=:expired WHERE id=:job_id"),
                {
                    "expired": datetime.now(UTC) - timedelta(seconds=1),
                    "job_id": claim.job_id,
                },
            )

        replacement = uuid.uuid4()
        await WorkerRegistry(
            seed.factory,
            version="test-m6-crash-cleanup",
        ).register(replacement, frozenset({"private_run"}), 1)
        async with seed.factory() as session, session.begin():
            assert (
                await production_jobs(session).claim_next(
                    worker_id=replacement,
                    capabilities=frozenset({"private_run"}),
                    lease_seconds=90,
                )
                is None
            )

        async with seed.factory() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT r.status,r.error,r.finalization_status,
                        j.status,j.public_error_code,
                        (SELECT count(*) FROM files f
                         WHERE f.created_by_run_id=r.run_id
                           AND f.status='staging') AS staging_count
                        FROM runs r JOIN jobs j ON j.id=r.job_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert tuple(row) == (
            "error",
            "SIDE_EFFECT_STATE_UNKNOWN",
            "pending",
            "dead",
            "SIDE_EFFECT_STATE_UNKNOWN",
            0,
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_ambiguous_side_effect_is_dead_and_never_retried(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)

    class Executor:
        async def execute(self, _execution, _authority):
            raise AmbiguousExternalSideEffect(
                attempt_usage=PrivateRunUsageSnapshot(
                    total_input_tokens=21,
                    total_output_tokens=4,
                    total_tokens=25,
                    llm_call_count=1,
                    lead_agent_tokens=25,
                    token_usage_by_model={
                        "ambiguous-model": {
                            "input_tokens": 21,
                            "output_tokens": 4,
                            "total_tokens": 25,
                        }
                    },
                )
            )

    def jobs(session):
        return JobRepository(
            session,
            owner_ref_hasher=lambda _owner: JobOwnerRef(
                key_id="m6-test",
                hmac_hex="a" * 64,
            ),
        )

    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-ambiguous-{uuid.uuid4()}",
        )
        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
            job_repository_builder=jobs,
        )(
            claim,
            JobLeaseAuthority(
                seed.factory,
                claim,
                lease_seconds=90,
                repository_builder=jobs,
            ),
        )
        await settlement.commit()

        async with seed.factory() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT r.status AS run_status,
                        r.error AS run_error,j.status AS job_status,
                        j.retry_safety,j.public_error_code,
                        (SELECT count(*) FROM dead_jobs d
                         WHERE d.job_id=j.id) AS dead_count,
                        r.total_tokens,r.token_usage_by_model
                        FROM runs r JOIN jobs j ON j.id=r.job_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert tuple(row[:-1]) == (
            "error",
            "SIDE_EFFECT_STATE_UNKNOWN",
            "dead",
            "unknown",
            "SIDE_EFFECT_STATE_UNKNOWN",
            1,
            25,
        )
        assert row.token_usage_by_model == {
            "ambiguous-model": {
                "input_tokens": 21,
                "output_tokens": 4,
                "total_tokens": 25,
            }
        }
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_production_executor_materializes_running_snapshot_and_calls_runner(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    captured: dict[str, object] = {}

    class ScopedCheckpointer:
        def set_authorization_boundary(self, boundary) -> None:
            captured["checkpoint_boundary"] = boundary

    class ProjectCheckpointer:
        def for_context(self, context):
            captured["checkpoint_context"] = context
            return ScopedCheckpointer()

    async def runner(
        _bridge,
        run_manager,
        record,
        **kwargs,
    ) -> None:
        captured["record"] = record
        captured.update(kwargs)
        assert isinstance(kwargs["graph_input"]["messages"][0], BaseMessage)
        await run_manager.update_run_completion(
            record.run_id,
            status=RunStatus.success.value,
            total_input_tokens=120,
            total_output_tokens=30,
            total_tokens=150,
            llm_call_count=2,
            lead_agent_tokens=100,
            subagent_tokens=40,
            middleware_tokens=10,
            token_usage_by_model={
                "test-model": {
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "total_tokens": 150,
                },
            },
        )
        await run_manager.set_status(record.run_id, RunStatus.success)

    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-production-executor-{uuid.uuid4()}",
        )
        app_config = SimpleNamespace(
            get_model_config=lambda name: SimpleNamespace(name=name) if name == "test-model" else None,
            skills=SimpleNamespace(container_path="/mnt/skills"),
            run_events=SimpleNamespace(track_token_usage=True),
        )
        executor = RunAgentPrivateExecutor(
            seed.factory,
            app_config=app_config,
            bridge=object(),
            project_checkpointer=ProjectCheckpointer(),
            store=object(),
            event_store=object(),
            runner=runner,
            agent_factory=object(),
        )
        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=executor,
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()
        await settlement.commit()

        assert captured["checkpoint_context"].resource_scope == seed.owner_a.resource_scope
        assert captured["checkpoint_boundary"].execution_job_id == admitted.job.job_id
        assert captured["interrupt_before"] == "*"
        assert captured["record"].run_id == admitted.run.run_id
        async with seed.factory() as session:
            persisted = (
                await session.execute(
                    text(
                        """SELECT r.status,j.status,
                        r.total_input_tokens,r.total_output_tokens,r.total_tokens,
                        r.llm_call_count,r.lead_agent_tokens,r.subagent_tokens,
                        r.middleware_tokens,r.token_usage_by_model
                        FROM runs r JOIN jobs j ON j.id=r.job_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert tuple(persisted[:-1]) == (
            "success",
            "succeeded",
            120,
            30,
            150,
            2,
            100,
            40,
            10,
        )
        assert persisted.token_usage_by_model == {
            "test-model": {
                "input_tokens": 120,
                "output_tokens": 30,
                "total_tokens": 150,
            },
        }
        token_store = TokenRunRepository(seed.factory)
        project_owner_totals = await token_store.aggregate_tokens_by_thread(
            admitted.run.thread_id,
            scope=seed.owner_a_scope,
        )
        assert project_owner_totals == {
            "total_tokens": 150,
            "total_input_tokens": 120,
            "total_output_tokens": 30,
            "total_runs": 1,
            "by_model": {"test-model": {"tokens": 150, "runs": 1}},
            "by_caller": {
                "lead_agent": 100,
                "subagent": 40,
                "middleware": 10,
            },
        }
        assert (
            await token_store.aggregate_tokens_by_thread(
                admitted.run.thread_id,
                scope=seed.owner_b_scope,
            )
        )["total_tokens"] == 0
        assert (
            await token_store.aggregate_tokens_by_thread(
                admitted.run.thread_id,
                scope=seed.project_b_owner_a_scope,
            )
        )["total_tokens"] == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_job_heartbeat_is_mirrored_to_run_execution_lease(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    mirrored = False

    class Executor:
        async def execute(self, execution, authority):
            nonlocal mirrored
            await authority.heartbeat()
            async with seed.factory() as session:
                row = (
                    await session.execute(
                        text(
                            """SELECT
                            r.execution_heartbeat_at=j.heartbeat_at AS heartbeat,
                            r.execution_lease_expires_at=j.lease_expires_at AS expiry
                            FROM runs r JOIN jobs j ON j.id=r.job_id
                            WHERE r.run_id=:run_id"""
                        ),
                        {"run_id": execution.run.run_id},
                    )
                ).one()
            mirrored = bool(row.heartbeat and row.expiry)
            return AgentExecutionResult.succeeded()

    try:
        _admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-heartbeat-mirror-{uuid.uuid4()}",
        )
        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()
        assert mirrored is True
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_revoked_membership_cancels_claim_without_invoking_agent(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    executor_calls = 0

    class Executor:
        async def execute(self, _execution, _authority):
            nonlocal executor_calls
            executor_calls += 1
            return AgentExecutionResult.succeeded()

    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-revoked-before-start-{uuid.uuid4()}",
        )
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE project_memberships
                    SET status='removed',version=version+1
                    WHERE id=:membership_id"""
                ),
                {"membership_id": seed.owner_a.membership_id},
            )

        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        assert executor_calls == 0
        async with seed.factory() as session:
            states = (
                await session.execute(
                    text(
                        """SELECT r.status,j.status
                        FROM runs r JOIN jobs j ON j.id=r.job_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert tuple(states) == ("interrupted", "cancelled")
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_worker_retry_adopts_durable_terminal_without_replaying_graph(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    executor_calls = 0

    class Executor:
        async def execute(self, _execution, _authority):
            nonlocal executor_calls
            executor_calls += 1
            return AgentExecutionResult.succeeded()

    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-terminal-takeover-{uuid.uuid4()}",
        )
        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).begin_execution(
                scope=seed.owner_a.resource_scope,
                run_id=admitted.run.run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
            )
        raw_bridge = PostgresStreamBridge(seed.factory)
        with pytest.raises(StreamWriteAuthorityRequired):
            await raw_bridge.publish_terminal(
                seed.owner_a.resource_scope,
                admitted.run.thread_id,
                admitted.run.run_id,
                status="success",
            )
        with pytest.raises(StreamWriteLeaseLost):
            await raw_bridge.publish_terminal(
                seed.owner_a.resource_scope,
                admitted.run.thread_id,
                admitted.run.run_id,
                status="success",
                lease=StreamLeaseProof(
                    job_id=claim.job_id,
                    lease_token="forged-token",
                ),
            )
        authorized_bridge = LeaseAuthorizedStreamBridge(
            raw_bridge,
            PrivateRunExecutionBoundary(
                seed.factory,
                context=seed.owner_a,
                claim=claim,
            ),
            scope=seed.owner_a.resource_scope,
            thread_id=admitted.run.thread_id,
            terminal_status=lambda: "success",
        )
        await authorized_bridge.publish_end(admitted.run.run_id)
        terminal = (
            await raw_bridge.read_after(
                seed.owner_a.resource_scope,
                admitted.run.thread_id,
                cursor=0,
                limit=10,
                run_id=admitted.run.run_id,
            )
        )[-1]

        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        assert executor_calls == 0
        assert settlement.outcome.status == "succeeded"
        retry_terminal = await PostgresStreamBridge(seed.factory).publish_terminal(
            seed.owner_a_scope,
            admitted.run.thread_id,
            admitted.run.run_id,
            status="success",
        )
        assert retry_terminal.id == terminal.id
        assert retry_terminal.created is False
        async with seed.factory() as session:
            states = (
                await session.execute(
                    text(
                        """SELECT r.status,j.status,
                                  (SELECT count(*) FROM run_events e
                                   WHERE e.run_id=r.run_id
                                     AND e.category='stream'
                                     AND e.event_type='stream.end')
                           FROM runs r JOIN jobs j ON j.id=r.job_id
                           WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert tuple(states) == ("success", "succeeded", 1)
    finally:
        await seed.engine.dispose()


async def _wait_for_postgres_advisory_wait(factory) -> None:
    for _ in range(200):
        async with factory() as session:
            waiting = await session.scalar(
                text(
                    """SELECT EXISTS (
                        SELECT 1 FROM pg_stat_activity
                        WHERE datname=current_database()
                          AND pid<>pg_backend_pid()
                          AND wait_event_type='Lock'
                          AND wait_event='advisory'
                    )"""
                )
            )
        if waiting:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("stream append did not wait on the thread advisory lock")


@pytest.mark.postgres
@pytest.mark.anyio
async def test_stream_append_revalidates_lease_after_waiting_for_thread_lock(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    publish_task = None
    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-stream-lease-race-{uuid.uuid4()}",
        )
        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).begin_execution(
                scope=seed.owner_a.resource_scope,
                run_id=admitted.run.run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
            )
        boundary = PrivateRunExecutionBoundary(
            seed.factory,
            context=seed.owner_a,
            claim=claim,
        )
        bridge = LeaseAuthorizedStreamBridge(
            PostgresStreamBridge(seed.factory),
            boundary,
            scope=seed.owner_a.resource_scope,
            thread_id=admitted.run.thread_id,
            terminal_status=lambda: "success",
        )

        async with seed.factory() as blocker, blocker.begin():
            await blocker.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(CAST(:thread_id AS text))::bigint)"),
                {"thread_id": admitted.run.thread_id},
            )
            publish_task = asyncio.create_task(
                bridge.publish_end(admitted.run.run_id),
            )
            await _wait_for_postgres_advisory_wait(seed.factory)
            async with seed.factory() as session, session.begin():
                await session.execute(
                    text("UPDATE jobs SET lease_token_hash=:successor WHERE id=:job_id"),
                    {
                        "successor": "d" * 64,
                        "job_id": claim.job_id,
                    },
                )

        with pytest.raises(AuthorizationRevoked):
            await publish_task
        frames = await PostgresStreamBridge(seed.factory).read_after(
            seed.owner_a.resource_scope,
            admitted.run.thread_id,
            cursor=0,
            limit=10,
            run_id=admitted.run.run_id,
        )
        assert frames == ()
        assert boundary.lease_lost is True
    finally:
        if publish_task is not None and not publish_task.done():
            publish_task.cancel()
            await asyncio.gather(publish_task, return_exceptions=True)
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_cancel_requested_before_terminal_is_persisted_as_interrupted(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-terminal-cancel-write-{uuid.uuid4()}",
        )
        async with seed.factory() as session, session.begin():
            runs = PrivateRunRepository(session)
            await runs.begin_execution(
                scope=seed.owner_a.resource_scope,
                run_id=admitted.run.run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
            )
            assert (
                await runs.request_cancel(
                    scope=seed.owner_a.resource_scope,
                    thread_id=admitted.run.thread_id,
                    run_id=admitted.run.run_id,
                    job_id=claim.job_id,
                    reason="user_requested",
                )
                == "requested"
            )
        bridge = PostgresStreamBridge(seed.factory)
        await LeaseAuthorizedStreamBridge(
            bridge,
            PrivateRunExecutionBoundary(
                seed.factory,
                context=seed.owner_a,
                claim=claim,
            ),
            scope=seed.owner_a.resource_scope,
            thread_id=admitted.run.thread_id,
            terminal_status=lambda: "success",
        ).publish_end(admitted.run.run_id)
        terminal = (
            await bridge.read_after(
                seed.owner_a.resource_scope,
                admitted.run.thread_id,
                cursor=0,
                limit=10,
                run_id=admitted.run.run_id,
            )
        )[-1]
        assert terminal.terminal is True
        assert terminal.data == {"status": "interrupted"}
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_success_terminal_wins_over_late_cancel_during_takeover(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    executor_calls = 0

    class Executor:
        async def execute(self, _execution, _authority):
            nonlocal executor_calls
            executor_calls += 1
            return AgentExecutionResult.succeeded()

    try:
        admitted, claim = await _admit_and_claim(
            seed,
            thread_id=f"m6-terminal-cancel-takeover-{uuid.uuid4()}",
        )
        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).begin_execution(
                scope=seed.owner_a.resource_scope,
                run_id=admitted.run.run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
            )
        await LeaseAuthorizedStreamBridge(
            PostgresStreamBridge(seed.factory),
            PrivateRunExecutionBoundary(
                seed.factory,
                context=seed.owner_a,
                claim=claim,
            ),
            scope=seed.owner_a.resource_scope,
            thread_id=admitted.run.thread_id,
            terminal_status=lambda: "success",
        ).publish_end(admitted.run.run_id)
        async with seed.factory() as session, session.begin():
            assert (
                await PrivateRunRepository(session).request_cancel(
                    scope=seed.owner_a.resource_scope,
                    thread_id=admitted.run.thread_id,
                    run_id=admitted.run.run_id,
                    job_id=claim.job_id,
                    reason="user_requested",
                )
                == "requested"
            )

        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        assert executor_calls == 0
        assert settlement.outcome.status == "succeeded"
        async with seed.factory() as session:
            states = (
                await session.execute(
                    text(
                        """SELECT r.status,j.status
                        FROM runs r JOIN jobs j ON j.id=r.job_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert tuple(states) == ("success", "succeeded")
        terminal = (
            await PostgresStreamBridge(seed.factory).read_after(
                seed.owner_a_scope,
                admitted.run.thread_id,
                cursor=0,
                limit=10,
                run_id=admitted.run.run_id,
            )
        )[-1]
        assert terminal.data == {"status": "success"}
    finally:
        await seed.engine.dispose()
